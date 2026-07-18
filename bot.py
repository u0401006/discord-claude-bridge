"""
Discord ↔ Claude Code Bridge
每則訊息透過 `claude --print` subprocess 送進 Claude Code，結果回傳 Discord。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
import unicodedata
import uuid
from collections import defaultdict

import discord
from dotenv import load_dotenv

import bridge_core

# 支援 --env /path/to/.env，讓多個 bot 實例共用同一份 bot.py
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
            os.path.expanduser(os.getenv("LOG_FILE", "~/discord-claude-bridge.log"))
        ),
    ],
)
log = logging.getLogger(__name__)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ALLOWED_CHANNEL_IDS: set[int] = {
    int(x) for x in os.getenv("ALLOWED_CHANNEL_IDS", "").split(",") if x.strip()
}
ALLOWED_USER_IDS: set[int] = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
BLOCKED_CHANNEL_IDS: set[int] = {
    int(x) for x in os.getenv("BLOCKED_CHANNEL_IDS", "").split(",") if x.strip()
}
BLOCKED_USER_IDS: set[int] = {
    int(x) for x in os.getenv("BLOCKED_USER_IDS", "").split(",") if x.strip()
}
# Webhook/bot user IDs that bypass _stopped_sessions and auto-reset on each new task.
# Add webhook user IDs here to allow task dispatchers to re-trigger Claude after [DONE].
WEBHOOK_PASSTHROUGH_IDS: set[int] = {
    int(x) for x in os.getenv("WEBHOOK_PASSTHROUGH_IDS", "").split(",") if x.strip()
}
WORKING_DIR = os.path.expanduser(os.getenv("WORKING_DIR", "~/agent-dev"))
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
# Extra args prepended to every CLAUDE_BIN call.
# Claude Code unattended mode: set --dangerously-skip-permissions explicitly in .env.
# OpenAI adapter: leave empty or set --model gpt-4o
CLAUDE_EXTRA_ARGS: list[str] = [
    a for a in os.getenv("CLAUDE_EXTRA_ARGS", "").split() if a
]
# Session scope: how a Discord message maps to a Claude session.
#   thread  (default) — inside a thread everyone shares one session (th{thread_id});
#                       top-level channel messages stay per-user (ch{id}_u{uid})
#   channel           — everyone in a channel shares one session (ch{id})
#   user              — per channel/user pair (legacy behaviour)
SESSION_SCOPE = os.getenv("SESSION_SCOPE", "thread").strip().lower()
if SESSION_SCOPE not in {"thread", "channel", "user"}:
    sys.exit(f"Invalid SESSION_SCOPE {SESSION_SCOPE!r}: use thread, channel, or user")
TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "120"))  # seconds
# Credential isolation: backend subprocesses get a filtered environment —
# frontend secrets (DISCORD_TOKEN etc.) are never printenv-able by the agent.
# BACKEND_ENV_PASS=OPENAI_API_KEY re-allows a var (needed for openai-adapter).
_BACKEND_ENV = bridge_core.build_backend_env(
    extra_deny=os.getenv("BACKEND_ENV_DENY", ""),
    extra_pass=os.getenv("BACKEND_ENV_PASS", ""),
)
# [[ws:path]] directive may point the session's working dir anywhere under
# these roots (colon-separated; default: the user's home, like OpenAB)
WS_ALLOWED_DIRS: list[str] = [
    d for d in os.getenv("WS_ALLOWED_DIRS", "~").split(":") if d.strip()
]
# Live progress: stream tool activity into an auto-edited Discord message
# (uses --output-format stream-json; works with the claude CLI and with the
# bundled adapters — adapters just show no intermediate steps). Default off.
STREAM_PROGRESS = os.getenv("STREAM_PROGRESS", "0") == "1"
PROGRESS_EDIT_INTERVAL = float(os.getenv("PROGRESS_EDIT_INTERVAL", "2.0"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "5"))
MAX_TURNS = int(os.getenv("MAX_TURNS_PER_SESSION", "20"))
TASK_STATE_INTERVAL = int(os.getenv("TASK_STATE_INTERVAL", "5"))  # 0 = disabled
DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "2.5"))
STREAM_HOLD_SIGNAL = os.getenv("STREAM_HOLD_SIGNAL", "[未完]")
DISCORD_CHUNK = 1900  # Discord limit is 2000; leave room for code fences

# SEND_FILE: colon-separated list of allowed base dirs (realpath).  Default: WORKING_DIR only.
# Set SEND_FILE_ALLOWED_DIRS=/path/a:/path/b to expand. Empty = use WORKING_DIR.
_SEND_FILE_DIRS: list[str] = [
    os.path.realpath(os.path.expanduser(d))
    for d in os.getenv("SEND_FILE_ALLOWED_DIRS", "").split(":")
    if d.strip()
]
# Comma-separated allowed extensions (including leading dot).
_SEND_FILE_EXTS: frozenset[str] = frozenset(
    e.strip().lower()
    for e in os.getenv(
        "SEND_FILE_ALLOWED_EXTS",
        ".png,.jpg,.jpeg,.gif,.webp,.pdf,.txt,.csv,.json,.md,.log",
    ).split(",")
    if e.strip()
)

# Same default as the logging handler above, so sessions.json/memory live next to the log
_LOG_DIR = os.path.dirname(
    os.path.expanduser(os.getenv("LOG_FILE", "~/discord-claude-bridge.log"))
)
SESSIONS_FILE = os.path.join(_LOG_DIR, "sessions.json")
MEMORY_DIR = os.path.join(_LOG_DIR, "memory")

# attachment handling: dedicated dir + size cap (bytes)
ATTACH_DIR = os.path.expanduser(os.getenv("ATTACH_DIR", "/tmp/discord-attachments"))
ATTACH_MAX_BYTES = int(os.getenv("ATTACH_MAX_BYTES", str(8 * 1024 * 1024)))

# turn counter / stopped sessions — initialized from disk below (see _load_sessions)
_turn_counts: dict[str, int] = defaultdict(int)
_stopped_sessions: set[str] = set()

# debounce: buffer incoming messages, cancel+restart timer on each new message
_pending: dict[str, asyncio.Task] = {}
_pending_texts: dict[str, list[str]] = defaultdict(list)

# thread redirect: session_key → channel to reply to (updated by on_thread_create)
_pending_channel: dict[str, discord.abc.Messageable] = {}
# last buffering author per session — used to redirect only the thread creator's session
_pending_author: dict[str, int] = {}

# sessions already told they hit MAX_TURNS (avoid repeating the notice every message)
_limit_notified: set[str] = set()

# token Claude uses to signal conversation end
_DONE_SIGNAL = "[DONE]"

# marker for shared cross-bot task state posted in Discord threads
_TASK_STATE_MARKER = "📋 [TASK STATE]"

# per-session model override: session_key → model name
_session_model: dict[str, str] = {}

# ── CLI command mapping — shared with all frontends (see bridge_core.CMD_MAP) ─
_CMD_MAP: dict[str, dict] = bridge_core.CMD_MAP

# Direct CLI commands: these bypass run_claude and call claude subcommands directly.
# "!discord_cmd" → {"cli": ["subcommand", ...], "help": str}
_DIRECT_CMD_MAP: dict[str, dict] = {
    "!ultrareview": {
        "cli": ["ultrareview"],
        "help": "!ultrareview [PR號 or branch] — 多 agent code review（cloud-hosted）",
    },
    "!ultrawork": {
        "cli": ["ultrawork"],
        "help": "!ultrawork <task> — 多 agent 並行拆解任務（需 Workflows flag）",
    },
    "!ultracode": {
        "cli": ["ultracode"],
        "help": "!ultracode <task> — 多 agent 並行寫 code（需 Workflows flag）",
    },
}

# model aliases for !model command
_MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "fable": "claude-fable-5",
}


async def _run_direct_cmd(
    cmd_args: list[str], timeout: int = 600
) -> str:
    """Run a claude subcommand directly (not --print). Returns stdout or error."""
    args = [CLAUDE_BIN] + cmd_args
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=WORKING_DIR,
            env=_BACKEND_ENV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Timeout after {timeout}s"
        if proc.returncode != 0:
            err = stderr.decode().strip()
            return f"```\nError (exit {proc.returncode}):\n{err[:1800]}\n```"
        return stdout.decode().strip() or "(no output)"
    except Exception as e:
        return f"Bridge error: {e}"


_parse_command = bridge_core.parse_command


def _session_key_for(channel, user_id: int) -> str:
    """Map a Discord channel/author pair to a session key according to SESSION_SCOPE.

    Threads are detected via parent_id (threads have one, regular channels don't).
    """
    ch_id = getattr(channel, "id", 0)
    parent_id = getattr(channel, "parent_id", None)
    if SESSION_SCOPE == "thread":
        if parent_id:
            return f"th{ch_id}"
        return f"ch{ch_id}_u{user_id}"
    if SESSION_SCOPE == "channel":
        return f"ch{parent_id or ch_id}"
    return f"ch{ch_id}_u{user_id}"


def _is_shared_scope(session_key: str) -> bool:
    """True if multiple users share this session (no per-user suffix)."""
    return "_u" not in session_key


# silent-ack patterns: any message whose stripped content matches one of these
# is dropped without calling Claude (covers ASCII and CJK punctuation)
_ACK_CONTENT: frozenset[str] = frozenset({".", "。", "·", "…", "...", "、"})


def _is_punct_only(text: str) -> bool:
    """Return True if every character in text is punctuation, symbol, or whitespace."""
    return bool(text) and all(
        unicodedata.category(c)[0] in {"P", "S", "Z"} or c.isspace()
        for c in text
    )

# system instruction injected once at the start of every new session
def _make_session_instruction(session_key: str) -> str:
    """Build system instructions for a new Claude session (B3: memory hint, B2: discord-context)."""
    # Extract channel/thread id from session_key formats: ch{id}, th{id}, ch{id}_u{uid}
    m = re.match(r"^(?:ch|th)(\d+)", session_key)
    channel_id = m.group(1) if m else ""

    done_rule = (
        f"[System: When you consider this conversation complete or the task fully done, "
        f"append exactly `{_DONE_SIGNAL}` on its own line at the very end of your response. "
        "The bridge will then stop forwarding further bot messages to you.]"
    )

    # B3: instruct Claude to read memory files at session start
    memory_hint = (
        "[System: This is a new session. Before responding to the first task, "
        "read your MEMORY.md index, then Read any memory files relevant to the request.]"
    )

    parts = [done_rule, memory_hint]

    # B2: inject channel_id so Claude can restore Discord thread context via skill
    if channel_id:
        ctx_hint = (
            f"[System: This is a Discord bridge session (channel {channel_id}). "
            "If the task references prior discussions you lack context for, restore it first:\n"
            f"python3 ~/.claude/skills/discord-context/scripts/fetch_thread.py {channel_id}]"
        )
        parts.append(ctx_hint)

    return "\n\n".join(parts) + "\n\n"


def _load_sessions() -> tuple[dict[str, str], dict[str, int], set[str], dict[str, str]]:
    # module-level SESSIONS_FILE is read at call time (tests patch it)
    return bridge_core.load_sessions(SESSIONS_FILE)


def _save_sessions() -> None:
    bridge_core.save_sessions(
        SESSIONS_FILE, _sessions, dict(_turn_counts), _stopped_sessions,
        _session_model, _session_workdir,
    )


# per-session working directory override (set via [[ws:path]] directive)
_session_workdir: dict[str, str] = {}

# session tracker — all five structures persisted together across restarts
_sessions, _tc, _ss, _sm, _swd = _load_sessions()
_sessions: dict[str, str] = _sessions          # type: ignore[no-redef]
_turn_counts = defaultdict(int, _tc)
_stopped_sessions: set[str] = _ss
_session_model.update(_sm)
_session_workdir.update(_swd)
del _tc, _ss, _sm, _swd


_rate_limiter = bridge_core.RateLimiter(RATE_LIMIT_PER_MIN)


def is_rate_limited(user_id: int) -> bool:
    return _rate_limiter.is_limited(user_id)


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# ── helpers ──────────────────────────────────────────────────────────────────
# fence-aware chunking lives in bridge_core (shared by all frontends)
_scan_fence = bridge_core.scan_fence


def chunk_text(text: str, limit: int = DISCORD_CHUNK) -> list[str]:
    """Split text into Discord-safe chunks, closing/reopening code fences at boundaries."""
    return bridge_core.chunk_text(text, limit)


def _should_write_interim_task_state(turn_count: int, interval: int) -> bool:
    """Return True if an interim TASK STATE should be written at this turn."""
    if interval <= 0 or turn_count <= 0:
        return False
    return turn_count % interval == 0


async def fetch_task_state(channel: discord.abc.Messageable, max_merge: int = 3) -> str | None:
    """Search recent 50 messages for TASK STATE markers; merge up to max_merge of them."""
    found: list[str] = []
    try:
        async for msg in channel.history(limit=50):  # type: ignore[union-attr]
            # only trust task states authored by bots — humans could spoof the
            # marker to inject content into other users' new sessions
            if msg.author.bot and msg.content.startswith(_TASK_STATE_MARKER):
                found.append(msg.content)
                if len(found) >= max_merge:
                    break
    except Exception as e:
        log.warning(f"fetch_task_state failed: {e}")
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return "\n\n---\n".join(found)


async def write_task_state(channel: discord.abc.Messageable, bot_name: str, summary: str) -> None:
    """Post a structured TASK STATE message to the channel."""
    content = f"{_TASK_STATE_MARKER}\nupdated_by: {bot_name}\n\n{summary}"
    try:
        await channel.send(content[:1900])  # type: ignore[union-attr]
        log.info(f"Task state written to channel by {bot_name}")
    except Exception as e:
        log.warning(f"write_task_state failed: {e}")


def _attachment_path(filename: str) -> str:
    """Build a collision-free save path inside ATTACH_DIR for an uploaded attachment.

    The client-supplied filename is reduced to its basename (no path components can
    escape ATTACH_DIR) and prefixed with a random token so concurrent users can
    never overwrite each other's uploads.
    """
    safe = os.path.basename(filename.replace("\\", "/")).strip() or "attachment"
    return os.path.join(ATTACH_DIR, f"{uuid.uuid4().hex[:8]}_{safe}")


def _validate_send_file(path: str) -> str | None:
    """Return realpath if the file is in an allowed dir with an allowed extension; else None."""
    try:
        real = os.path.realpath(os.path.expanduser(path.strip()))
    except Exception:
        return None

    allowed_dirs = _SEND_FILE_DIRS or [os.path.realpath(WORKING_DIR)]
    if not any(
        real == d or real.startswith(d + os.sep) for d in allowed_dirs
    ):
        log.warning("SEND_FILE rejected (outside allowed dirs): %r", path)
        return None

    _, ext = os.path.splitext(real)
    if _SEND_FILE_EXTS and ext.lower() not in _SEND_FILE_EXTS:
        log.warning("SEND_FILE rejected (extension not allowed): %r", path)
        return None

    if not os.path.isfile(real):
        log.warning("SEND_FILE rejected (not a regular file): %r", path)
        return None

    return real


class _ProgressReporter:
    """Streams backend tool activity into one auto-edited Discord message."""

    def __init__(self, channel: discord.abc.Messageable, max_lines: int = 6):
        self.channel = channel
        self.max_lines = max_lines
        self.msg: discord.Message | None = None
        self.steps: list[str] = []
        self.count = 0
        self.start = time.monotonic()
        self._last_edit = 0.0

    def _render(self, header: str) -> str:
        lines = "\n".join(f"▸ {s}" for s in self.steps[-self.max_lines:])
        text = f"{header}（{self.count} 步，{int(time.monotonic() - self.start)}s）"
        if lines:
            text += f"\n```\n{lines}\n```"
        return text[:1900]

    async def on_step(self, desc: str) -> None:
        self.count += 1
        self.steps.append(desc)
        now = time.monotonic()
        try:
            if self.msg is None:
                self.msg = await self.channel.send(self._render("⏳ 執行中"))  # type: ignore[union-attr]
                self._last_edit = now
            elif now - self._last_edit >= PROGRESS_EDIT_INTERVAL:
                await self.msg.edit(content=self._render("⏳ 執行中"))
                self._last_edit = now
        except Exception as e:  # progress UI must never break the run
            log.debug(f"progress update failed: {e}")

    async def finish(self, cancelled: bool = False) -> None:
        if self.msg is None:
            return
        header = "⛔ 已取消" if cancelled else "✅ 完成"
        try:
            elapsed = int(time.monotonic() - self.start)
            await self.msg.edit(content=f"{header}（{self.count} 步，{elapsed}s）")
        except Exception as e:
            log.debug(f"progress finish failed: {e}")


async def run_claude(
    prompt: str,
    session_key: str,
    extra_args: list[str] | None = None,
    on_progress=None,
) -> str:
    """Returns the backend reply text; empty string means the run was cancelled."""
    session_id = _sessions.get(session_key)

    # Inject exit-signal instruction once on the first turn of a new session
    send_prompt = prompt if session_id else _make_session_instruction(session_key) + prompt

    kwargs = dict(
        backend_bin=CLAUDE_BIN,
        base_args=CLAUDE_EXTRA_ARGS,
        model=_session_model.get(session_key),
        extra_args=extra_args or (),
        resume=session_id,
        cwd=_session_workdir.get(session_key, WORKING_DIR),
        timeout=TIMEOUT,
        proc_key=session_key,
        env=_BACKEND_ENV,
    )
    if on_progress is not None:
        reply = await bridge_core.run_backend_streaming(
            send_prompt, on_progress=on_progress, **kwargs
        )
    else:
        reply = await bridge_core.run_backend(send_prompt, **kwargs)

    if reply.cancelled:
        return ""

    # 失效 session：清除舊 ID 並重試一次（不帶 --resume，重新注入 instruction）
    if reply.stale_session:
        _sessions.pop(session_key, None)
        _save_sessions()
        return await run_claude(prompt, session_key, extra_args=extra_args, on_progress=on_progress)

    if reply.session_id:
        _sessions[session_key] = reply.session_id
        _save_sessions()
    return reply.text


async def _flush(
    session_key: str,
    channel: discord.abc.Messageable,
    author: discord.abc.User,
    is_bot_author: bool,
) -> None:
    """Debounce timer: fires after DEBOUNCE_SECONDS of silence, sends combined prompt."""
    await asyncio.sleep(DEBOUNCE_SECONDS)
    # Use thread-redirected channel if on_thread_create updated it
    channel = _pending_channel.pop(session_key, channel)  # type: ignore[assignment]

    combined = "\n".join(_pending_texts.pop(session_key, []))
    _pending.pop(session_key, None)
    _pending_author.pop(session_key, None)

    if not combined:
        return

    # [DONE] only blocks bot follow-ups; humans can always continue
    if session_key in _stopped_sessions and is_bot_author:
        return

    # parse !cmd prefixes from the first line (human messages only)
    extra_args: list[str] = []
    if not is_bot_author:
        combined, extra_args = _parse_command(combined)

    # On new session: fetch shared task state from thread and prepend to prompt
    is_new_session = session_key not in _sessions
    if is_new_session:
        task_state = await fetch_task_state(channel)
        if task_state:
            combined = f"[Shared task state from this thread]\n{task_state}\n\n[New message]\n{combined}"
            log.info(f"Task state injected for new session {session_key}")

    _turn_counts[session_key] += 1
    current_turn = _turn_counts[session_key]

    if current_turn > MAX_TURNS:
        # tell the user once instead of silently swallowing their messages
        if session_key not in _limit_notified and not is_bot_author:
            _limit_notified.add(session_key)
            await channel.send(  # type: ignore[union-attr]
                f"{author.mention} 本對話已達上限 {MAX_TURNS} 輪，訊息不會再轉給 Claude。"
                "輸入 `!reset` 開啟新 session。"
            )
        return

    log.info(
        f"Request from {author} ({author.id}) [{session_key}] "
        f"turn {current_turn}/{MAX_TURNS}: {combined[:80]}"
    )

    async with channel.typing():  # type: ignore[arg-type]
        if STREAM_PROGRESS and not is_bot_author:
            reporter = _ProgressReporter(channel)
            reply = await run_claude(
                combined, session_key, extra_args=extra_args, on_progress=reporter.on_step
            )
            await reporter.finish(cancelled=(reply == ""))
        else:
            reply = await run_claude(combined, session_key, extra_args=extra_args)

    # cancelled via !cancel — the cancel handler already confirmed to the user
    if reply == "":
        return

    # Detect Claude's self-exit signal
    if _DONE_SIGNAL in reply:
        reply = reply.replace(_DONE_SIGNAL, "").rstrip()
        _stopped_sessions.add(session_key)
        _save_sessions()
        log.info(f"Claude signaled [DONE] for {session_key}")
        if not is_bot_author:
            reply += "\n\n---\n> Claude 已結束此對話。Bot 訊息將被擋下；你仍可繼續說話，或以 `!flush` 加入記憶，以 `!reset` 開啟新 session。"
        # Write shared task state so other bots in this thread can pick up context
        state_prompt = (
            "請用以下 YAML 格式，產出本次對話的任務狀態摘要（100字以內）：\n"
            "decided: <已決定的事項，若無填 none>\n"
            "open_questions: <未決定的問題，若無填 none>\n"
            "next_action: <下一步行動，若無填 none>\n"
            "只輸出 YAML 內容，不加任何說明或 markdown 包裝。"
        )
        async with channel.typing():  # type: ignore[arg-type]
            state_summary = await run_claude(state_prompt, session_key)
        bot_name = str(client.user) if client.user else "bot"
        await write_task_state(channel, bot_name, state_summary)
    elif is_bot_author and _is_punct_only(reply):
        # Claude returned pure punctuation in a bot-to-bot turn → auto-stop to break loop
        _stopped_sessions.add(session_key)
        _save_sessions()
        log.info(f"Claude punct-only reply in bot session, auto-stop for {session_key}")
        return  # send nothing; silence breaks the ping-pong
    elif _should_write_interim_task_state(current_turn, TASK_STATE_INTERVAL):
        # Idea 3: write interim TASK STATE every N turns so other users get fresh context
        state_prompt = (
            "請用以下 YAML 格式，產出本次對話的任務狀態摘要（100字以內）：\n"
            "decided: <已決定的事項，若無填 none>\n"
            "open_questions: <未決定的問題，若無填 none>\n"
            "next_action: <下一步行動，若無填 none>\n"
            "只輸出 YAML 內容，不加任何說明或 markdown 包裝。"
        )
        async with channel.typing():  # type: ignore[arg-type]
            state_summary = await run_claude(state_prompt, session_key)
        bot_name = str(client.user) if client.user else "bot"
        await write_task_state(channel, bot_name, state_summary)
        log.info(f"Interim task state written at turn {current_turn} for {session_key}")
    elif current_turn == MAX_TURNS:
        reply += f"\n\n---\n> 本對話已達上限 {MAX_TURNS} 輪，請輸入 `!reset` 重啟新的 session。"

    # Extract [SEND_FILE:/path] tokens before sending; validate each path
    raw_paths = re.findall(r'\[SEND_FILE:([^\]]+)\]', reply)
    reply = re.sub(r'\[SEND_FILE:[^\]]+\]', '', reply).strip()
    file_paths: list[str] = []
    for rp in raw_paths:
        safe = _validate_send_file(rp)
        if safe:
            file_paths.append(safe)
        else:
            log.warning("SEND_FILE path rejected, skipping: %r", rp)

    log.info(f"Response length: {len(reply)} chars, files: {len(file_paths)}")
    mention = author.mention
    if reply:
        chunks = chunk_text(reply)
        for i, chunk in enumerate(chunks):
            text = f"{mention} {chunk}" if i == 0 else chunk
            if i < len(chunks) - 1 and STREAM_HOLD_SIGNAL:
                text = text.rstrip() + f"\n{STREAM_HOLD_SIGNAL}"
            await channel.send(text)  # type: ignore[union-attr]
        for path in file_paths:
            try:
                await channel.send(file=discord.File(path))  # type: ignore[union-attr]
            except Exception as e:
                await channel.send(f"{author.mention} [檔案傳送失敗: {path}] {e}")  # type: ignore[union-attr]
    else:
        for i, path in enumerate(file_paths):
            try:
                content = mention if i == 0 else None
                await channel.send(content=content, file=discord.File(path))  # type: ignore[union-attr]
            except Exception as e:
                pfx = mention if i == 0 else ""
                await channel.send(f"{pfx} [檔案傳送失敗: {path}] {e}")  # type: ignore[union-attr]


# ── event handlers ────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    log.info(f"Logged in as {client.user}")
    log.info(f"Allowed channels: {ALLOWED_CHANNEL_IDS or 'ALL (danger!)'}")
    log.info(f"Allowed users:   {ALLOWED_USER_IDS or 'ALL (danger!)'}")
    log.info(f"Blocked channels: {BLOCKED_CHANNEL_IDS or 'none'}")
    log.info(f"Blocked users:   {BLOCKED_USER_IDS or 'none'}")
    log.info(f"Working dir:     {WORKING_DIR}")


@client.event
async def on_thread_create(thread: discord.Thread) -> None:
    """Redirect pending session replies into a newly created thread.

    Only the thread creator's own pending session is redirected — otherwise a
    thread created by user A would hijack user B's in-flight reply.
    """
    owner_id = getattr(thread, "owner_id", None)
    if owner_id is None:
        return
    for sk, ch in list(_pending_channel.items()):
        if (
            getattr(ch, "id", None) == thread.parent_id
            and _pending_author.get(sk) == owner_id
        ):
            _pending_channel[sk] = thread
            log.info(f"Thread redirect: {sk} → thread {thread.id} ({thread.name!r})")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    # ignore system messages (thread_created, pins, joins, etc.)
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return

    # channel guard: support threads by checking parent_id against ALLOWED/BLOCKED lists
    ch_id = getattr(message.channel, "parent_id", None) or message.channel.id
    if BLOCKED_CHANNEL_IDS and ch_id in BLOCKED_CHANNEL_IDS:
        return
    if ALLOWED_CHANNEL_IDS and ch_id not in ALLOWED_CHANNEL_IDS:
        return

    # user guard
    if BLOCKED_USER_IDS and message.author.id in BLOCKED_USER_IDS:
        log.warning(f"Blocked user {message.author} ({message.author.id})")
        return
    if ALLOWED_USER_IDS and message.author.id not in ALLOWED_USER_IDS:
        log.warning(f"Blocked user {message.author} ({message.author.id})")
        return

    # ignore if someone/some role is mentioned but not the bot
    # exception: trusted users (in ALLOWED_USER_IDS) may mention anyone freely
    is_trusted = bool(ALLOWED_USER_IDS) and message.author.id in ALLOWED_USER_IDS
    if not is_trusted and (message.mentions or message.role_mentions) and client.user not in message.mentions:
        return

    # rate limit
    if is_rate_limited(message.author.id):
        await message.channel.send(f"{message.author.mention} Rate limit: max {RATE_LIMIT_PER_MIN} requests/min.")
        return

    content = message.content.strip()

    # silently drop task state announcements — bots read them via fetch_task_state(), not on_message
    if content.startswith(_TASK_STATE_MARKER):
        return

    # Handle file attachments: download into ATTACH_DIR; append text content for .txt files
    IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    TEXT_EXTS  = {'.txt'}
    attachment_texts = []
    if message.attachments:
        import aiohttp
        os.makedirs(ATTACH_DIR, exist_ok=True)
        for att in message.attachments:
            if att.size and att.size > ATTACH_MAX_BYTES:
                attachment_texts.append(
                    f"\n\n[附件 {att.filename} 超過大小上限 "
                    f"{ATTACH_MAX_BYTES // (1024 * 1024)}MB，已略過]"
                )
                continue
            ext = '.' + att.filename.rsplit('.', 1)[-1].lower() if '.' in att.filename else ''
            tmp_path = _attachment_path(att.filename)
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.get(att.url) as resp:
                        raw = await resp.read()
                        with open(tmp_path, 'wb') as f:
                            f.write(raw)
                if ext in IMAGE_EXTS:
                    attachment_texts.append(f"\n\n[圖片附件：{att.filename}，已存至 {tmp_path}，可直接 Read 該檔案]")
                elif ext in TEXT_EXTS:
                    text = raw.decode('utf-8', errors='replace')
                    attachment_texts.append(
                        f"\n\n[附件：{att.filename}，已存至 {tmp_path}]\n{text}"
                    )
                else:
                    attachment_texts.append(f"\n\n[附件：{att.filename}，已存至 {tmp_path}]")
            except Exception as e:
                attachment_texts.append(f"\n\n[附件 {att.filename} 讀取失敗：{e}]")
    if attachment_texts:
        content = content + "".join(attachment_texts)

    if not content:
        return

    content = re.sub(r'^(<@!?\d+>\s*)+', '', content).strip()

    session_key = _session_key_for(message.channel, message.author.id)

    # [[ws:path]] control directive: set this session's working directory
    content, ws_path = bridge_core.parse_ws_directive(content)
    if ws_path is not None:
        real = bridge_core.validate_workdir(ws_path, WS_ALLOWED_DIRS)
        if real is None:
            await message.channel.send(
                f"{message.author.mention} 無效的工作目錄 `{ws_path}`"
                "（必須是存在的目錄，且位於允許範圍內）"
            )
            return
        _session_workdir[session_key] = real
        _save_sessions()
        log.info(f"Workdir for {session_key}: {real}")
        await message.channel.send(f"{message.author.mention} 工作目錄已設為 `{real}`")
        if not content:
            return  # directive-only message

    if content == "!reset":
        if session_key in _pending:
            _pending.pop(session_key).cancel()
        _pending_texts.pop(session_key, None)
        _pending_author.pop(session_key, None)
        _sessions.pop(session_key, None)
        _turn_counts.pop(session_key, None)
        _stopped_sessions.discard(session_key)
        _limit_notified.discard(session_key)
        _session_workdir.pop(session_key, None)
        _save_sessions()
        log.info(f"Session reset for {session_key}")
        await message.channel.send(f"{message.author.mention} Session cleared.")
        return

    if content == "!cancel":
        # drop any buffered-but-unsent messages first
        if session_key in _pending:
            _pending.pop(session_key).cancel()
        _pending_texts.pop(session_key, None)
        _pending_author.pop(session_key, None)
        if bridge_core.cancel_backend(session_key):
            log.info(f"Cancelled in-flight backend run for {session_key}")
            await message.channel.send(f"{message.author.mention} ⛔ 已中斷目前執行。session 保留，可直接繼續對話。")
        else:
            await message.channel.send(f"{message.author.mention} 沒有進行中的任務。")
        return

    if content == "!stop":
        if session_key in _pending:
            _pending.pop(session_key).cancel()
        _pending_texts.pop(session_key, None)
        _stopped_sessions.add(session_key)
        _save_sessions()
        log.info(f"Session force-stopped for {session_key}")
        await message.channel.send(f"{message.author.mention} Session stopped. 輸入 `!flush` 存入記憶，或 `!reset` 開啟新 session。")
        return

    if content == "!flush":
        session_id = _sessions.get(session_key)
        if not session_id:
            await message.channel.send(f"{message.author.mention} 沒有可 flush 的 session。")
            return
        await message.channel.send(f"{message.author.mention} 正在壓縮對話記憶…")
        summary_prompt = (
            "請將本次對話的關鍵 findings、決策與結論，濃縮成 300 字以內的 Markdown 摘要，"
            "供後續 agent 在同一 thread 繼續工作時參考。只輸出摘要本文，不加任何說明或前言。"
        )
        async with message.channel.typing():
            summary = await run_claude(summary_prompt, session_key)
        ch_id = str(getattr(message.channel, "parent_id", None) or message.channel.id)
        thread_id = str(message.channel.id)
        memory_dir = os.path.join(MEMORY_DIR, ch_id, thread_id)
        os.makedirs(memory_dir, exist_ok=True)
        memory_path = os.path.join(memory_dir, "context.md")
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(memory_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n---\n<!-- flushed at {timestamp} by {message.author} -->\n\n{summary}\n")
        log.info(f"Flushed session context for {session_key} → {memory_path}")
        await message.channel.send(f"{message.author.mention} 已存入 `{memory_path}`")
        return

    if content.startswith("!model"):
        arg = content[len("!model"):].strip()
        if not arg:
            current = _session_model.get(session_key, "default")
            aliases = ", ".join(f"`{k}` → `{v}`" for k, v in _MODEL_ALIASES.items())
            await message.channel.send(
                f"{message.author.mention} 目前模型：`{current}`\n"
                f"用法：`!model <name>` — 可用別名：{aliases}\n"
                "`!model reset` 恢復預設"
            )
            return
        if arg == "reset":
            _session_model.pop(session_key, None)
            _save_sessions()
            await message.channel.send(f"{message.author.mention} 已恢復預設模型。")
            return
        resolved = _MODEL_ALIASES.get(arg.lower())
        if not resolved:
            aliases = ", ".join(f"`{k}`" for k in _MODEL_ALIASES)
            await message.channel.send(
                f"{message.author.mention} 未知的模型 `{arg}`。可用別名：{aliases}，或 `!model reset`"
            )
            return
        _session_model[session_key] = resolved
        _save_sessions()
        await message.channel.send(f"{message.author.mention} 本 session 模型已切換為 `{resolved}`")
        log.info(f"Model override for {session_key}: {resolved}")
        return

    if content == "!status":
        session_id = _sessions.get(session_key)
        turn = _turn_counts.get(session_key, 0)
        shared = "共享（此範圍內所有人同一對話）" if _is_shared_scope(session_key) else "個人（每人獨立對話）"
        lines = [
            "**Session 狀態**",
            f"- session key: `{session_key}`（scope: `{SESSION_SCOPE}`，{shared}）",
            f"- Claude session: `{session_id[:8] + '…' if session_id else '尚未建立'}`",
            f"- 輪數: {turn}/{MAX_TURNS}",
            f"- 模型: `{_session_model.get(session_key, 'default')}`",
            f"- 工作目錄: `{_session_workdir.get(session_key, WORKING_DIR)}`",
            f"- 狀態: {'已停止（!reset 重啟）' if session_key in _stopped_sessions else '進行中'}",
        ]
        await message.channel.send(f"{message.author.mention}\n" + "\n".join(lines))
        return

    if content == "!help":
        lines = [
            "**Discord ↔ Claude Bridge 指令**",
            "",
            "**Session 控制**",
            "`!reset` — 清除 session，重新開始",
            "`!cancel` — 中斷進行中的執行（session 保留）",
            "`!stop` — 停止 Claude 回應（bot 訊息被擋下）",
            "`!flush` — 壓縮對話記憶存檔",
            "`!status` — 顯示目前 session 狀態（key / 輪數 / 模型）",
            "`!model <name>` — 切換模型（opus / sonnet / haiku / fable）",
            "`[[ws:路徑]]` — 設定本 session 的工作目錄（可與任務同一則訊息）",
            "",
            "**工作模式**（`!cmd <task>`，後接任務內容）",
        ]
        for cmd, spec in _CMD_MAP.items():
            lines.append(f"`{spec['help']}`")
        lines.append("")
        lines.append("**CLI 直通指令**")
        for cmd, spec in _DIRECT_CMD_MAP.items():
            lines.append(f"`{spec['help']}`")
        lines.append("")
        lines.append("`!help` — 顯示此說明")
        await message.channel.send(f"{message.author.mention}\n" + "\n".join(lines))
        return

    # Direct CLI commands (ultrareview etc.) — run subcommand, not --print
    for dcmd, dspec in _DIRECT_CMD_MAP.items():
        if content == dcmd or content.startswith(dcmd + " "):
            extra = content[len(dcmd):].strip().split() if content != dcmd else []
            cli_args = list(dspec["cli"]) + extra
            log.info(f"Direct CLI: {cli_args} from {message.author} ({message.author.id})")
            await message.channel.send(f"{message.author.mention} 執行中：`claude {' '.join(cli_args)}`（可能需要數分鐘）")
            async with message.channel.typing():
                result = await _run_direct_cmd(cli_args)
            chunks = chunk_text(result)
            for chunk in chunks:
                await message.channel.send(chunk)
            return

    # silent ack: known ack tokens OR any all-punctuation message from a bot
    if content in _ACK_CONTENT or (message.author.bot and _is_punct_only(content)):
        log.info(f"Ack-drop ({content!r}) for {session_key}")
        return

    # fast-path: [DONE] blocks further bot messages immediately, before debounce
    if session_key in _stopped_sessions and message.author.bot:
        if message.author.id not in WEBHOOK_PASSTHROUGH_IDS:
            return
        # Whitelisted webhook: auto-reset stopped session so new tasks are processed fresh
        _stopped_sessions.discard(session_key)
        _sessions.pop(session_key, None)
        _turn_counts.pop(session_key, None)
        _save_sessions()
        log.info(f"Webhook passthrough auto-reset for {session_key} (user {message.author.id})")

    # shared sessions (thread/channel scope): tag each buffered message with the
    # speaker so Claude can tell participants apart (commands stay unprefixed)
    if _is_shared_scope(session_key) and not content.startswith("!"):
        speaker = getattr(message.author, "display_name", None) or str(message.author)
        content = f"[{speaker}] {content}"

    # debounce: buffer content and restart sliding-window timer
    _pending_texts[session_key].append(content)
    _pending_author[session_key] = message.author.id
    if session_key in _pending:
        _pending[session_key].cancel()

    # [未完] on any chunk: buffer only, don't start flush timer, wait for next part
    if STREAM_HOLD_SIGNAL and STREAM_HOLD_SIGNAL in content:
        _pending_texts[session_key][-1] = (
            _pending_texts[session_key][-1].replace(STREAM_HOLD_SIGNAL, "").rstrip()
        )
        _pending.pop(session_key, None)
        _pending_channel[session_key] = message.channel
        log.info(f"[未完] buffered chunk for {session_key}, waiting for next part")
        return

    _pending_channel[session_key] = message.channel
    task = asyncio.create_task(
        _flush(session_key, message.channel, message.author, message.author.bot)
    )
    _pending[session_key] = task


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        sys.exit("DISCORD_TOKEN is not set")
    # unattended-permissions mode with no user allowlist = anyone in the channel
    # can run arbitrary commands on this machine; refuse unless explicitly overridden
    if "--dangerously-skip-permissions" in CLAUDE_EXTRA_ARGS and not ALLOWED_USER_IDS:
        if os.getenv("UNSAFE_ALLOW_ALL_USERS") != "1":
            sys.exit(
                "Refusing to start: --dangerously-skip-permissions is set but "
                "ALLOWED_USER_IDS is empty (anyone could execute commands). "
                "Set ALLOWED_USER_IDS, or set UNSAFE_ALLOW_ALL_USERS=1 to override."
            )
        log.warning("UNSAFE_ALLOW_ALL_USERS=1: skip-permissions with no user allowlist!")
    client.run(DISCORD_TOKEN)
