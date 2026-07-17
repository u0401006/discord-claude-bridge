# Discord–Claude Bridge: Architecture

## Overview

A Discord bot that bridges user messages to AI backends (Claude Code or OpenAI Codex) and returns responses. Each Discord channel/user pair is a persistent session.

```
Discord
  │  (message)
  ▼
bot.py  ──────────────────► claude CLI  (CLAUDE_BIN, default)
  │                     or  codex-adapter.py  (via CLAUDE_BIN override)
  │                     or  openai-adapter.py
  │
  └─ sessions.json  (session IDs + turn counts + stopped flags)
  └─ memory/<ch>/<thread>/context.md  (flushed summaries)
```

## Components

### bot.py

Main process. Responsibilities:

| Area | Detail |
|---|---|
| Session key | Scope-dependent (`SESSION_SCOPE`): `thread` (default) → `th{thread_id}` inside threads (shared by all participants), `ch{channel_id}_u{user_id}` at channel top level; `channel` → `ch{channel_id}` (shared); `user` → `ch{channel_id}_u{user_id}` (legacy). Shared-scope messages are prefixed `[speaker]` before forwarding. Backend-agnostic: adapters only ever see `--resume <session_id>`. |
| Debounce | Buffers incoming messages for `DEBOUNCE_SECONDS` before sending to Claude |
| Rate limiting | `RATE_LIMIT_PER_MIN` per user (sliding window, in-memory) |
| Turn cap | `MAX_TURNS` per session; saved to disk |
| Stop / Done | `!stop` command or `[DONE]` token from Claude; saved to disk |
| Chunking | `chunk_text()` splits long replies while closing/reopening Markdown code fences |
| File upload | `[SEND_FILE:/path]` token in Claude output → `discord.File`; path validated against `SEND_FILE_ALLOWED_DIRS` + `SEND_FILE_ALLOWED_EXTS` |
| Memory flush | `!flush` summarises session and appends to `memory/<ch>/<thread>/context.md` |
| Session reset | `!reset` clears session_id, turn count, stopped flag |

**Persisted state** (`sessions.json` envelope, written atomically via temp file + `os.replace`):
- `sessions`: `{session_key → claude_session_id}`
- `turn_counts`: `{session_key → int}` — survives restart
- `stopped_sessions`: `[session_key, …]` — survives restart
- `session_models`: `{session_key → model}` — `!model` overrides survive restart

**Ephemeral state** (lost on restart, by design):
- `_rate_buckets`: sliding-window rate limiter
- `_pending` / `_pending_texts`: debounce buffer (messages mid-flight are dropped)
- `_pending_channel`: thread-redirect mapping

### codex-adapter.py

Wraps OpenAI Codex CLI (`codex exec`) to mimic the Claude Code JSON output format so `bot.py` needs zero changes. Manages its own session history at `~/.codex-sessions/<id>.json`. Activated by setting `CLAUDE_BIN=.../codex-adapter.py`.

### openai-adapter.py

Direct OpenAI API adapter (no CLI required). Same interface contract as `codex-adapter.py`.

## Configuration (.env)

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | — | Bot token (required) |
| `ALLOWED_CHANNEL_IDS` | (all) | Comma-separated channel IDs |
| `ALLOWED_USER_IDS` | (all) | Comma-separated user IDs |
| `BLOCKED_CHANNEL_IDS` | — | Deny-list channels |
| `BLOCKED_USER_IDS` | — | Deny-list users |
| `WORKING_DIR` | `~/agent-dev` | CWD for Claude subprocess |
| `CLAUDE_BIN` | `claude` | AI backend binary |
| `CLAUDE_EXTRA_ARGS` | `` | Extra args for every Claude call |
| `CLAUDE_TIMEOUT` | `120` | Subprocess timeout (seconds) |
| `RATE_LIMIT_PER_MIN` | `5` | Max requests/min per user |
| `MAX_TURNS_PER_SESSION` | `20` | Hard turn cap per session |
| `DEBOUNCE_SECONDS` | `2.5` | Message-buffer window |
| `SEND_FILE_ALLOWED_DIRS` | (WORKING_DIR) | Colon-separated dirs Claude may serve files from |
| `SEND_FILE_ALLOWED_EXTS` | `.png,.jpg,…,.log` | Comma-separated allowed file extensions |
| `LOG_FILE` | `~/discord-claude-bridge.log` | Log output path |

## Security model

- **SEND_FILE**: `_validate_send_file()` resolves `realpath`, checks the result is inside `SEND_FILE_ALLOWED_DIRS` (default: `WORKING_DIR`), and verifies the extension is in `SEND_FILE_ALLOWED_EXTS`. Paths that escape these constraints are silently dropped and logged as warnings.
- **Permissions**: `--dangerously-skip-permissions` is **not** a default; callers must set it explicitly via `CLAUDE_EXTRA_ARGS`. If it is set while `ALLOWED_USER_IDS` is empty, the bot refuses to start (`UNSAFE_ALLOW_ALL_USERS=1` overrides).
- **Channel/user guards**: evaluated before any AI call; both allow-list and deny-list supported.
- **Task state trust**: `fetch_task_state()` only accepts `📋 [TASK STATE]` messages authored by bots — a human pasting the marker cannot inject content into other sessions.
- **Attachments**: saved under `ATTACH_DIR` with a random prefix + basename only (no traversal, no cross-user overwrites); uploads over `ATTACH_MAX_BYTES` are skipped.
- **Model override**: `!model` accepts alias-listed models only.

## Runtime management (macOS)

Managed by launchd:

```bash
# Restart
launchctl kickstart -k gui/$(id -u)/com.capo.discord-claude-bridge

# Verify
pgrep -fla bot.py
tail -f logs/bridge.err
```

Plist: `~/Library/LaunchAgents/com.capo.discord-claude-bridge.plist`
