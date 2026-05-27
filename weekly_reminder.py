#!/usr/bin/env python3
"""
weekly_reminder.py — 每週五在指定 channel 建立進度報告 thread 並 @lab 提醒。

Usage (bot token mode — bot 必須在 cnaserver):
    python weekly_reminder.py [--env /path/to/.env] [--dry-run]

Usage (webhook mode — 不需要 bot，在 Discord channel 設定 > 整合 > 新增 Webhook 取得 URL):
    python weekly_reminder.py --webhook https://discord.com/api/webhooks/... [--dry-run]

注意：webhook 模式下無法自動 @lab（role mention 需 bot token），
      請手動在 LAB_ROLE_ID 填入 role ID，或改用 bot token 模式。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from dotenv import load_dotenv

REMINDER_CHANNEL_ID = "1502222902957703219"
DOC_LINK = (
    "https://docs.google.com/document/d/1sk4J7UgywpBS-PtfSmjSdUiK9FzsnWMoTKfBZ1Wfiq8"
    "/edit?usp=sharing"
)
LAB_ROLE_NAME = "lab"
# Hard-code role ID here if using webhook mode (get from Server Settings > Roles)
LAB_ROLE_ID = os.environ.get("LAB_ROLE_ID", "")
API = "https://discord.com/api/v10"


def _request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    url = f"{API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (weekly_reminder, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {body}") from e


def get_channel(token: str, channel_id: str) -> dict:
    return _request("GET", f"/channels/{channel_id}", token)


def find_role_id(token: str, guild_id: str, role_name: str) -> str | None:
    roles = _request("GET", f"/guilds/{guild_id}/roles", token)
    for role in roles:
        if role["name"].lower() == role_name.lower():
            return role["id"]
    return None


def create_forum_thread(token: str, channel_id: str, name: str, content: str) -> dict:
    """Forum channel (type=15): thread + starter message in one request."""
    return _request(
        "POST",
        f"/channels/{channel_id}/threads",
        token,
        {"name": name, "message": {"content": content}, "auto_archive_duration": 10080},
    )


def create_text_thread(token: str, channel_id: str, name: str, content: str) -> dict:
    """Text channel: create PUBLIC_THREAD then post starter message."""
    thread = _request(
        "POST",
        f"/channels/{channel_id}/threads",
        token,
        {"name": name, "type": 11, "auto_archive_duration": 10080},
    )
    _request("POST", f"/channels/{thread['id']}/messages", token, {"content": content})
    return thread


def post_via_webhook(webhook_url: str, thread_title: str, message: str, dry_run: bool) -> None:
    """Post to channel via webhook. For forum channels this creates a new post/thread."""
    print(f"Thread : {thread_title}")
    print(f"Message: {message}")
    print("Mode   : webhook")

    if dry_run:
        print("[dry-run] 未實際發送")
        return

    payload = {
        "content": message,
        "thread_name": thread_title,  # used when posting to a forum channel
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        webhook_url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            print(f"OK: webhook response {resp.status} {body[:100]}")
    except urllib.error.HTTPError as e:
        print(f"Error: HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env",
        default="/Users/capo/code/agent-instances/jimingcc-config/.env",
    )
    parser.add_argument("--webhook", help="Discord webhook URL (alternative to bot token)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without sending")
    args = parser.parse_args()

    # Thread title: 進度報告 yy/mm/dd (today+3 = Monday)
    target_date = datetime.now() + timedelta(days=3)
    thread_title = f"進度報告 {target_date.strftime('%y/%m/%d')}"

    # ── Webhook mode ──────────────────────────────────────────────────────────
    # Also check env var DISCORD_WEBHOOK_URL as fallback
    webhook_url = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if webhook_url:
        args.webhook = webhook_url

    if args.webhook:
        role_mention = f"<@&{LAB_ROLE_ID}>" if LAB_ROLE_ID else f"@{LAB_ROLE_NAME}"
        message = f"{role_mention} 記得寫一下管理部例會報告喔！連結：{DOC_LINK}"
        post_via_webhook(args.webhook, thread_title, message, args.dry_run)
        return

    # ── Bot token mode ────────────────────────────────────────────────────────
    load_dotenv(args.env)
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        sys.exit("DISCORD_TOKEN not set")

    # Get channel metadata
    try:
        channel = get_channel(token, REMINDER_CHANNEL_ID)
    except RuntimeError as e:
        sys.exit(f"Cannot fetch channel: {e}")

    guild_id = channel.get("guild_id", "")
    channel_type = channel.get("type", 0)

    # Resolve @lab role
    role_mention = f"@{LAB_ROLE_NAME}"
    if guild_id:
        try:
            role_id = find_role_id(token, guild_id, LAB_ROLE_NAME)
            if role_id:
                role_mention = f"<@&{role_id}>"
        except RuntimeError as e:
            print(f"Warning: could not look up role: {e}", file=sys.stderr)

    message = f"{role_mention} 記得寫一下管理部例會報告喔！連結：{DOC_LINK}"

    print(f"Thread : {thread_title}")
    print(f"Message: {message}")
    print(f"Channel type: {channel_type} ({'forum' if channel_type == 15 else 'text'})")

    if args.dry_run:
        print("[dry-run] 未實際發送")
        return

    try:
        if channel_type == 15:
            result = create_forum_thread(token, REMINDER_CHANNEL_ID, thread_title, message)
        else:
            result = create_text_thread(token, REMINDER_CHANNEL_ID, thread_title, message)
        print(f"OK: thread {result.get('id')} — {result.get('name')}")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
