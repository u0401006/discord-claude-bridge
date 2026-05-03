#!/usr/bin/env python3
"""
codex-adapter.py — OpenAI Codex CLI adapter for discord-claude-bridge
Tested against codex-cli 0.128.0+

Wraps the `codex exec` subcommand to mimic the Claude Code CLI interface
so bot.py needs zero changes.

  Input:  same args as `claude --print --output-format json -- <prompt>`
  Output: {"result": "...", "session_id": "..."}

Session management:
  - New session:    codex exec --dangerously-bypass-approvals-and-sandbox ... <prompt>
  - Resume session: codex exec --dangerously-bypass-approvals-and-sandbox ... resume <id> <prompt>
  - Fallback:       ~/.codex-sessions/<id>.json history injection (if resume fails)

Auth (one-time setup, run manually):
  printenv OPENAI_API_KEY | codex login --with-api-key
  # credentials saved to ~/.codex/

Requirements:
  npm install -g @openai/codex

Usage (via bot .env):
  CLAUDE_BIN=/path/to/discord-claude-bridge/codex-adapter.py
  CLAUDE_EXTRA_ARGS=--model o4-mini
  # OPENAI_API_KEY must be pre-loaded via `codex login --with-api-key`

Direct test:
  python3 codex-adapter.py --print --output-format json -- "hello"
  python3 codex-adapter.py --resume <session_id> --print --output-format json -- "follow up"
"""

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

SESSIONS_DIR = Path.home() / ".codex-sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

DEFAULT_MODEL = os.getenv("CODEX_MODEL", "codex-mini-latest")
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
MAX_HISTORY_TURNS = int(os.getenv("CODEX_HISTORY_TURNS", "6"))

# ── ANSI / control-char stripping ────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b[@-Z\\-_]|\x1b[()][A-B0-2]")
_CTRL_RE = re.compile(r"[\r\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keep \n \t


def strip_ansi(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    return text.strip()


# ── Output parsing ────────────────────────────────────────────────────────────

# codex exec header pattern:  "key   value" lines before the response body
# e.g. "workdir  /Users/...", "model  o4-mini", "session  <uuid>"
_HEADER_RE = re.compile(
    r"^(workdir|model|approval|sandbox|session[\s_]id?|session)\s.+$",
    re.IGNORECASE,
)
_SESSION_ID_RE = re.compile(
    r"(?:session[\s_]id?|session)\s+([\w-]{8,})", re.IGNORECASE
)


def parse_output(raw: str) -> tuple[str, str | None]:
    """
    Parse codex exec stdout.
    Returns (response_text, session_id_or_None).
    Strips the status header block, returns the assistant body.
    """
    cleaned = strip_ansi(raw)
    lines = cleaned.splitlines()

    # Extract session_id from header
    session_id: str | None = None
    for line in lines[:20]:  # header is near the top
        m = _SESSION_ID_RE.search(line)
        if m:
            session_id = m.group(1)
            break

    # Drop leading header lines; body starts at first non-header non-empty line
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _HEADER_RE.match(stripped):
            body_start = i + 1
        else:
            break

    body = "\n".join(lines[body_start:]).strip()
    return body or "(no output)", session_id


# ── Session history (fallback for resume) ───────────────────────────────────

def load_history(session_id: str) -> list[dict]:
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def save_history(session_id: str, history: list[dict]) -> None:
    path = SESSIONS_DIR / f"{session_id}.json"
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def build_prompt_with_history(history: list[dict], new_prompt: str) -> str:
    """Prepend last N turns as context (used when native resume is unavailable)."""
    if not history:
        return new_prompt
    turns = history[-(MAX_HISTORY_TURNS * 2):]
    parts: list[str] = ["[Conversation history]"]
    for entry in turns:
        role = entry.get("role", "")
        content = entry.get("content", "")
        if role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
    parts.append("[End history]")
    parts.append(new_prompt)
    return "\n".join(parts)


# ── Core runner ───────────────────────────────────────────────────────────────

def run_codex(prompt: str, model: str, resume_id: str | None = None) -> tuple[str, str | None]:
    """
    Spawn codex exec and return (reply_text, codex_session_id).
    Uses native `resume` subcommand if resume_id is provided.
    """
    base_flags = [
        CODEX_BIN, "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--model", model,
    ]

    if resume_id:
        cmd = base_flags + ["resume", resume_id, prompt]
    else:
        cmd = base_flags + [prompt]

    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,   # prevent hang on "Reading from stdin..."
            capture_output=True,
            text=True,
            timeout=int(os.getenv("CODEX_TIMEOUT", "120")),
        )

        raw = result.stdout or ""

        if result.returncode != 0 and not raw.strip():
            err = strip_ansi(result.stderr or "(no output)")
            return f"[ERROR] codex exited {result.returncode}:\n{err[:1800]}", None

        body, codex_sid = parse_output(raw)
        return body, codex_sid

    except FileNotFoundError:
        return (
            f"[ERROR] `{CODEX_BIN}` not found. "
            "Install: npm install -g @openai/codex",
            None,
        )
    except subprocess.TimeoutExpired:
        return f"[ERROR] Codex timed out after {os.getenv('CODEX_TIMEOUT', '120')}s", None
    except Exception as e:
        return f"[ERROR] {e}", None


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--print", action="store_true")
    parser.add_argument("--output-format", default="json")
    parser.add_argument("--resume", default=None, metavar="SESSION_ID")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("prompt", nargs="?", default=None)

    argv = sys.argv[1:]
    if "--" in argv:
        sep = argv.index("--")
        prompt_parts = argv[sep + 1:]
        argv = argv[:sep]
    else:
        prompt_parts = []

    args = parser.parse_args(argv)
    prompt = " ".join(prompt_parts) if prompt_parts else (args.prompt or "")

    if not prompt.strip():
        out = {"result": "(empty prompt)", "session_id": str(uuid.uuid4())}
        print(json.dumps(out, ensure_ascii=False))
        return

    adapter_session_id = args.resume or str(uuid.uuid4())
    history = load_history(adapter_session_id) if args.resume else []

    # Try native resume first; fall back to history injection
    if args.resume:
        reply, codex_sid = run_codex(prompt, args.model, resume_id=adapter_session_id)
        # If native resume errored (e.g. session expired), retry with history
        if reply.startswith("[ERROR]") and history:
            full_prompt = build_prompt_with_history(history, prompt)
            reply, codex_sid = run_codex(full_prompt, args.model, resume_id=None)
    else:
        reply, codex_sid = run_codex(prompt, args.model, resume_id=None)

    # Persist history (keyed by our adapter session_id)
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": reply})
    save_history(adapter_session_id, history)

    # Use codex's own session_id if captured; else keep ours
    out_session = codex_sid or adapter_session_id
    out = {"result": reply, "session_id": out_session}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
