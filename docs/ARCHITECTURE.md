# Claude Terminal Add-on — Architecture & Current State

This document captures the full context of the Claude Terminal add-on's AI integration so that any future session can pick up where we left off.

## Current Architecture (v2.3.5)

```
┌─────────────────────────────────────────┐
│          Home Assistant Core            │
│                                         │
│  custom_components/claude_terminal/     │
│  ├── conversation.py (ConversationEntity)│
│  ├── api.py (HTTP client → add-on)      │
│  ├── config_flow.py                     │
│  ├── const.py (hostname discovery)      │
│  └── __init__.py                        │
│                                         │
│  Registers as:                          │
│  - Conversation agent (Assist pipeline) │
│  - (AI Task entity — planned)           │
└────────────┬────────────────────────────┘
             │ HTTP POST
             │ http://{hash}-claude-terminal:8099
             ▼
┌─────────────────────────────────────────┐
│       Claude Terminal Add-on            │
│                                         │
│  api-server.js (Node.js, port 8099)     │
│  ├── POST /api/query → spawns claude -p │
│  ├── POST /api/run-script (planned)     │
│  └── GET /api/health                    │
│                                         │
│  ttyd (port 7681, web terminal)         │
│  └── tmux → claude [--dangerously-...]  │
│                                         │
│  Runtime: Node.js + Python + claude CLI │
└─────────────────────────────────────────┘
```

## What Works Today

| Feature | Status | Notes |
|---|---|---|
| Web terminal (ttyd + tmux + claude) | Working | v2.3.5, tested on HA Green |
| YOLO mode (--dangerously-skip-permissions) | Configured | Via `IS_SANDBOX=1` + config toggle, NOT yet verified on HA |
| Agent teams env var | Configured | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, NOT yet verified |
| API server (api-server.js) | Deployed | Port 8099, `--` bug fixed |
| Custom integration auto-install | Deployed | Copies to `/config/custom_components/` on startup |
| Hostname discovery | Implemented | Writes `.addon_hostname` file, integration reads it |
| Conversation entity | Deployed | DNS error was fixed, but NOT yet verified end-to-end |

## What Needs to Change: Agent SDK Instead of `claude -p`

### The Problem

The current API server (`api-server.js`) spawns `claude -p "query" --output-format json` for each request. This is the Claude Code CLI in print mode. Issues:
1. It's a Node.js server spawning a Node.js CLI — unnecessary overhead
2. No proper session management beyond `--resume`
3. The `--` flag placement bug was hard to diagnose
4. Limited control over model, tools, and conversation flow

### The Solution: Claude Agent SDK (Python)

Replace `claude -p` with the Python Agent SDK (`claude_agent_sdk`). The SDK still spawns the `claude` CLI internally, but provides:
- Proper async message handling
- Model selection (`haiku`, `sonnet`, `opus`)
- System prompt control
- Permission modes (`bypassPermissions`)
- Session resumption
- Tool restrictions
- MCP server passthrough

### Proposed New Architecture

Replace the Node.js `api-server.js` with a Python `api-server.py` using `aiohttp`:

```
┌─────────────────────────────────────────┐
│       Claude Terminal Add-on            │
│                                         │
│  api-server.py (Python aiohttp, 8099)   │
│  ├── POST /api/query                    │
│  │   Uses claude_agent_sdk.query()      │
│  ├── POST /api/run-script               │
│  │   Runs /config/scripts/*.py          │
│  │   Or runs inline Python code         │
│  └── GET /api/health                    │
│                                         │
│  ttyd (port 7681, web terminal)         │
│  └── tmux → claude                      │
└─────────────────────────────────────────┘
```

### Agent SDK Usage Pattern

From the working `irrigation_advisor_claude.py`:

```python
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

async def call_claude(prompt, system_prompt):
    options = ClaudeAgentOptions(
        cli_path="/usr/local/bin/claude",
        max_turns=1,
        permission_mode="bypassPermissions",
        model="haiku",  # cheapest for simple queries
        system_prompt=system_prompt,
        env={"CLAUDE_CODE_OAUTH_TOKEN": oauth_token},
    )

    result_text = ""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            result_text = msg.result

    return result_text
```

### Authentication

**OAuth token flow:**
1. Token is `sk-ant-oat01-...` (NOT an API key — it's an OAuth token from Max/Pro plan)
2. Stored in `/config/secrets.yaml` as `claude_oauth_token`
3. Scripts read it directly from secrets.yaml (existing pattern)
4. Passed to Agent SDK via `env={"CLAUDE_CODE_OAUTH_TOKEN": token}`
5. The SDK passes it to the `claude` CLI which handles OAuth→API translation

**HA API access from scripts:**
- Add-on's `SUPERVISOR_TOKEN` env var works with `http://supervisor/core/api`
- HA Core's `SUPERVISOR_TOKEN` does NOT work for Core API (returns 401)
- For shell_commands from HA Core, use a long-lived JWT token + `http://localhost:8123/api`

### Files That Need to Change

| File | Change |
|---|---|
| `api-server.js` | **Replace** with `api-server.py` (Python aiohttp) |
| `run.sh` | Change `node /opt/scripts/api-server.js &` → `python3 /opt/scripts/api-server.py &` |
| `Dockerfile` | Add `RUN pip3 install --break-system-packages claude-agent-sdk` |
| `api.py` (custom integration) | No change — still calls HTTP endpoints |
| `conversation.py` | No change — still calls `api.async_query()` |
| `const.py` | No change |
| Tests | Rewrite Node.js tests as Python tests |

### /api/query — New Implementation

```python
# In api-server.py, the /api/query handler:
async def handle_query(request):
    body = await request.json()
    query_text = body["query"]
    context = body.get("context", {})
    conversation_id = body.get("conversation_id")

    system_prompt = build_system_prompt(context)
    oauth_token = read_oauth_token()  # from /config/secrets.yaml

    options = ClaudeAgentOptions(
        cli_path="/usr/local/bin/claude",
        max_turns=3,
        permission_mode="bypassPermissions",
        model="haiku",  # default, override via request
        system_prompt=system_prompt,
        env={"CLAUDE_CODE_OAUTH_TOKEN": oauth_token},
    )

    if conversation_id:
        options.resume = conversation_id

    result_text = ""
    session_id = None
    async for msg in query(prompt=query_text, options=options):
        if isinstance(msg, ResultMessage):
            result_text = msg.result
        elif isinstance(msg, SystemMessage) and msg.subtype == "init":
            session_id = msg.data.get("session_id")

    return web.json_response({
        "result": result_text,
        "session_id": session_id,
    })
```

### /api/run-script — New Implementation

```python
async def handle_run_script(request):
    body = await request.json()
    script = body.get("script")
    code = body.get("code")
    args = body.get("args", [])

    if script:
        # Validate: no path traversal, must be .py, must exist
        script_path = f"/config/scripts/{script}"
        cmd = ["python3", script_path] + args
    elif code:
        # Write to temp file, execute
        tmp = f"/tmp/claude_run_{uuid4().hex}.py"
        Path(tmp).write_text(code)
        cmd = ["python3", tmp] + args
    else:
        return web.json_response({"error": True, "message": "Provide script or code"}, status=400)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=PIPE, stderr=PIPE,
        env={**os.environ}
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

    # Try to parse last line as JSON for structured_output
    structured = None
    lines = stdout.decode().strip().splitlines()
    if lines:
        try:
            structured = json.loads(lines[-1])
        except json.JSONDecodeError:
            pass

    return web.json_response({
        "stdout": stdout.decode(),
        "stderr": stderr.decode(),
        "exit_code": proc.returncode,
        "structured_output": structured,
        "duration_ms": ...,
    })
```

## Assist Pipeline Integration

The custom integration registers as a **conversation agent** in HA. This means:

1. **Settings → Voice Assistants:** Users can select "Claude Terminal" as the conversation agent
2. **Assist panel:** Typing in the HA Assist dialog routes to Claude
3. **Voice satellites:** Alexa/Google/local satellites can use Claude for voice commands
4. **Automations:** `conversation.process` service targets the Claude agent

The `ConversationEntity` in `conversation.py`:
- Receives `ConversationInput` with: `text`, `context` (user_id), `device_id`, `satellite_id`, `language`, `extra_system_prompt`
- Resolves user/device names from IDs
- Builds context dict with source, user, device, language
- POSTs to the add-on's `/api/query` endpoint
- Returns `ConversationResult` with Claude's response

## Files Reference

```
claude-terminal/
├── config.yaml                          # Add-on config (v2.3.5)
├── Dockerfile                           # Alpine + Node.js + Python + claude CLI
├── run.sh                               # Startup: env, tools, MCP, integration install, API server, ttyd
├── scripts/
│   ├── api-server.js                    # Current API server (TO BE REPLACED with .py)
│   ├── claude-session-picker.sh         # Interactive session menu
│   ├── ha-context.sh                    # Generates CLAUDE.md with HA entity info
│   ├── setup-ha-mcp.sh                  # Configures ha-mcp for Claude
│   └── ...
├── custom_components/claude_terminal/   # HA custom integration (auto-installed)
│   ├── __init__.py                      # Forwards Platform.CONVERSATION
│   ├── conversation.py                  # ConversationEntity → HTTP → add-on
│   ├── api.py                           # HTTP client (ClaudeTerminalAPI)
│   ├── config_flow.py                   # "Add Integration" UI
│   ├── const.py                         # Constants + hostname discovery
│   ├── manifest.json                    # Integration metadata
│   └── strings.json                     # UI strings
├── tests/
│   ├── test-api-server.js               # Node.js tests (TO BE REPLACED)
│   ├── test-e2e.sh                      # E2E shell tests
│   └── test_integration/               # Python tests for custom integration
└── docs/
    └── superpowers/
        ├── specs/                       # Design specs
        └── plans/                       # Implementation plans
```

## Known Issues to Fix in Next Session

1. **`--` bug was fixed** but needs verification on real HA (v2.3.5 pushed but not tested)
2. **Conversation agent DNS** — hostname discovery implemented but end-to-end not verified
3. **YOLO mode** — `IS_SANDBOX=1` configured but not verified in interactive terminal
4. **Agent teams** — env var set but untested
5. **AI Task entity** — not yet registered (only `Platform.CONVERSATION` in __init__.py)
6. **Replace api-server.js with api-server.py** — the main work for next session
7. **Add /api/run-script endpoint** — for Agent SDK Python script execution
8. **Install claude-agent-sdk in Dockerfile** — `pip3 install --break-system-packages claude-agent-sdk`
