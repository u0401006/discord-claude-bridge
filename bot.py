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
DISCORD_CHUNK = 1900  # Discord limit is 2000; leave room for code fences

_LOG_DIR = os.path.dirname(os.path.expanduser(os.getenv("LOG_FILE", "logs/bot.log")))
SESSIONS_FILE = os.path.join(_LOG_DIR, "sessions.json")

# rate limiter: user_id → list of timestamps
_rate_buckets: dict[int, list[float]] = defaultdict(list)


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
    args = [CLAUDE_BIN, "--dangerously-skip-permissions"]
    if session_id:
        args += ["--resume", session_id]
    args += ["--print", "--output-format", "json", prompt]

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

    # channel guard
    if ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS:
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
        _sessions.pop(session_key, None)
        _save_sessions()
        log.info(f"Session reset for {session_key}")
        await message.channel.send("Session cleared.")
        return

    log.info(f"Request from {message.author} ({message.author.id}) [{session_key}]: {content[:80]}")

    async with message.channel.typing():
        reply = await run_claude(content, session_key)

    log.info(f"Response length: {len(reply)} chars")
    for chunk in chunk_text(reply):
        await message.channel.send(chunk)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        sys.exit("DISCORD_TOKEN is not set")
    client.run(DISCORD_TOKEN)
