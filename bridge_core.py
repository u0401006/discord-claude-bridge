"""
bridge_core.py — platform-agnostic core shared by all chat frontends.

The bridge has exactly one backend seam: the CLI JSON contract
    <CLAUDE_BIN> [extra args] [--model M] [--resume ID] --print --output-format json -- <prompt>
    → stdout: {"result": "...", "session_id": "..."}
Anything that speaks this contract (claude CLI, codex-adapter.py,
openai-adapter.py) works with every frontend (bot.py for Discord,
gchat_bridge.py for Google Chat) with zero changes.

This module holds everything that is neither Discord- nor Google-Chat-specific:
  - run_backend():      one backend invocation over the CLI contract
  - load/save_sessions: the persisted session envelope (atomic writes)
  - chunk_text():       fence-aware message splitting (limit is per-platform)
  - CMD_MAP / parse_command: the !plan/!think/... prompt-mode prefixes
  - RateLimiter:        sliding-window per-user rate limiting
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ── backend invocation (the CLI JSON contract) ───────────────────────────────

@dataclass
class BackendReply:
    text: str                       # user-visible result (or error text)
    session_id: str | None = None   # returned on success; persist for --resume
    ok: bool = True
    stale_session: bool = False     # --resume ID no longer exists; caller should retry fresh


async def run_backend(
    prompt: str,
    *,
    backend_bin: str,
    base_args: list[str] | tuple[str, ...] = (),
    model: str | None = None,
    extra_args: list[str] | tuple[str, ...] = (),
    resume: str | None = None,
    cwd: str | None = None,
    timeout: int = 120,
) -> BackendReply:
    """Run one backend call over the CLI JSON contract. No session bookkeeping —
    callers own the session store and decide how to react to stale_session."""
    args = [backend_bin, *base_args]
    if model:
        args += ["--model", model]
    if extra_args:
        args += list(extra_args)
    if resume:
        args += ["--resume", resume]
    args += ["--print", "--output-format", "json", "--", prompt]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return BackendReply(f"Timeout after {timeout}s", ok=False)

        if proc.returncode != 0:
            err = stderr.decode().strip()
            # 失效 session：交由呼叫端清除舊 ID 並重試（不帶 --resume）
            if resume and "No conversation found" in err:
                return BackendReply("", ok=False, stale_session=True)
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
            log.error(f"backend exit {proc.returncode}: {detail[:200]}")
            return BackendReply(
                f"```\nError (exit {proc.returncode}):\n{detail[:1800]}\n```", ok=False
            )

        raw = stdout.decode().strip()
        try:
            data = json.loads(raw)
            return BackendReply(
                data.get("result") or "(no output)", session_id=data.get("session_id")
            )
        except json.JSONDecodeError:
            return BackendReply(raw or "(no output)")

    except FileNotFoundError:
        return BackendReply(f"`{backend_bin}` not found. Set CLAUDE_BIN in .env.", ok=False)
    except Exception as e:
        return BackendReply(f"Bridge error: {e}", ok=False)


# ── session persistence (shared envelope format) ─────────────────────────────

def load_sessions(path: str) -> tuple[dict[str, str], dict[str, int], set[str], dict[str, str]]:
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and "sessions" in data:
            # New envelope format
            sessions = data["sessions"]
            turn_counts = {k: int(v) for k, v in data.get("turn_counts", {}).items()}
            stopped = set(data.get("stopped_sessions", []))
            models = dict(data.get("session_models", {}))
        else:
            # Legacy format: plain {session_key: session_id}
            sessions = data
            turn_counts = {}
            stopped = set()
            models = {}
        return sessions, turn_counts, stopped, models
    except (FileNotFoundError, json.JSONDecodeError):
        return {}, {}, set(), {}


def save_sessions(
    path: str,
    sessions: dict[str, str],
    turn_counts: dict[str, int],
    stopped: set[str],
    models: dict[str, str],
) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = {
        "sessions": sessions,
        "turn_counts": dict(turn_counts),
        "stopped_sessions": list(stopped),
        "session_models": models,
    }
    # atomic write: a crash mid-dump must not corrupt the existing file
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.replace(tmp_path, path)


# ── message chunking (fence-aware) ───────────────────────────────────────────

def scan_fence(text: str) -> str | None:
    """Return the currently-open fence lang (empty str for plain ```), or None if closed."""
    state: str | None = None
    for line in text.splitlines():
        m = re.match(r"^```(\w*)$", line.rstrip())
        if m:
            state = None if state is not None else m.group(1)
    return state


def chunk_text(text: str, limit: int = 1900) -> list[str]:
    """Split text into platform-safe chunks, closing/reopening code fences at boundaries."""
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

        fence = scan_fence(head)
        if fence is not None:
            head += "\n```"
            reopen = f"```{fence}\n"

        chunks.append(head)

    return chunks


# ── !cmd prompt-mode prefixes (frontend-agnostic prompt engineering) ─────────
# Each entry: "!cmd" → {"prefix": str, "args": list[str], "help": str}
# - prefix: prepended to the user prompt (system-style instruction)
# - args: extra CLI flags added to the backend invocation
# - help: shown in !help output

CMD_MAP: dict[str, dict] = {
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


def parse_command(content: str) -> tuple[str, list[str]]:
    """Parse !cmd from content. Returns (modified_content, extra_cli_args).

    If content starts with a mapped !cmd, prepend its prefix and collect extra args.
    """
    extra_args: list[str] = []
    for cmd, spec in CMD_MAP.items():
        if content == cmd or content.startswith(cmd + " "):
            task = content[len(cmd):].strip()
            prefix = spec["prefix"]
            extra_args = list(spec["args"])
            return prefix + task, extra_args
    return content, extra_args


# ── rate limiting ────────────────────────────────────────────────────────────

class RateLimiter:
    """Sliding-window per-key limiter (key is typically a user id)."""

    def __init__(self, per_min: int):
        self.per_min = per_min
        self._buckets: dict = defaultdict(list)

    def is_limited(self, key) -> bool:
        now = time.monotonic()
        bucket = [t for t in self._buckets[key] if now - t < 60]
        self._buckets[key] = bucket
        if len(bucket) >= self.per_min:
            return True
        bucket.append(now)
        return False
