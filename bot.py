"""
Discord ↔ Claude Code Bridge
每則訊息透過 `claude --print` subprocess 送進 Claude Code，結果回傳 Discord。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict

import discord
from dotenv import load_dotenv

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
WORKING_DIR = os.path.expanduser(os.getenv("WORKING_DIR", "~/agent-dev"))
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
# Extra args prepended to every CLAUDE_BIN call.
# Claude Code default: --dangerously-skip-permissions
# OpenAI adapter: leave empty or set --model gpt-4o
CLAUDE_EXTRA_ARGS: list[str] = [
    a for a in os.getenv("CLAUDE_EXTRA_ARGS", "--dangerously-skip-permissions").split() if a
]
TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "120"))  # seconds
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "5"))
MAX_TURNS = int(os.getenv("MAX_TURNS_PER_SESSION", "20"))
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

_LOG_DIR = os.path.dirname(os.path.expanduser(os.getenv("LOG_FILE", "logs/bot.log")))
SESSIONS_FILE = os.path.join(_LOG_DIR, "sessions.json")
MEMORY_DIR = os.path.join(_LOG_DIR, "memory")

# rate limiter: user_id → list of timestamps
_rate_buckets: dict[int, list[float]] = defaultdict(list)

# turn counter / stopped sessions — initialized from disk below (see _load_sessions)
_turn_counts: dict[str, int] = defaultdict(int)
_stopped_sessions: set[str] = set()

# debounce: buffer incoming messages, cancel+restart timer on each new message
_pending: dict[str, asyncio.Task] = {}
_pending_texts: dict[str, list[str]] = defaultdict(list)

# thread redirect: session_key → channel to reply to (updated by on_thread_create)
_pending_channel: dict[str, discord.abc.Messageable] = {}

# token Claude uses to signal conversation end
_DONE_SIGNAL = "[DONE]"

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
    # Extract channel_id from session_key format: ch{channel_id}_u{user_id}
    channel_id = ""
    if session_key.startswith("ch") and "_u" in session_key:
        channel_id = session_key[2:].split("_u")[0]

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


def _load_sessions() -> tuple[dict[str, str], dict[str, int], set[str]]:
    try:
        with open(SESSIONS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict) and "sessions" in data:
            # New envelope format
            sessions = data["sessions"]
            turn_counts = {k: int(v) for k, v in data.get("turn_counts", {}).items()}
            stopped = set(data.get("stopped_sessions", []))
        else:
            # Legacy format: plain {session_key: session_id}
            sessions = data
            turn_counts = {}
            stopped = set()
        return sessions, turn_counts, stopped
    except (FileNotFoundError, json.JSONDecodeError):
        return {}, {}, set()


def _save_sessions() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(SESSIONS_FILE)), exist_ok=True)
    data = {
        "sessions": _sessions,
        "turn_counts": dict(_turn_counts),
        "stopped_sessions": list(_stopped_sessions),
    }
    with open(SESSIONS_FILE, "w") as f:
        json.dump(data, f)


# session tracker — all three dicts persisted together across restarts
_sessions, _tc, _ss = _load_sessions()
_sessions: dict[str, str] = _sessions          # type: ignore[no-redef]
_turn_counts = defaultdict(int, _tc)
_stopped_sessions: set[str] = _ss
del _tc, _ss


def is_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    bucket = _rate_buckets[user_id]
    _rate_buckets[user_id] = [t for t in bucket if now - t < 60]
    if len(_rate_buckets[user_id]) >= RATE_LIMIT_PER_MIN:
        return True
    _rate_buckets[user_id].append(now)
    return False


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# ── helpers ──────────────────────────────────────────────────────────────────

def _scan_fence(text: str) -> str | None:
    """Return the currently-open fence lang (empty str for plain ```), or None if closed."""
    state: str | None = None
    for line in text.splitlines():
        m = re.match(r"^```(\w*)$", line.rstrip())
        if m:
            state = None if state is not None else m.group(1)
    return state


def chunk_text(text: str, limit: int = DISCORD_CHUNK) -> list[str]:
    """Split text into Discord-safe chunks, closing/reopening code fences at boundaries."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    reopen = ""  # fence re-open header to prepend to next chunk

    while remaining:
        work = reopen + remaining
        reopen = ""

        if len(work) <= limit:
            chunks.append(work)
            break

        # Leave 4 chars headroom for potential "\n```" close suffix
        budget = limit - 4
        nl = work.rfind("\n", 0, budget)
        cut = nl if nl > budget // 2 else budget  # prefer newline; hard-cut for long lines

        head = work[:cut]
        remaining = work[cut:].lstrip("\n")

        fence = _scan_fence(head)
        if fence is not None:
            head += "\n```"
            reopen = f"```{fence}\n"

        chunks.append(head)

    return chunks


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


async def run_claude(prompt: str, session_key: str) -> str:
    session_id = _sessions.get(session_key)

    # Inject exit-signal instruction once on the first turn of a new session
    if not session_id:
        prompt = _make_session_instruction(session_key) + prompt

    args = [CLAUDE_BIN] + CLAUDE_EXTRA_ARGS
    if session_id:
        args += ["--resume", session_id]
    args += ["--print", "--output-format", "json", "--", prompt]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=WORKING_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Timeout after {TIMEOUT}s"

        if proc.returncode != 0:
            err = stderr.decode().strip()
            # 失效 session：清除舊 ID 並重試一次（不帶 --resume）
            if session_id and "No conversation found" in err:
                _sessions.pop(session_key, None)
                _save_sessions()
                return await run_claude(prompt, session_key)
            return f"```\nError (exit {proc.returncode}):\n{err[:1800]}\n```"

        raw = stdout.decode().strip()
        try:
            data = json.loads(raw)
            new_id: str | None = data.get("session_id")
            if new_id:
                _sessions[session_key] = new_id
                _save_sessions()
            return data.get("result") or "(no output)"
        except json.JSONDecodeError:
            return raw or "(no output)"

    except FileNotFoundError:
        return f"`{CLAUDE_BIN}` not found. Set CLAUDE_BIN in .env."
    except Exception as e:
        return f"Bridge error: {e}"


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

    if not combined:
        return

    # [DONE] only blocks bot follow-ups; humans can always continue
    if session_key in _stopped_sessions and is_bot_author:
        return

    _turn_counts[session_key] += 1
    current_turn = _turn_counts[session_key]

    if current_turn > MAX_TURNS:
        return

    log.info(
        f"Request from {author} ({author.id}) [{session_key}] "
        f"turn {current_turn}/{MAX_TURNS}: {combined[:80]}"
    )

    async with channel.typing():  # type: ignore[arg-type]
        reply = await run_claude(combined, session_key)

    # Detect Claude's self-exit signal
    if _DONE_SIGNAL in reply:
        reply = reply.replace(_DONE_SIGNAL, "").rstrip()
        _stopped_sessions.add(session_key)
        _save_sessions()
        log.info(f"Claude signaled [DONE] for {session_key}")
        if not is_bot_author:
            reply += "\n\n---\n> Claude 已結束此對話。Bot 訊息將被擋下；你仍可繼續說話，或以 `!flush` 加入記憶，以 `!reset` 開啟新 session。"
    elif is_bot_author and _is_punct_only(reply):
        # Claude returned pure punctuation in a bot-to-bot turn → auto-stop to break loop
        _stopped_sessions.add(session_key)
        _save_sessions()
        log.info(f"Claude punct-only reply in bot session, auto-stop for {session_key}")
        return  # send nothing; silence breaks the ping-pong
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
    """Redirect pending session replies into a newly created thread."""
    for sk, ch in list(_pending_channel.items()):
        if getattr(ch, "id", None) == thread.parent_id:
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

    # Handle .txt file attachments: download and append content to prompt
    txt_attachments = [a for a in message.attachments if a.filename.lower().endswith('.txt')]
    if txt_attachments:
        import tempfile, aiohttp
        attachment_texts = []
        for att in txt_attachments:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(att.url) as resp:
                        raw = await resp.read()
                        text = raw.decode('utf-8', errors='replace')
                        # Save to /tmp so Claude can reference by path if needed
                        tmp_path = f"/tmp/discord_attach_{att.filename}"
                        with open(tmp_path, 'w', encoding='utf-8') as f:
                            f.write(text)
                        attachment_texts.append(
                            f"\n\n[附件：{att.filename}，已存至 {tmp_path}]\n{text}"
                        )
            except Exception as e:
                attachment_texts.append(f"\n\n[附件 {att.filename} 讀取失敗：{e}]")
        content = content + "".join(attachment_texts)

    if not content:
        return

    if client.user and content.startswith(f"<@{client.user.id}>"):
        content = content[len(f"<@{client.user.id}>"):].strip()

    session_key = f"ch{message.channel.id}_u{message.author.id}"

    if content == "!reset":
        if session_key in _pending:
            _pending.pop(session_key).cancel()
        _pending_texts.pop(session_key, None)
        _sessions.pop(session_key, None)
        _turn_counts.pop(session_key, None)
        _stopped_sessions.discard(session_key)
        _save_sessions()
        log.info(f"Session reset for {session_key}")
        await message.channel.send(f"{message.author.mention} Session cleared.")
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

    # silent ack: known ack tokens OR any all-punctuation message from a bot
    if content in _ACK_CONTENT or (message.author.bot and _is_punct_only(content)):
        log.info(f"Ack-drop ({content!r}) for {session_key}")
        return

    # fast-path: [DONE] blocks further bot messages immediately, before debounce
    if session_key in _stopped_sessions and message.author.bot:
        return

    # debounce: buffer content and restart sliding-window timer
    _pending_texts[session_key].append(content)
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
    client.run(DISCORD_TOKEN)
