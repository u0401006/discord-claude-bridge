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
# Webhook/bot user IDs that bypass _stopped_sessions and auto-reset on each new task.
# Add webhook user IDs here to allow task dispatchers to re-trigger Claude after [DONE].
WEBHOOK_PASSTHROUGH_IDS: set[int] = {
    int(x) for x in os.getenv("WEBHOOK_PASSTHROUGH_IDS", "").split(",") if x.strip()
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

# marker for shared cross-bot task state posted in Discord threads
_TASK_STATE_MARKER = "📋 [TASK STATE]"

# per-session model override: session_key → model name
_session_model: dict[str, str] = {}

# ── CLI command mapping ──────────────────────────────────────────────────────
# Each entry: "!cmd" → {"prefix": str, "args": list[str], "help": str}
# - prefix: prepended to the user prompt (system-style instruction)
# - args: extra CLI flags added to the claude invocation
# - help: shown in !help output
# Commands that take remaining text as <task>; standalone commands return immediately.

_CMD_MAP: dict[str, dict] = {
    "!plan": {
        "prefix": (
            "[Mode: Plan] 在動手寫任何程式碼之前，先產出結構化的實作計畫：\n"
            "1. 目標確認\n2. 子任務拆解（含檔案路徑）\n3. 風險與邊界條件\n4. 驗收標準\n"
            "計畫完成後停下，等使用者確認才執行。\n\n"
        ),
        "args": [],
        "help": "!plan <task> — 先規劃再執行，等確認後才動手",
    },
    "!think": {
        "prefix": (
            "[Mode: Deep Think] 這個問題需要深度思考。\n"
            "請先花時間分析問題的各個面向、可能的方案與 trade-offs，"
            "再給出你的結論和建議。不急著給答案，品質優先。\n\n"
        ),
        "args": [],
        "help": "!think <task> — 深度思考模式，分析各面向再給結論",
    },
    "!review": {
        "prefix": (
            "[Mode: Code Review] 請對以下程式碼或變更進行 code review：\n"
            "- 指出 bug、安全風險、效能問題\n"
            "- 建議改善方向\n"
            "- 標明嚴重程度 (critical / warning / suggestion)\n\n"
        ),
        "args": [],
        "help": "!review <code or file> — Code review 模式",
    },
    "!debug": {
        "prefix": (
            "[Mode: Systematic Debug] 請用系統化方式除錯：\n"
            "1. 重現問題（確認症狀）\n2. 建立假設\n3. 驗證假設（讀 code / 跑測試）\n"
            "4. 找到 root cause\n5. 修復並驗證\n"
            "每一步都回報你的發現。\n\n"
        ),
        "args": [],
        "help": "!debug <問題描述> — 系統化除錯流程",
    },
    "!quick": {
        "prefix": (
            "[Mode: Quick] 直接執行，不需要解釋過程。"
            "只回覆結果和必要的 before/after 對比。\n\n"
        ),
        "args": [],
        "help": "!quick <task> — 快速執行，省略解釋",
    },
    "!summarize": {
        "prefix": (
            "[Mode: Summarize] 請用繁體中文、中央社風格，將以下內容濃縮為 100 字以內摘要。"
            "只輸出摘要本文。\n\n"
        ),
        "args": [],
        "help": "!summarize <內容> — 中央社風格 100 字摘要",
    },
}

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


def _parse_command(content: str) -> tuple[str, list[str]]:
    """Parse !cmd from content. Returns (modified_content, extra_cli_args).

    If content starts with a mapped !cmd, prepend its prefix and collect extra args.
    """
    extra_args: list[str] = []
    for cmd, spec in _CMD_MAP.items():
        if content == cmd or content.startswith(cmd + " "):
            task = content[len(cmd):].strip()
            prefix = spec["prefix"]
            extra_args = list(spec["args"])
            return prefix + task, extra_args
    return content, extra_args

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
            if msg.content.startswith(_TASK_STATE_MARKER):
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


async def run_claude(
    prompt: str, session_key: str, extra_args: list[str] | None = None
) -> str:
    session_id = _sessions.get(session_key)

    # Inject exit-signal instruction once on the first turn of a new session
    if not session_id:
        prompt = _make_session_instruction(session_key) + prompt

    args = [CLAUDE_BIN] + CLAUDE_EXTRA_ARGS
    # per-session model override
    model = _session_model.get(session_key)
    if model:
        args += ["--model", model]
    if extra_args:
        args += extra_args
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
            # Claude CLI outputs errors as JSON to stdout (not stderr) on some failures
            raw_out = stdout.decode().strip()
            human_reason = ""
            if raw_out:
                try:
                    out_data = json.loads(raw_out)
                    result_msg = out_data.get("result", "")
                    api_status = out_data.get("api_error_status")
                    if api_status == 429 or "session limit" in result_msg.lower():
                        human_reason = f"Claude session limit 已達上限。{result_msg}"
                    elif result_msg:
                        human_reason = result_msg
                except json.JSONDecodeError:
                    human_reason = raw_out[:500]
            detail = human_reason or err or "(no detail)"
            log.error(f"claude exit {proc.returncode}: {detail[:200]}")
            return f"```\nError (exit {proc.returncode}):\n{detail[:1800]}\n```"

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
        return

    log.info(
        f"Request from {author} ({author.id}) [{session_key}] "
        f"turn {current_turn}/{MAX_TURNS}: {combined[:80]}"
    )

    async with channel.typing():  # type: ignore[arg-type]
        reply = await run_claude(combined, session_key, extra_args=extra_args)

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

    # silently drop task state announcements — bots read them via fetch_task_state(), not on_message
    if content.startswith(_TASK_STATE_MARKER):
        return

    # Handle file attachments: download and save to /tmp; append text content for .txt files
    IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    TEXT_EXTS  = {'.txt'}
    attachment_texts = []
    if message.attachments:
        import aiohttp
        for att in message.attachments:
            ext = '.' + att.filename.rsplit('.', 1)[-1].lower() if '.' in att.filename else ''
            tmp_path = f"/tmp/discord_attach_{att.filename}"
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.get(att.url) as resp:
                        raw = await resp.read()
                        with open(tmp_path, 'wb') as f:
                            f.write(raw)
                if ext in IMAGE_EXTS:
                    attachment_texts.append(f"\n\n[圖片附件：{att.filename}，已存至 {tmp_path}]")
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
            await message.channel.send(f"{message.author.mention} 已恢復預設模型。")
            return
        resolved = _MODEL_ALIASES.get(arg.lower(), arg)
        _session_model[session_key] = resolved
        await message.channel.send(f"{message.author.mention} 本 session 模型已切換為 `{resolved}`")
        log.info(f"Model override for {session_key}: {resolved}")
        return

    if content == "!help":
        lines = [
            "**Discord ↔ Claude Bridge 指令**",
            "",
            "**Session 控制**",
            "`!reset` — 清除 session，重新開始",
            "`!stop` — 停止 Claude 回應（bot 訊息被擋下）",
            "`!flush` — 壓縮對話記憶存檔",
            "`!model <name>` — 切換模型（opus / sonnet / haiku）",
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
