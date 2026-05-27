#!/usr/bin/env python3
"""
channel_summary.py — 摘要指定 Discord channel 在一段時間內的活動。

Usage:
    # 最近 7 天，channel 清單從 env 的 ALLOWED_CHANNEL_IDS 讀取
    python channel_summary.py --days 7

    # 指定日期區間
    python channel_summary.py --from 2026-05-15 --to 2026-05-22

    # 指定 channel（逗號分隔）
    python channel_summary.py --days 7 --channels 1234567890,9876543210

    # 輸出到檔案（不輸出到 stdout）
    python channel_summary.py --days 7 --out summary.md

    # 只輸出原始訊息，不呼叫 Claude
    python channel_summary.py --days 7 --no-summary
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

DISCORD_EPOCH_MS = 1420070400000
API = "https://discord.com/api/v10"

# Max chars of transcript fed to Claude (keeps token count sane)
TRANSCRIPT_LIMIT = 12000


# ── Discord REST helpers ───────────────────────────────────────────────────────

def _get(path: str, token: str, params: dict | None = None) -> dict | list:
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (channel_summary, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code} on {path}: {body}") from e


def dt_to_snowflake(dt: datetime) -> int:
    ms = int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS
    return ms << 22


def snowflake_to_dt(snowflake: int) -> datetime:
    ms = (snowflake >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def get_channel_name(token: str, channel_id: str) -> str:
    try:
        ch = _get(f"/channels/{channel_id}", token)
        return ch.get("name", channel_id)  # type: ignore[union-attr]
    except RuntimeError:
        return channel_id


def fetch_messages_in_range(
    token: str,
    channel_id: str,
    after_dt: datetime,
    before_dt: datetime,
) -> list[dict]:
    """Paginate through messages in [after_dt, before_dt]."""
    before_sf = str(dt_to_snowflake(before_dt))
    after_sf = dt_to_snowflake(after_dt)

    messages: list[dict] = []
    cursor = before_sf

    while True:
        try:
            batch: list[dict] = _get(  # type: ignore[assignment]
                f"/channels/{channel_id}/messages",
                token,
                {"limit": 100, "before": cursor},
            )
        except RuntimeError as e:
            print(f"  [warn] {e}", file=sys.stderr)
            break

        if not batch:
            break

        oldest_sf: int | None = None
        for msg in batch:
            msg_sf = int(msg["id"])
            if oldest_sf is None or msg_sf < oldest_sf:
                oldest_sf = msg_sf
            if msg_sf > after_sf:
                messages.append(msg)

        if oldest_sf is None or oldest_sf <= after_sf:
            break
        if len(batch) < 100:
            break

        cursor = str(oldest_sf)
        time.sleep(0.25)  # stay under rate limit

    messages.sort(key=lambda m: int(m["id"]))
    return messages


# ── Formatting ────────────────────────────────────────────────────────────────

def format_channel_transcript(channel_name: str, messages: list[dict]) -> str:
    if not messages:
        return f"\n## #{channel_name}\n（無訊息）\n"

    lines = [f"\n## #{channel_name}\n"]
    for msg in messages:
        content = msg.get("content", "").strip()
        # Skip empty, purely-attachment, or bot-status messages
        if not content:
            if msg.get("embeds") or msg.get("attachments"):
                content = "[附件/嵌入內容]"
            else:
                continue

        ts = snowflake_to_dt(int(msg["id"])).strftime("%m-%d %H:%M")
        author = msg.get("author", {}).get("username", "?")
        is_bot = msg.get("author", {}).get("bot", False)
        bot_tag = " [bot]" if is_bot else ""
        # Trim very long messages
        if len(content) > 600:
            content = content[:597] + "…"
        lines.append(f"[{ts}] **{author}**{bot_tag}: {content}")

    return "\n".join(lines) + "\n"


# ── Claude summarizer ─────────────────────────────────────────────────────────

def summarize(transcript: str, date_range: str, claude_bin: str) -> str:
    prompt = (
        f"以下是 Discord 頻道在 {date_range} 的訊息紀錄，請整理：\n\n"
        "1. **主要進度** — 各頻道完成或推進了什麼\n"
        "2. **結論與決策** — 有哪些確認事項或共識\n"
        "3. **待辦/規劃** — 提到但尚未完成的事\n\n"
        "規則：條列式、繁體中文、省略閒聊與重複訊息、一個要點一行。\n\n"
        "---\n"
        f"{transcript[:TRANSCRIPT_LIMIT]}"
    )

    result = subprocess.run(
        [claude_bin, "--dangerously-skip-permissions", "--print",
         "--output-format", "json", "--", prompt],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        return f"Claude error (exit {result.returncode}):\n{result.stderr[:400]}"

    raw = result.stdout.strip()
    try:
        return json.loads(raw).get("result", raw)
    except json.JSONDecodeError:
        return raw


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Discord channel activity")
    parser.add_argument(
        "--env",
        default="/Users/capo/code/agent-instances/jimingcc-config/.env",
        help="Path to .env file with DISCORD_TOKEN",
    )
    parser.add_argument("--days", type=int, default=7, help="Last N days (default: 7)")
    parser.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD")
    parser.add_argument(
        "--channels",
        help="Channel IDs comma-separated (default: ALLOWED_CHANNEL_IDS from env)",
    )
    parser.add_argument("--out", help="Write output to this file instead of stdout")
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Output raw transcript only, skip Claude",
    )
    args = parser.parse_args()

    load_dotenv(args.env)
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        sys.exit("DISCORD_TOKEN not set")

    claude_bin = os.getenv("CLAUDE_BIN", "/Users/capo/.local/bin/claude")

    # Date range
    now = datetime.now(tz=timezone.utc)
    if args.from_date and args.to_date:
        after_dt = datetime.fromisoformat(args.from_date).replace(tzinfo=timezone.utc)
        before_dt = (
            datetime.fromisoformat(args.to_date).replace(tzinfo=timezone.utc)
            + timedelta(days=1)
        )
    else:
        after_dt = now - timedelta(days=args.days)
        before_dt = now

    date_range = f"{after_dt.strftime('%Y-%m-%d')} ~ {(before_dt - timedelta(seconds=1)).strftime('%Y-%m-%d')}"
    print(f"Date range : {date_range}", file=sys.stderr)

    # Channel list
    if args.channels:
        channel_ids = [c.strip() for c in args.channels.split(",") if c.strip()]
    else:
        env_val = os.getenv("ALLOWED_CHANNEL_IDS", "")
        channel_ids = [c.strip() for c in env_val.split(",") if c.strip()]

    if not channel_ids:
        sys.exit(
            "No channels found. Use --channels or set ALLOWED_CHANNEL_IDS in the env file."
        )

    print(f"Channels   : {len(channel_ids)}", file=sys.stderr)

    # Fetch and format
    transcript_parts: list[str] = []
    for ch_id in channel_ids:
        name = get_channel_name(token, ch_id)
        print(f"  #{name} ({ch_id}) …", file=sys.stderr, end=" ", flush=True)
        msgs = fetch_messages_in_range(token, ch_id, after_dt, before_dt)
        print(f"{len(msgs)} msgs", file=sys.stderr)
        transcript_parts.append(format_channel_transcript(name, msgs))

    full_transcript = "\n".join(transcript_parts)

    if args.no_summary:
        output = f"# Discord 訊息紀錄 {date_range}\n\n{full_transcript}"
    else:
        print("Summarizing with Claude …", file=sys.stderr)
        summary = summarize(full_transcript, date_range, claude_bin)
        output = (
            f"# Discord 活動摘要 {date_range}\n\n"
            f"{summary}\n\n"
            f"---\n\n"
            f"## 原始訊息\n\n{full_transcript}"
        )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
