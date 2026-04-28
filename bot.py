"""
Discord ↔ Claude Code Bridge
每則訊息透過 `claude --print` subprocess 送進 Claude Code，結果回傳 Discord。
"""

import asyncio
import json
import logging
import os
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
WORKING_DIR = os.path.expanduser(os.getenv("WORKING_DIR", "~/agent-dev"))
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "120"))  # seconds
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "5"))
MAX_TURNS = int(os.getenv("MAX_TURNS_PER_SESSION", "20"))
DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "2.5"))
DISCORD_CHUNK = 1900  # Discord limit is 2000; leave room for code fences

_LOG_DIR = os.path.dirname(os.path.expanduser(os.getenv("LOG_FILE", "logs/bot.log")))
SESSIONS_FILE = os.path.join(_LOG_DIR, "sessions.json")

# rate limiter: user_id → list of timestamps
_rate_buckets: dict[int, list[float]] = defaultdict(list)

# turn counter: session_key → number of turns used (in-memory, resets on bot restart)
_turn_counts: dict[str, int] = defaultdict(int)

# stopped sessions: force-stopped by !stop or Claude's [DONE] signal (in-memory)
# only blocks bot messages; humans can still continue
_stopped_sessions: set[str] = set()

# debounce: buffer incoming messages, cancel+restart timer on each new message
_pending: dict[str, asyncio.Task] = {}
_pending_texts: dict[str, list[str]] = defaultdict(list)

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
_DONE_INSTRUCTION = (
    "[System: When you consider this conversation complete or the task fully done, "
    f"append exactly `{_DONE_SIGNAL}` on its own line at the very end of your response. "
    "The bridge will then stop forwarding further bot messages to you.]\n\n"
)


def _load_sessions() -> dict[str, str]:
    try:
        with open(SESSIONS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sessions() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(SESSIONS_FILE)), exist_ok=True)
    with open(SESSIONS_FILE, "w") as f:
        json.dump(_sessions, f)


# session tracker: session_key → claude session_id (persisted across restarts)
_sessions: dict[str, str] = _load_sessions()


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

def chunk_text(text: str, limit: int = DISCORD_CHUNK) -> list[str]:
    """Split long text into Discord-safe chunks, respecting code blocks."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


async def run_claude(prompt: str, session_key: str) -> str:
    session_id = _sessions.get(session_key)

    # Inject exit-signal instruction once on the first turn of a new session
    if not session_id:
        prompt = _DONE_INSTRUCTION + prompt

    args = [CLAUDE_BIN, "--dangerously-skip-permissions"]
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
            if "No conversation found" in err and session_id:
                # stale session: clear and retry as new conversation
                log.warning(f"Stale session {session_id} for {session_key}, retrying as new")
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
        log.info(f"Claude signaled [DONE] for {session_key}")
        if not is_bot_author:
            reply += "\n\n---\n> Claude 已結束此對話。Bot 訊息將被擋下；你仍可繼續說話，或輸入 `!reset` 開啟新 session。"
    elif is_bot_author and _is_punct_only(reply):
        # Claude returned pure punctuation in a bot-to-bot turn → auto-stop to break loop
        _stopped_sessions.add(session_key)
        log.info(f"Claude punct-only reply in bot session, auto-stop for {session_key}")
        return  # send nothing; silence breaks the ping-pong
    elif current_turn == MAX_TURNS:
        reply += f"\n\n---\n> 本對話已達上限 {MAX_TURNS} 輪，請輸入 `!reset` 重啟新的 session。"

    log.info(f"Response length: {len(reply)} chars")
    mention = author.mention
    chunks = chunk_text(reply)
    for i, chunk in enumerate(chunks):
        text = f"{mention} {chunk}" if i == 0 else chunk
        await channel.send(text)  # type: ignore[union-attr]


# ── event handlers ────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    log.info(f"Logged in as {client.user}")
    log.info(f"Allowed channels: {ALLOWED_CHANNEL_IDS or 'ALL (danger!)'}")
    log.info(f"Allowed users:   {ALLOWED_USER_IDS or 'ALL (danger!)'}")
    log.info(f"Working dir:     {WORKING_DIR}")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    # ignore system messages (thread_created, pins, joins, etc.)
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return

    # channel guard: support threads by checking parent_id against ALLOWED_CHANNEL_IDS
    if ALLOWED_CHANNEL_IDS:
        ch_id = getattr(message.channel, "parent_id", None) or message.channel.id
        if ch_id not in ALLOWED_CHANNEL_IDS:
            return

    # user guard
    if ALLOWED_USER_IDS and message.author.id not in ALLOWED_USER_IDS:
        log.warning(f"Blocked user {message.author} ({message.author.id})")
        return

    # ignore if someone else is mentioned but not the bot
    if message.mentions and client.user not in message.mentions:
        return

    # rate limit
    if is_rate_limited(message.author.id):
        await message.channel.send(f"Rate limit: max {RATE_LIMIT_PER_MIN} requests/min.")
        return

    content = message.content.strip()
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
        await message.channel.send("Session cleared.")
        return

    if content == "!stop":
        if session_key in _pending:
            _pending.pop(session_key).cancel()
        _pending_texts.pop(session_key, None)
        _stopped_sessions.add(session_key)
        log.info(f"Session force-stopped for {session_key}")
        await message.channel.send("Session stopped. 輸入 `!reset` 開啟新 session。")
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
    task = asyncio.create_task(
        _flush(session_key, message.channel, message.author, message.author.bot)
    )
    _pending[session_key] = task


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        sys.exit("DISCORD_TOKEN is not set")
    client.run(DISCORD_TOKEN)
