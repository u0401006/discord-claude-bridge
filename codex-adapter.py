#!/usr/bin/env python3
"""
codex-adapter.py — OpenAI Codex CLI adapter for discord-claude-bridge

Wraps the `codex` CLI to mimic the Claude Code CLI interface so bot.py
needs zero changes.

  Input:  same args as `claude --print --output-format json -- <prompt>`
  Output: {"result": "...", "session_id": "..."}

Session history is stored in ~/.codex-sessions/<session_id>.json as a
plain conversation transcript; each turn is prepended to the next prompt
so Codex has context across turns.

Requirements:
  npm install -g @openai/codex        (install Codex CLI globally)
  OPENAI_API_KEY env var              (set in .env or shell)

Usage (via bot .env):
  CLAUDE_BIN=/path/to/discord-claude-bridge/codex-adapter.py
  CLAUDE_EXTRA_ARGS=--model o4-mini   # optional; default: codex-mini-latest
  OPENAI_API_KEY=sk-...

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

# Max turns of history to inject (keeps prompt from growing unbounded)
MAX_HISTORY_TURNS = int(os.getenv("CODEX_HISTORY_TURNS", "6"))

# Approval mode: full-auto skips all interactive prompts
APPROVAL_MODE = os.getenv("CODEX_APPROVAL_MODE", "full-auto")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b[@-Z\\-_]|\x1b[()][A-B0-2]")
_CTRL_RE = re.compile(r"[\r\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keep \n \t


def strip_ansi(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    return text.strip()


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
    """Prepend conversation history so Codex has multi-turn context."""
    if not history:
        return new_prompt

    turns = history[-(MAX_HISTORY_TURNS * 2):]  # keep last N user+assistant pairs
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


def run_codex(prompt: str, model: str) -> str:
    """Spawn codex CLI and return its text output."""
    cmd = [
        CODEX_BIN,
        "--approval-mode", APPROVAL_MODE,
        "--model", model,
        "--quiet",          # suppress interactive TUI chrome
        prompt,
    ]

    env = {**os.environ}  # inherit OPENAI_API_KEY etc.

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=int(os.getenv("CODEX_TIMEOUT", "120")),
            env=env,
        )
        output = result.stdout or ""
        if result.returncode != 0 and not output:
            err = result.stderr or "(no output)"
            return f"[ERROR] codex exited {result.returncode}:\n{err[:1800]}"

        cleaned = strip_ansi(output)
        return cleaned or "(no output)"

    except FileNotFoundError:
        return (
            f"[ERROR] `{CODEX_BIN}` not found. "
            "Install with: npm install -g @openai/codex"
        )
    except subprocess.TimeoutExpired:
        return f"[ERROR] Codex timed out after {os.getenv('CODEX_TIMEOUT', '120')}s"
    except Exception as e:
        return f"[ERROR] {e}"


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    # Flags passed by bot.py that we accept but may ignore
    parser.add_argument("--print", action="store_true")
    parser.add_argument("--output-format", default="json")
    parser.add_argument("--resume", default=None, metavar="SESSION_ID")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    # Model override (set via CLAUDE_EXTRA_ARGS=--model o4-mini in .env)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    # Prompt comes after --
    parser.add_argument("prompt", nargs="?", default=None)

    # Split on "--" separator (bot.py passes: ... --output-format json -- <prompt>)
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

    # Session management
    session_id = args.resume or str(uuid.uuid4())
    history = load_history(session_id) if args.resume else []

    # Build prompt with conversation history injected
    full_prompt = build_prompt_with_history(history, prompt)

    # Call Codex CLI
    reply = run_codex(full_prompt, args.model)

    # Update and persist history
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": reply})
    save_history(session_id, history)

    # Output in Claude Code compatible format
    out = {"result": reply, "session_id": session_id}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
