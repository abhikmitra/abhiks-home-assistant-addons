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

**File:** `claude-terminal/scripts/api-server.js` (uses Node.js built-in `http` module — no extra npm dependencies needed)
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

**Multi-turn conversations:** If `conversation_id` is provided, adds `--resume <id>` to continue a prior conversation. Known limitation: `--resume` replays the full conversation history on each call, so latency and cost grow with each turn. To mitigate:
- Cap at 10 turns per conversation, then start fresh
- Use `--no-session-persistence` for AI Task calls (one-shot by nature)
- Document as a known limitation for v2.3.0

**Response:** The API server extracts the response from Claude's JSON output:
- For normal calls: reads the `result` field (text response)
- For structured output (`--json-schema`): reads the `structured_output` field instead (`result` is empty when schema is used)
- Always returns `session_id` for conversation continuity

**Concurrency:** One request at a time. The API server queues incoming requests and processes them sequentially. Each `claude -p` process loads Node.js + Claude Code runtime, which is memory-intensive. On resource-constrained hardware (HA Green, 1GB RAM), concurrent processes would OOM. Additional requests receive a 503 with "busy" message.

**Error handling:** If Claude fails (API key expired, rate limit, timeout, malformed response), the API server returns a structured error:
```json
{"error": true, "message": "Rate limited, please try again", "code": 429}
```
The custom integration catches these and returns a user-friendly `ConversationResult`/`GenDataTaskResult` (e.g., "I'm sorry, I couldn't process that request") while logging the actual error.

**Timeout:** 120 seconds default.

**No authentication needed** — only reachable from containers on the HA Supervisor network, not exposed externally.

### Custom Integration

**Directory:** `claude-terminal/custom_components/claude_terminal/`

**Files:**
- `__init__.py` — integration setup
- `manifest.json` — metadata (domain: `claude_terminal`, dependencies: `[hassio]`)
- `config_flow.py` — simple "Add Integration" flow, no config needed
- `conversation.py` — implements `ConversationEntity` with `_async_handle_message()`
- `ai_task.py` — implements `AITaskEntity` with `_async_generate_data(task, chat_log)`
- `const.py` — constants (addon URL, timeouts)

**Design choice:** Both `_async_handle_message` and `_async_generate_data` receive a `ChatLog` parameter from HA. We intentionally bypass HA's built-in LLM conversation management — the integration does not use `chat_log` to manage context. Instead, all context is managed by Claude Code's own session system via `-p` and `--resume`. The `ChatLog` parameter is accepted but not used.

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

**Add-on hostname:** The HA Supervisor derives hostnames from the slug by replacing underscores with hyphens. Since slug is `claude_terminal`, the hostname is `claude-terminal`. The API URL is `http://claude-terminal:8099/api/query`. This is hardcoded in `const.py` as a fallback, with runtime discovery via the Supervisor API as the primary method.

### Auto-Installation

**`run.sh`** — new `install_custom_integration()` function:
- Copies `custom_components/claude_terminal/` to `/config/custom_components/claude_terminal/`
- Only copies if files are missing or the add-on version has changed (checks a version marker file)
- Always overwrites on version mismatch (ensures corrupted files get replaced on next add-on update)
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

- Do NOT add port 8099 to `ports` — the `ports` section controls host-level port publishing, and adding it would expose the API externally. The port is already accessible on the internal Supervisor Docker bridge network without declaration.
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
target:
  entity_id: ai_task.claude_terminal
data:
  task_name: "morning_briefing"
  instructions: "What lights are on and what's the temperature?"
```

## Context Available to Claude

Claude `-p` calls (without `--bare`) load the same context as interactive sessions:

1. **`$HOME/CLAUDE.md`** — HA context generated by `ha-context.sh` (entity counts, installed add-ons, error logs, API examples)
2. **ha-mcp server** — configured via `claude mcp add`, provides 97+ HA tools (entity control, automations, dashboards, etc.)
3. **Dynamic system prompt** — source, user, device, time, language (built from trigger context)

## API Server Resilience

The API server runs as a background process before `exec ttyd`. Since `exec` replaces the shell, the API server must be self-sustaining:
- Started with `node /opt/scripts/api-server.js &` (background job)
- The server includes a self-restart mechanism: on uncaught exception, it logs the error and restarts after 5 seconds
- A `GET /api/health` endpoint returns 200 if the server is alive (usable by HA for diagnostics)
- If the entire container restarts (add-on restart), both ttyd and the API server restart via `run.sh`

## Cost Safety

Since `--dangerously-skip-permissions` is always on and the API server accepts requests from automations, a misconfigured automation could trigger expensive Claude calls in a loop. Mitigations:
- API server enforces a rate limit: max 10 requests per minute (configurable later)
- Consider adding `--max-budget-usd` to `-p` calls as a per-request safety cap (implementation can decide the default)
- The conversation entity logs cost from Claude's response JSON for monitoring

## Verified Against

- Claude Code CLI `-p` mode: tested, returns structured JSON, blocks until complete (~3.5s for simple queries)
- HA `ConversationInput` fields: verified against `homeassistant/components/conversation/models.py` in HA core
- HA `GenDataTask` fields: verified against `homeassistant/components/ai_task/task.py` in HA core
- HA `Context` class: verified against `homeassistant/core.py` — has `user_id`, `parent_id`, `id`
- AI Task limitation: entity code passes `context=None`, `device_id=None` to LLM context (no user/device info)
