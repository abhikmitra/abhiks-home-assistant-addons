# Claude Terminal v2.3.0 — Bypass Permissions, Agent Teams, AI Integration

## Overview

Three enhancements to the Claude Terminal add-on:

1. **Bypass permissions** — always run Claude Code with `--dangerously-skip-permissions`
2. **Agent teams** — enable experimental agent teams for multi-agent coordination
3. **AI integration** — expose Claude Code as a Home Assistant conversation agent and AI Task entity

## Feature 1: Bypass Permissions

Claude Code runs as a dedicated HA appliance. All `claude` invocations get `--dangerously-skip-permissions` appended. No user toggle — always on.

### Changes

**`run.sh`** — update `get_claude_launch_command()`:
- `'claude'` → `'claude --dangerously-skip-permissions'` in both auto-launch and fallback paths

**`claude-session-picker.sh`** — update all launch functions:
- `launch_claude_new()`: `'claude --dangerously-skip-permissions'`
- `launch_claude_continue()`: `'claude -c --dangerously-skip-permissions'`
- `launch_claude_resume()`: `'claude -r --dangerously-skip-permissions'`
- `launch_claude_custom()`: append `--dangerously-skip-permissions` to user-provided args

## Feature 2: Agent Teams

Enable the experimental agent teams feature so users can coordinate multiple Claude Code instances.

### Changes

**`run.sh`** — add to `init_environment()`:
```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

This runs before any Claude process starts. tmux is already installed (required for agent teams split-pane mode). Since `--dangerously-skip-permissions` is set on the lead, all spawned teammates inherit it.

## Feature 3: AI Integration

Expose Claude Code as both a **conversation agent** (for voice assistants and chat) and an **AI Task entity** (for automations) in Home Assistant.

### Architecture

Two components:

1. **API server** (Node.js, runs in the add-on container) — receives HTTP requests, spawns `claude -p` processes, returns responses
2. **Custom integration** (Python, auto-installed to HA) — registers with HA as conversation agent + AI Task entity, calls the API server

```
┌─────────────────────────┐     HTTP      ┌──────────────────────────┐
│   Home Assistant Core   │ ───────────── │   Claude Terminal Add-on │
│                         │  port 8099    │                          │
│  custom_components/     │               │  api-server.js           │
│    claude_terminal/     │               │    ↓                     │
│    - conversation.py    │               │  claude -p "query"       │
│    - ai_task.py         │               │    --dangerously-skip-   │
│                         │               │    permissions           │
│                         │               │    --output-format json  │
└─────────────────────────┘               └──────────────────────────┘
```

### API Server

**File:** `claude-terminal/scripts/api-server.js`
**Port:** 8099 (internal, Supervisor network only)
**Endpoint:** `POST /api/query`

**Request format:**
```json
{
  "query": "Turn off the living room lights",
  "conversation_id": "optional-session-id",
  "context": {
    "source": "conversation",
    "user_name": "Abhik",
    "device_name": "Kitchen Speaker",
    "satellite_name": "Kitchen Satellite",
    "language": "en",
    "extra_system_prompt": "optional extra prompt from HA pipeline"
  }
}
```

For AI Task calls:
```json
{
  "query": "Generate a morning briefing",
  "context": {
    "source": "ai_task",
    "task_name": "morning_briefing",
    "language": "en"
  }
}
```

**Execution:** Spawns `claude -p "query" --dangerously-skip-permissions --output-format json --append-system-prompt "<dynamic prompt>"` as a child process.

**Dynamic system prompt** (built from context):

For conversation:
```
You are responding via Home Assistant's conversation interface, not an interactive terminal.
Current time: 2026-03-23 14:30:00 Asia/Kolkata
User: Abhik
Triggered from device: Kitchen Speaker
Language: en

Be concise and action-oriented. When controlling devices, confirm what you did in one sentence.
```

For AI Task:
```
You are responding via Home Assistant's AI Task interface, not an interactive terminal.
Current time: 2026-03-23 06:00:00 Asia/Kolkata
Task: morning_briefing
Language: en

Structure your output clearly as it will be consumed by automations.
```

If `extra_system_prompt` is provided by HA (conversation only), it gets appended.

If `conversation_id` is provided, adds `--resume <id>` to continue a prior conversation.

**Response:** Returns Claude's JSON directly — includes `result` (text), `session_id` (for follow-ups), `total_cost_usd`, and `usage`.

**Timeout:** 120 seconds default.

**No authentication needed** — only reachable from containers on the HA Supervisor network, not exposed externally.

### Custom Integration

**Directory:** `claude-terminal/custom_components/claude_terminal/`

**Files:**
- `__init__.py` — integration setup
- `manifest.json` — metadata (domain: `claude_terminal`, dependencies: `[hassio]`)
- `config_flow.py` — simple "Add Integration" flow, no config needed
- `conversation.py` — implements `ConversationEntity` with `_async_handle_message()`
- `ai_task.py` — implements `AITaskEntity` with `_async_generate_data()`
- `const.py` — constants (addon URL, timeouts)

**Conversation entity (`conversation.py`):**
- Receives `ConversationInput` with: `text`, `context` (has `user_id`), `conversation_id`, `device_id`, `satellite_id`, `language`, `extra_system_prompt`
- Resolves `user_id` → user display name via `hass.auth.async_get_user()`
- Resolves `device_id` and `satellite_id` → device names via device registry
- POSTs to `http://<addon_hostname>:8099/api/query` with full context
- Returns `ConversationResult` with Claude's response text and `session_id` as `conversation_id`

**AI Task entity (`ai_task.py`):**
- Receives `GenDataTask` with: `name`, `instructions`, `structure`, `attachments`
- Note: AI Task gets `context=None` and `device_id=None` from HA (HA limitation) — no user or device info available
- POSTs to `http://<addon_hostname>:8099/api/query`
- Returns `GenDataTaskResult` with Claude's response and `session_id`
- If `structure` is provided, passes `--json-schema` to Claude for structured output

**Add-on hostname:** Within the HA Supervisor network, add-ons are addressable by their slug-based hostname. The integration discovers this at runtime via the Supervisor API.

### Auto-Installation

**`run.sh`** — new `install_custom_integration()` function:
- Copies `custom_components/claude_terminal/` to `/config/custom_components/claude_terminal/`
- Only copies if files are missing or the add-on version has changed (checks a version marker file)
- Logs a notice on first install: "Please restart Home Assistant to load the Claude Terminal integration"

### Startup Flow Changes

Updated `main()` in `run.sh`:
```
1.  run_health_check
2.  init_environment           ← adds CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
3.  install_tools
4.  setup_session_picker
5.  install_persistent_packages
6.  generate_ha_context
7.  setup_ha_mcp
8.  install_custom_integration ← NEW
9.  start_api_server           ← NEW (background, port 8099)
10. start_web_terminal         ← existing (ttyd, port 7681)
```

### config.yaml Changes

- Add port `8099/tcp` for the API server (Supervisor network, not user-facing)
- Version bump: 2.2.0 → 2.3.0

## User-Facing Installation

The existing installation process does not change. For the new AI integration features, one additional step after updating:

1. Restart Home Assistant (one-time, so HA picks up the new integration)
2. Go to **Settings** → **Devices & Services** → **Add Integration**
3. Search for **Claude Terminal** and add it
4. (Optional) To use as a voice assistant: **Settings** → **Voice Assistants** → select Claude Terminal as your conversation agent

### Use in Automations

```yaml
action: ai_task.generate_data
data:
  task_name: "morning_briefing"
  instructions: "What lights are on and what's the temperature?"
  entity_id: ai_task.claude_terminal
```

## Context Available to Claude

Claude `-p` calls (without `--bare`) load the same context as interactive sessions:

1. **`$HOME/CLAUDE.md`** — HA context generated by `ha-context.sh` (entity counts, installed add-ons, error logs, API examples)
2. **ha-mcp server** — configured via `claude mcp add`, provides 97+ HA tools (entity control, automations, dashboards, etc.)
3. **Dynamic system prompt** — source, user, device, time, language (built from trigger context)

## Verified Against

- Claude Code CLI `-p` mode: tested, returns structured JSON, blocks until complete (~3.5s for simple queries)
- HA `ConversationInput` fields: verified against `homeassistant/components/conversation/models.py` in HA core
- HA `GenDataTask` fields: verified against `homeassistant/components/ai_task/task.py` in HA core
- HA `Context` class: verified against `homeassistant/core.py` — has `user_id`, `parent_id`, `id`
- AI Task limitation: entity code passes `context=None`, `device_id=None` to LLM context (no user/device info)
