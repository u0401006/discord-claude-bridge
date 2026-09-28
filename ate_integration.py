"""Opt-in prompt hints for the local Sheets CLI. No credentials or Sheets I/O."""
from datetime import datetime
import json
from pathlib import Path
import shlex
from zoneinfo import ZoneInfo


def instruction(tool_dir: str) -> str:
    if not tool_dir:
        return ""
    root = Path(tool_dir).expanduser().resolve()
    python = root / ".venv" / "bin" / "python"
    command = shlex.join([str(python), str(root / "ate.py")])
    return (
        "[Bridge configuration: A personal food-record tool is available. "
        "For food logging or nutrition queries, first read " + str(root / "AGENT.md") + ". "
        "Invoke the JSON CLI with: " + command + ". "
        "Use the bridge message context's source ID and date. "
        "Never disclose credential files. Do not use this tool for unrelated tasks.]\n\n"
    )


def message_context(message_id: int, user_id: int, created_at: datetime) -> str:
    local = created_at.astimezone(ZoneInfo("Asia/Taipei"))
    context = {"message_id": str(message_id), "user_id": str(user_id),
               "date": local.date().isoformat(), "timezone": "Asia/Taipei"}
    return "\n\n[Bridge message context: " + json.dumps(context, ensure_ascii=False) + "]"
