#!/usr/bin/env python3
"""
openai-adapter.py — OpenAI backend adapter for discord-claude-bridge

Mimics the Claude Code CLI interface so bot.py needs zero changes:
  Input:  same args as `claude --print --output-format json -- <prompt>`
  Output: {"result": "...", "session_id": "..."}

Session history is stored in ~/.openai-sessions/<session_id>.json
as a standard OpenAI messages[] array.

Usage (via bot .env):
  CLAUDE_BIN=/Users/capo_mac_mini/agent-dev/discord-claude-bridge/openai-adapter.py
  CLAUDE_EXTRA_ARGS=--model gpt-4o
  OPENAI_API_KEY=sk-...

Direct test:
  python3 openai-adapter.py --print --output-format json -- "hello"
  python3 openai-adapter.py --resume <session_id> --print --output-format json -- "follow up"
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from datetime import datetime

SESSIONS_DIR = Path.home() / ".openai-sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
DEFAULT_SYSTEM = os.getenv(
    "OPENAI_SYSTEM_PROMPT",
    "You are a helpful assistant. Be concise and direct."
)


def load_history(session_id: str) -> list[dict]:
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(session_id: str, messages: list[dict]) -> None:
    path = SESSIONS_DIR / f"{session_id}.json"
    path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")


def call_openai(messages: list[dict], model: str) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        return "[ERROR] openai package not installed. Run: pip install openai"

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "[ERROR] OPENAI_API_KEY not set"

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore
        )
        return response.choices[0].message.content or "(no output)"
    except Exception as e:
        return f"[ERROR] OpenAI API: {e}"


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    # Flags passed by bot.py that we accept but may ignore
    parser.add_argument("--print", action="store_true")
    parser.add_argument("--output-format", default="json")
    parser.add_argument("--resume", default=None, metavar="SESSION_ID")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    # Our own flags (set via CLAUDE_EXTRA_ARGS in .env)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--system", default=DEFAULT_SYSTEM)
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

    # Build messages
    messages: list[dict] = []
    if not history:
        messages.append({"role": "system", "content": args.system})
    else:
        messages = history

    messages.append({"role": "user", "content": prompt})

    # Call API
    reply = call_openai(messages, args.model)

    # Update history
    messages.append({"role": "assistant", "content": reply})
    save_history(session_id, messages)

    # Output in Claude Code compatible format
    out = {"result": reply, "session_id": session_id}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
