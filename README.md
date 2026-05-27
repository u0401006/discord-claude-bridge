# discord-claude-bridge

Discord bot that bridges channel messages to an AI backend (Claude Code or OpenAI Codex) and returns responses in-channel. Each Discord channel/user pair is a persistent, resumable session.

## Features

- **Multi-backend**: works with Claude Code CLI, OpenAI Codex CLI (`codex-adapter.py`), or direct OpenAI API (`openai-adapter.py`)
- **Persistent sessions**: conversation IDs, turn counts, and stopped flags survive bot restarts
- **Debounced input**: buffers rapid messages before forwarding, preventing duplicate requests
- **Long-message chunking**: splits replies that exceed Discord's 2000-char limit while keeping Markdown code fences intact
- **File upload**: Claude can emit `[SEND_FILE:/path]` tokens to attach files to Discord messages; paths are validated against a configurable allowlist
- **Rate limiting**: per-user request cap (default 5 req/min)
- **Turn cap**: hard limit per session (default 20 turns); `!reset` starts a fresh session
- **Memory flush**: `!flush` summarises the session and persists it to disk for future context
- **Bot-loop prevention**: auto-stops forwarding when Claude returns a `[DONE]` signal or pure punctuation in a bot-to-bot turn

## Quick start

```bash
git clone https://github.com/u0401006/discord-claude-bridge
cd discord-claude-bridge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # edit DISCORD_TOKEN, ALLOWED_CHANNEL_IDS, etc.
python3 bot.py --env .env
```

## Configuration

All settings are in the `.env` file (or environment variables):

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | — | Bot token (required) |
| `ALLOWED_CHANNEL_IDS` | (all) | Comma-separated channel IDs to accept |
| `ALLOWED_USER_IDS` | (all) | Comma-separated user IDs to accept |
| `BLOCKED_CHANNEL_IDS` | — | Deny-list channels |
| `BLOCKED_USER_IDS` | — | Deny-list users |
| `WORKING_DIR` | `~/agent-dev` | Working directory for the AI subprocess |
| `CLAUDE_BIN` | `claude` | AI backend binary path |
| `CLAUDE_EXTRA_ARGS` | `` | Extra flags passed to every AI call (e.g. `--dangerously-skip-permissions`) |
| `CLAUDE_TIMEOUT` | `120` | Subprocess timeout in seconds |
| `RATE_LIMIT_PER_MIN` | `5` | Max requests per user per minute |
| `MAX_TURNS_PER_SESSION` | `20` | Hard turn cap per session |
| `DEBOUNCE_SECONDS` | `2.5` | Message-buffer window before forwarding |
| `SEND_FILE_ALLOWED_DIRS` | `WORKING_DIR` | Colon-separated dirs Claude may serve files from |
| `SEND_FILE_ALLOWED_EXTS` | `.png,.jpg,…,.log` | Allowed file extensions for upload |
| `LOG_FILE` | `~/discord-claude-bridge.log` | Log output path |

## User commands

| Command | Effect |
|---|---|
| `!reset` | Clear session; start fresh |
| `!stop` | Stop bot from forwarding further replies in this session |
| `!flush` | Summarise session and persist to disk for future reference |

## Backends

### Claude Code (default)
```env
CLAUDE_BIN=claude
CLAUDE_EXTRA_ARGS=--dangerously-skip-permissions
```

### OpenAI Codex CLI
```env
CLAUDE_BIN=/path/to/discord-claude-bridge/codex-adapter.py
CLAUDE_EXTRA_ARGS=--model o4-mini
```
One-time auth: `printenv OPENAI_API_KEY | codex login --with-api-key`

### Direct OpenAI API
```env
CLAUDE_BIN=/path/to/discord-claude-bridge/openai-adapter.py
OPENAI_API_KEY=sk-...
```

## Running as a service (macOS)

```bash
cp com.capo.discord-claude-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.capo.discord-claude-bridge.plist
```

Restart: `launchctl kickstart -k gui/$(id -u)/com.capo.discord-claude-bridge`

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for component breakdown, security model, and state-persistence details.

## Tests

```bash
python3 -m unittest tests/test_bot.py -v
```
