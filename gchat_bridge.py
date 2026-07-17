#!/usr/bin/env python3
"""
gchat_bridge.py — Google Chat ↔ AI backend bridge

Frontend counterpart of bot.py (Discord). Both speak the same backend CLI
contract via bridge_core, so claude CLI / codex-adapter.py / openai-adapter.py
all plug in unchanged — set CLAUDE_BIN exactly as you would for bot.py.

Transport: Google Cloud Pub/Sub *pull* subscription (outbound gRPC only —
works behind NAT with no public HTTPS endpoint). Replies go through the Chat
API (spaces.messages.create) as the app itself (scope chat.bot).

Google Chat delivers MESSAGE events only for DMs and @mentions of the app.

Setup: see docs/gchat-setup.md.  Run: python3 gchat_bridge.py --env .env.gchat
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from collections import OrderedDict, defaultdict

from dotenv import load_dotenv

import bridge_core

# 支援 --env /path/to/.env，多個 bridge 實例共用同一份程式
_env_path: str | None = None
for _i, _arg in enumerate(sys.argv[1:], 1):
    if _arg == "--env" and _i < len(sys.argv) - 1:
        _env_path = sys.argv[_i + 1]
        break

load_dotenv(_env_path)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.expanduser(os.getenv("LOG_FILE", "~/gchat-claude-bridge.log"))
        ),
    ],
)
log = logging.getLogger(__name__)

# ── config (backend vars are identical to bot.py's — same .env conventions) ──
GCHAT_PROJECT_ID = os.getenv("GCHAT_PROJECT_ID", "")
GCHAT_SUBSCRIPTION_ID = os.getenv("GCHAT_SUBSCRIPTION_ID", "")
# service-account key; the google libs also honour this env var natively
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

WORKING_DIR = os.path.expanduser(os.getenv("WORKING_DIR", "~/agent-dev"))
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
CLAUDE_EXTRA_ARGS: list[str] = [a for a in os.getenv("CLAUDE_EXTRA_ARGS", "").split() if a]
TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "120"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "5"))
MAX_TURNS = int(os.getenv("MAX_TURNS_PER_SESSION", "20"))

# space/user guards — resource names like "spaces/AAAA" / "users/1234", comma-separated
ALLOWED_SPACES: set[str] = {
    s.strip() for s in os.getenv("GCHAT_ALLOWED_SPACES", "").split(",") if s.strip()
}
ALLOWED_USERS: set[str] = {
    u.strip() for u in os.getenv("GCHAT_ALLOWED_USERS", "").split(",") if u.strip()
}

# Session scope: thread | space | auto (default).
# auto = thread-scoped in threaded spaces, space-scoped in DMs/flat spaces
# (flat spaces mint a new thread per message, which would reset the session every turn).
GCHAT_SESSION_SCOPE = os.getenv("GCHAT_SESSION_SCOPE", "auto").strip().lower()
if GCHAT_SESSION_SCOPE not in {"auto", "thread", "space"}:
    sys.exit(f"Invalid GCHAT_SESSION_SCOPE {GCHAT_SESSION_SCOPE!r}: use auto, thread, or space")

# On new sessions, hint the backend to restore prior context via the Google Chat
# MCP server (chatmcp.googleapis.com) if the user has it configured in Claude Code.
# Set to 0 to disable. See docs/gchat-setup.md.
GCHAT_MCP_CONTEXT_HINT = os.getenv("GCHAT_MCP_CONTEXT_HINT", "1") == "1"

# Chat text messages cap at 32,000 bytes; chunk_text counts chars, so stay
# conservative for CJK (3 bytes/char in UTF-8): 8,000 chars ≈ max 24 KB.
GCHAT_CHUNK = int(os.getenv("GCHAT_CHUNK", "8000"))
# Chat API write quota is 1 message/second/space
SEND_INTERVAL = float(os.getenv("GCHAT_SEND_INTERVAL", "1.1"))

_LOG_DIR = os.path.dirname(
    os.path.expanduser(os.getenv("LOG_FILE", "~/gchat-claude-bridge.log"))
)
# distinct filename so a Discord bridge sharing the directory never clobbers it
SESSIONS_FILE = os.getenv("GCHAT_SESSIONS_FILE", os.path.join(_LOG_DIR, "gchat-sessions.json"))

# ── state (same envelope as bot.py, separate file) ───────────────────────────
_sessions, _tc, _ss, _sm = bridge_core.load_sessions(SESSIONS_FILE)
_sessions: dict[str, str] = _sessions          # type: ignore[no-redef]
_turn_counts = defaultdict(int, _tc)
_stopped_sessions: set[str] = _ss
_session_model: dict[str, str] = _sm
del _tc, _ss, _sm

_rate_limiter = bridge_core.RateLimiter(RATE_LIMIT_PER_MIN)
_limit_notified: set[str] = set()

# Pub/Sub is at-least-once: remember recently seen message names (LRU)
_seen_messages: OrderedDict[str, None] = OrderedDict()
_SEEN_MAX = 2048

# per-space send throttle (1 write/s/space quota)
_send_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_last_send: dict[str, float] = {}


def _save_sessions() -> None:
    bridge_core.save_sessions(
        SESSIONS_FILE, _sessions, dict(_turn_counts), _stopped_sessions, _session_model
    )


# ── pure helpers (unit-tested without google deps) ───────────────────────────

def session_key_for(event: dict) -> str:
    """Map a Chat MESSAGE event to a session key.

    thread scope → gth{thread_id} (everyone in the thread shares one session,
    mirroring bot.py's SESSION_SCOPE=thread); space scope → gsp{space_id}.
    """
    space = event.get("space", {})
    space_id = (space.get("name") or "").split("/")[-1]
    thread_name = (event.get("message", {}).get("thread", {}) or {}).get("name", "")
    thread_id = thread_name.split("/")[-1] if thread_name else ""

    scope = GCHAT_SESSION_SCOPE
    if scope == "auto":
        threaded = space.get("spaceThreadingState") == "THREADED_MESSAGES"
        scope = "thread" if threaded else "space"

    if scope == "thread" and thread_id:
        return f"gth{thread_id}"
    return f"gsp{space_id}"


def is_duplicate(message_name: str) -> bool:
    """LRU dedup for Pub/Sub at-least-once redelivery."""
    if not message_name:
        return False
    if message_name in _seen_messages:
        return True
    _seen_messages[message_name] = None
    while len(_seen_messages) > _SEEN_MAX:
        _seen_messages.popitem(last=False)
    return False


def markdown_to_gchat(text: str) -> str:
    """Convert common Markdown (backend output) to Google Chat's own syntax.

    Chat is NOT Markdown: bold is *x*, italic _x_, strikethrough ~x~,
    links are <url|text>. Code fences/inline code pass through untouched
    (Chat supports ``` and `). Single-asterisk MD italic is left as-is —
    it renders as bold in Chat, an acceptable approximation.
    """
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", text)
    for i, part in enumerate(parts):
        if i % 2 == 1:  # code segment — leave untouched
            continue
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", part)                        # bold
        part = re.sub(r"(?<![\w_])__(.+?)__(?![\w_])", r"*\1*", part)         # bold alt
        part = re.sub(r"~~(.+?)~~", r"~\1~", part)                            # strikethrough
        part = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", part)  # links
        part = re.sub(r"^#{1,6}\s+(.+?)\s*$", r"*\1*", part, flags=re.M)      # headings
        parts[i] = part
    return "".join(parts)


def _make_session_instruction(space_name: str, thread_name: str | None) -> str:
    """System hint injected once on the first turn of a new session (mirrors
    bot.py's _make_session_instruction, which uses the discord-context skill).

    Phrased conditionally so it degrades gracefully when the backend has no
    Google Chat MCP server configured.
    """
    if not GCHAT_MCP_CONTEXT_HINT or not space_name:
        return ""
    where = f"space: {space_name}"
    if thread_name:
        where += f", thread: {thread_name}"
    target = thread_name or space_name
    return (
        f"[System: This is a new session bridged from Google Chat ({where}). "
        "If Google Chat MCP tools (list_messages / search_messages / "
        "search_conversations) are available and the task references prior "
        "discussion you lack context for, first restore it by calling "
        f"list_messages for {target}, then respond. "
        "If those tools are unavailable, respond directly. "
        "Never use an MCP send_message tool to reply — the bridge delivers "
        "your response itself.]\n\n"
    )


def extract_prompt(event: dict) -> str:
    """User text with the leading @mention already stripped (argumentText)."""
    msg = event.get("message", {})
    text = msg.get("argumentText") or msg.get("text") or ""
    return text.strip()


# ── Chat API send (lazy google imports so tests run without the packages) ────

_chat_client = None


def _chat():
    global _chat_client
    if _chat_client is None:
        from google.apps import chat_v1 as google_chat
        from google.oauth2.service_account import Credentials

        _chat_client = google_chat.ChatServiceClient(
            credentials=Credentials.from_service_account_file(
                GOOGLE_APPLICATION_CREDENTIALS,
                scopes=["https://www.googleapis.com/auth/chat.bot"],
            )
        )
    return _chat_client


async def send_message(space_name: str, text: str, thread_name: str | None) -> None:
    """Send one message, chunked and throttled to the 1 write/s/space quota."""
    from google.apps import chat_v1 as google_chat

    loop = asyncio.get_running_loop()
    for chunk in bridge_core.chunk_text(markdown_to_gchat(text), GCHAT_CHUNK):
        async with _send_locks[space_name]:
            wait = SEND_INTERVAL - (loop.time() - _last_send.get(space_name, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            req = google_chat.CreateMessageRequest(
                parent=space_name,
                message_reply_option=google_chat.CreateMessageRequest
                    .MessageReplyOption.REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD,
                message=google_chat.Message(
                    text=chunk,
                    thread=google_chat.Thread(name=thread_name) if thread_name else None,
                ),
            )
            await loop.run_in_executor(None, lambda r=req: _chat().create_message(r))
            _last_send[space_name] = loop.time()


# ── backend orchestration (same semantics as bot.run_claude) ─────────────────

async def run_ai(
    prompt: str,
    session_key: str,
    extra_args: list[str] | None = None,
    instruction: str = "",
) -> str:
    session_id = _sessions.get(session_key)

    # Inject the context-restore instruction once on the first turn of a new session
    send_prompt = prompt if session_id else instruction + prompt

    reply = await bridge_core.run_backend(
        send_prompt,
        backend_bin=CLAUDE_BIN,
        base_args=CLAUDE_EXTRA_ARGS,
        model=_session_model.get(session_key),
        extra_args=extra_args or (),
        resume=session_id,
        cwd=WORKING_DIR,
        timeout=TIMEOUT,
    )

    if reply.stale_session:
        _sessions.pop(session_key, None)
        _save_sessions()
        return await run_ai(prompt, session_key, extra_args=extra_args, instruction=instruction)

    if reply.session_id:
        _sessions[session_key] = reply.session_id
        _save_sessions()
    return reply.text


# ── event handling ────────────────────────────────────────────────────────────

async def handle_event(event: dict) -> None:
    if event.get("type") != "MESSAGE":
        return

    space_name = event.get("space", {}).get("name", "")
    user = event.get("user", {})
    user_name = user.get("name", "")
    msg = event.get("message", {})
    thread_name = (msg.get("thread") or {}).get("name")

    if is_duplicate(msg.get("name", "")):
        return
    if ALLOWED_SPACES and space_name not in ALLOWED_SPACES:
        log.warning(f"Blocked space {space_name}")
        return
    if ALLOWED_USERS and user_name not in ALLOWED_USERS:
        log.warning(f"Blocked user {user_name} in {space_name}")
        return

    content = extract_prompt(event)
    if not content:
        return

    session_key = session_key_for(event)

    if content == "!reset":
        _sessions.pop(session_key, None)
        _turn_counts.pop(session_key, None)
        _stopped_sessions.discard(session_key)
        _limit_notified.discard(session_key)
        _save_sessions()
        await send_message(space_name, "Session cleared.", thread_name)
        return

    if content == "!status":
        session_id = _sessions.get(session_key)
        shared = "共享（同 thread/space 所有人同一對話）"
        lines = [
            "*Session 狀態*",
            f"- session key: `{session_key}`（scope: `{GCHAT_SESSION_SCOPE}`，{shared}）",
            f"- 後端 session: `{session_id[:8] + '…' if session_id else '尚未建立'}`",
            f"- 輪數: {_turn_counts.get(session_key, 0)}/{MAX_TURNS}",
            f"- 模型: `{_session_model.get(session_key, 'default')}`",
        ]
        await send_message(space_name, "\n".join(lines), thread_name)
        return

    if content == "!help":
        lines = [
            "*Google Chat ↔ AI Bridge 指令*",
            "`!reset` — 清除 session，重新開始",
            "`!status` — 顯示目前 session 狀態",
            "",
            "*工作模式*（`!cmd <task>`）",
        ]
        lines += [f"`{spec['help']}`" for spec in bridge_core.CMD_MAP.values()]
        await send_message(space_name, "\n".join(lines), thread_name)
        return

    if _rate_limiter.is_limited(user_name):
        await send_message(
            space_name, f"Rate limit: max {RATE_LIMIT_PER_MIN} requests/min.", thread_name
        )
        return

    _turn_counts[session_key] += 1
    current_turn = _turn_counts[session_key]
    if current_turn > MAX_TURNS:
        if session_key not in _limit_notified:
            _limit_notified.add(session_key)
            await send_message(
                space_name,
                f"本對話已達上限 {MAX_TURNS} 輪，訊息不會再轉給後端。輸入 `!reset` 開啟新 session。",
                thread_name,
            )
        return

    prompt, extra_args = bridge_core.parse_command(content)

    # shared sessions: tag the speaker so the backend can tell participants apart
    speaker = user.get("displayName") or user_name
    if speaker and not content.startswith("!"):
        prompt = f"[{speaker}] {prompt}"

    log.info(f"Request from {speaker} [{session_key}] turn {current_turn}/{MAX_TURNS}: {content[:80]}")
    reply = await run_ai(
        prompt,
        session_key,
        extra_args=extra_args,
        instruction=_make_session_instruction(space_name, thread_name),
    )
    await send_message(space_name, reply, thread_name)


# ── Pub/Sub pull loop ─────────────────────────────────────────────────────────

async def main() -> None:
    if not GCHAT_PROJECT_ID or not GCHAT_SUBSCRIPTION_ID:
        sys.exit("GCHAT_PROJECT_ID / GCHAT_SUBSCRIPTION_ID are not set")
    if not GOOGLE_APPLICATION_CREDENTIALS:
        sys.exit("GOOGLE_APPLICATION_CREDENTIALS is not set")
    # same guard as bot.py: unattended permissions need an explicit user allowlist
    if "--dangerously-skip-permissions" in CLAUDE_EXTRA_ARGS and not ALLOWED_USERS:
        if os.getenv("UNSAFE_ALLOW_ALL_USERS") != "1":
            sys.exit(
                "Refusing to start: --dangerously-skip-permissions is set but "
                "GCHAT_ALLOWED_USERS is empty. Set it, or UNSAFE_ALLOW_ALL_USERS=1."
            )
        log.warning("UNSAFE_ALLOW_ALL_USERS=1: skip-permissions with no user allowlist!")

    from google.cloud import pubsub_v1

    loop = asyncio.get_running_loop()

    def callback(pubsub_msg) -> None:
        # ack immediately: Claude runs regularly exceed the ack deadline, and
        # redelivery would double-trigger the backend; dedup is belt-and-braces
        try:
            event = json.loads(pubsub_msg.data.decode("utf-8"))
        except Exception as e:
            log.warning(f"Undecodable Pub/Sub message dropped: {e}")
            pubsub_msg.ack()
            return
        pubsub_msg.ack()
        asyncio.run_coroutine_threadsafe(handle_event(event), loop)

    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(GCHAT_PROJECT_ID, GCHAT_SUBSCRIPTION_ID)
    streaming_pull = subscriber.subscribe(subscription_path, callback=callback)
    log.info(f"Listening on {subscription_path} (backend: {CLAUDE_BIN})")
    log.info(f"Allowed spaces: {ALLOWED_SPACES or 'ALL (danger!)'}")
    log.info(f"Allowed users:  {ALLOWED_USERS or 'ALL (danger!)'}")

    try:
        await loop.run_in_executor(None, streaming_pull.result)
    except (KeyboardInterrupt, asyncio.CancelledError):
        streaming_pull.cancel()


if __name__ == "__main__":
    asyncio.run(main())
