# Agent SDK Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Node.js API server (`api-server.js` using `claude -p`) with a Python API server (`api-server.py` using the Claude Agent SDK), add script execution endpoint, and add OAuth token config option.

**Architecture:** Python `aiohttp` server runs alongside ttyd. Uses `claude_agent_sdk.query()` for conversation/AI Task requests. Uses `subprocess` for `/api/run-script`. Auth via config option → env var → stored CLI session fallback chain.

**Tech Stack:** Python 3.12, aiohttp, claude-agent-sdk, asyncio

**Spec:** `docs/ARCHITECTURE.md` (architecture doc) and `docs/superpowers/specs/2026-03-23-claude-terminal-enhancements-design.md`

**Reference:** Working Agent SDK example at `/Users/abhikmitra/Github/home-assistant-configs/scripts/irrigation_advisor_claude.py`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `claude-terminal/scripts/api-server.py` | Create | Python aiohttp API server with Agent SDK |
| `claude-terminal/scripts/api-server.js` | Delete | Replaced by Python version |
| `claude-terminal/Dockerfile` | Modify | Add `pip3 install claude-agent-sdk` |
| `claude-terminal/config.yaml` | Modify | Add `claude_oauth_token` config option, bump version |
| `claude-terminal/run.sh` | Modify | Read token from config, change `node` → `python3`, export token |
| `claude-terminal/tests/test-api-server.py` | Create | Python pytest tests for the new API server |
| `claude-terminal/tests/test-api-server.js` | Delete | Replaced by Python tests |
| `claude-terminal/tests/test-e2e.sh` | Modify | Update API server checks |
| `claude-terminal/custom_components/claude_terminal/manifest.json` | Modify | Bump version |
| `claude-terminal/README.md` | Modify | Update auth section |

---

### Task 1: Add `claude_oauth_token` Config Option

**Files:**
- Modify: `claude-terminal/config.yaml`
- Modify: `claude-terminal/run.sh:35-40` (init_environment)

- [ ] **Step 1: Add config option to config.yaml**

In `config.yaml`, add `claude_oauth_token` to both `options` and `schema`:

```yaml
options:
  auto_launch_claude: true
  dangerously_skip_permissions: true
  claude_oauth_token: ""
  ha_smart_context: true
  enable_ha_mcp: true
  persistent_apk_packages: []
  persistent_pip_packages: []
schema:
  auto_launch_claude: bool?
  dangerously_skip_permissions: bool?
  claude_oauth_token: str?
  ha_smart_context: bool?
  enable_ha_mcp: bool?
  persistent_apk_packages:
    - str
  persistent_pip_packages:
    - str
```

- [ ] **Step 2: Read and export token in run.sh init_environment()**

After the `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` export, add:

```bash
    # Claude OAuth token for Agent SDK (optional — falls back to stored CLI session)
    local claude_oauth_token
    claude_oauth_token=$(bashio::config 'claude_oauth_token' '')
    if [ -n "$claude_oauth_token" ]; then
        export CLAUDE_CODE_OAUTH_TOKEN="$claude_oauth_token"
        bashio::log.info "  - Claude OAuth token: set from add-on config"
    else
        bashio::log.info "  - Claude OAuth token: not configured (using stored CLI session or secrets.yaml)"
    fi
```

- [ ] **Step 3: Verify syntax**

Run: `bash -n claude-terminal/run.sh`
Expected: No output

- [ ] **Step 4: Commit**

```bash
git add claude-terminal/config.yaml claude-terminal/run.sh
git commit -m "feat: add claude_oauth_token config option for Agent SDK auth"
```

---

### Task 2: Install `claude-agent-sdk` in Dockerfile

**Files:**
- Modify: `claude-terminal/Dockerfile`

- [ ] **Step 1: Add pip install after the uv install**

After `RUN apk add --no-cache uv`, add:

```dockerfile
# Install Claude Agent SDK for Python API server
RUN pip3 install --break-system-packages claude-agent-sdk
```

- [ ] **Step 2: Verify Dockerfile syntax**

Run: `podman build --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base:3.21 -t local/claude-terminal:test ./claude-terminal`

Expected: Build succeeds, `claude-agent-sdk` installed

- [ ] **Step 3: Verify SDK is available in container**

Run: `podman run --rm local/claude-terminal:test python3 -c "import claude_agent_sdk; print('SDK OK')"`

Expected: `SDK OK`

- [ ] **Step 4: Commit**

```bash
git add claude-terminal/Dockerfile
git commit -m "feat: install claude-agent-sdk in Dockerfile"
```

---

### Task 3: Create Python API Server

**Files:**
- Create: `claude-terminal/scripts/api-server.py`

This replaces `api-server.js`. Uses `aiohttp` web server + `claude_agent_sdk.query()`.

- [ ] **Step 1: Create api-server.py**

Create `claude-terminal/scripts/api-server.py`:

```python
#!/usr/bin/env python3
"""Claude Terminal API Server.

Python aiohttp server that uses the Claude Agent SDK for /api/query
and subprocess for /api/run-script. Runs alongside ttyd on port 8099.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from aiohttp import web

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_PORT = 8099
API_HOST = "0.0.0.0"
QUERY_TIMEOUT_S = 120
SCRIPT_TIMEOUT_S = 300
RATE_LIMIT_WINDOW_S = 60
RATE_LIMIT_MAX = 10
MAX_BODY_BYTES = 1_048_576  # 1 MB

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[API] %(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("api-server")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

busy = False
request_timestamps: list[float] = []


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

def is_rate_limited() -> bool:
    """Check if rate limit exceeded. Prunes old timestamps."""
    now = time.time()
    while request_timestamps and request_timestamps[0] <= now - RATE_LIMIT_WINDOW_S:
        request_timestamps.pop(0)
    return len(request_timestamps) >= RATE_LIMIT_MAX


# ---------------------------------------------------------------------------
# OAuth Token
# ---------------------------------------------------------------------------

def get_oauth_token() -> str | None:
    """Resolve Claude OAuth token from env, then secrets.yaml fallback."""
    # 1. Environment variable (set by run.sh from add-on config)
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        log.info("OAuth token source: environment variable")
        return token

    # 2. Fallback: read from /config/secrets.yaml
    secrets_path = Path("/config/secrets.yaml")
    if secrets_path.exists():
        try:
            for line in secrets_path.read_text().splitlines():
                if line.startswith("claude_oauth_token:"):
                    token = line.split(":", 1)[1].strip().strip("\"'")
                    if token:
                        log.info("OAuth token source: /config/secrets.yaml")
                        return token
        except Exception as e:
            log.warning("Failed to read secrets.yaml: %s", e)

    # 3. No explicit token — Agent SDK will use stored CLI session
    log.info("No explicit OAuth token — Agent SDK will use stored CLI session")
    return None


# ---------------------------------------------------------------------------
# System Prompt Builder
# ---------------------------------------------------------------------------

def build_system_prompt(context: dict | None) -> str:
    """Build a dynamic system prompt from request context."""
    if not context:
        context = {}

    now = datetime.now().strftime("%A %d %B %Y, %H:%M:%S %Z")
    source = context.get("source", "conversation")
    language = context.get("language", "en")
    parts = []

    if source == "ai_task":
        parts.append("You are responding via Home Assistant's AI Task interface, not an interactive terminal.")
        parts.append(f"Current time: {now}")
        if context.get("task_name"):
            parts.append(f"Task: {context['task_name']}")
        parts.append(f"Language: {language}")
        parts.append("")
        parts.append("Structure your output clearly as it will be consumed by automations.")
    else:
        parts.append("You are responding via Home Assistant's conversation interface, not an interactive terminal.")
        parts.append(f"Current time: {now}")
        if context.get("user_name"):
            parts.append(f"User: {context['user_name']}")
        if context.get("device_name"):
            parts.append(f"Triggered from device: {context['device_name']}")
        if context.get("satellite_name"):
            parts.append(f"Satellite: {context['satellite_name']}")
        parts.append(f"Language: {language}")
        parts.append("")
        parts.append("Be concise and action-oriented. When controlling devices, confirm what you did in one sentence.")

    if context.get("extra_system_prompt"):
        parts.append("")
        parts.append(context["extra_system_prompt"])

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Agent SDK Query
# ---------------------------------------------------------------------------

async def run_agent_query(prompt: str, context: dict | None, conversation_id: str | None) -> dict:
    """Run a query using the Claude Agent SDK."""
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, SystemMessage

    system_prompt = build_system_prompt(context)
    oauth_token = get_oauth_token()

    options_kwargs = {
        "cli_path": "/usr/local/bin/claude",
        "max_turns": 3,
        "permission_mode": "bypassPermissions",
        "model": "haiku",
        "system_prompt": system_prompt,
    }

    # Pass OAuth token if explicitly available; otherwise let CLI use stored session
    if oauth_token:
        options_kwargs["env"] = {"CLAUDE_CODE_OAUTH_TOKEN": oauth_token}

    if conversation_id:
        options_kwargs["resume"] = conversation_id

    options = ClaudeAgentOptions(**options_kwargs)

    result_text = ""
    session_id = None
    start_time = time.time()

    log.info("Starting Agent SDK query: prompt_length=%d, has_conversation_id=%s", len(prompt), bool(conversation_id))

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage):
                result_text = msg.result or ""
                log.info("Got ResultMessage: length=%d, stop_reason=%s", len(result_text), getattr(msg, 'stop_reason', 'unknown'))
            elif isinstance(msg, SystemMessage) and getattr(msg, 'subtype', '') == "init":
                session_id = getattr(msg, 'data', {}).get("session_id")
                log.info("Got session_id: %s", session_id)
    except Exception as e:
        # SDK may throw on rate_limit_event — if we have result text, that's OK
        if not result_text:
            raise
        log.warning("Agent SDK non-fatal error (result already collected): %s", e)

    duration_ms = int((time.time() - start_time) * 1000)
    log.info("Agent SDK query complete: duration=%dms, result_length=%d, session_id=%s", duration_ms, len(result_text), session_id)

    return {
        "result": result_text,
        "session_id": session_id,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Script Runner
# ---------------------------------------------------------------------------

async def run_script(script: str | None, code: str | None, args: list[str]) -> dict:
    """Execute a Python script or inline code."""
    start_time = time.time()

    if script:
        # Validate: no traversal, must be .py, must exist
        if ".." in script or script.startswith("/") or not script.endswith(".py"):
            raise ValueError(f"Invalid script path: {script}")
        script_path = Path("/config/scripts") / script
        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script}")
        cmd = ["python3", str(script_path)] + args
        log.info("Running script: %s %s", script, " ".join(args))
    elif code:
        tmp_path = Path(f"/tmp/claude_run_{uuid.uuid4().hex}.py")
        tmp_path.write_text(code)
        cmd = ["python3", str(tmp_path)] + args
        log.info("Running inline code: %d chars", len(code))
    else:
        raise ValueError("Provide 'script' or 'code'")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=SCRIPT_TIMEOUT_S
        )
    finally:
        # Clean up temp file for inline code
        if code:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    exit_code = proc.returncode or 0
    duration_ms = int((time.time() - start_time) * 1000)

    # Try to parse last line as JSON for structured_output
    structured_output = None
    lines = stdout.strip().splitlines()
    if lines:
        try:
            structured_output = json.loads(lines[-1])
        except (json.JSONDecodeError, ValueError):
            pass

    log.info(
        "Script complete: exit_code=%d, duration=%dms, stdout=%d chars, stderr=%d chars, has_structured=%s",
        exit_code, duration_ms, len(stdout), len(stderr), structured_output is not None,
    )

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "structured_output": structured_output,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Request Handlers
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    """GET /api/health"""
    log.debug("Health check")
    return web.json_response({"status": "ok", "busy": busy})


async def handle_query(request: web.Request) -> web.Response:
    """POST /api/query — run a Claude Agent SDK query."""
    global busy
    request_id = uuid.uuid4().hex[:8]

    log.info("[%s] Query request received", request_id)

    if is_rate_limited():
        log.warning("[%s] Rate limited", request_id)
        return web.json_response(
            {"error": True, "message": "Rate limit exceeded. Max 10 requests per minute.", "code": 429},
            status=429,
        )

    if busy:
        log.warning("[%s] Busy", request_id)
        return web.json_response(
            {"error": True, "message": "Another request is currently being processed.", "code": 503},
            status=503,
        )

    # Read body with size limit
    body_bytes = await request.content.read(MAX_BODY_BYTES + 1)
    if len(body_bytes) > MAX_BODY_BYTES:
        return web.json_response(
            {"error": True, "message": f"Request body exceeds {MAX_BODY_BYTES} byte limit", "code": 413},
            status=413,
        )

    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return web.json_response(
            {"error": True, "message": "Invalid JSON in request body", "code": 400},
            status=400,
        )

    query_text = body.get("query")
    if not query_text:
        return web.json_response(
            {"error": True, "message": "Missing required field: query", "code": 400},
            status=400,
        )

    context = body.get("context")
    conversation_id = body.get("conversation_id")

    log.info(
        "[%s] Processing query: source=%s, query_length=%d, has_conversation_id=%s",
        request_id,
        context.get("source", "unknown") if context else "unknown",
        len(query_text),
        bool(conversation_id),
    )

    busy = True
    request_timestamps.append(time.time())

    try:
        result = await asyncio.wait_for(
            run_agent_query(query_text, context, conversation_id),
            timeout=QUERY_TIMEOUT_S,
        )
        log.info("[%s] Query complete: session_id=%s, result_length=%d", request_id, result.get("session_id"), len(result.get("result", "")))
        return web.json_response(result)
    except asyncio.TimeoutError:
        log.error("[%s] Query timed out after %ds", request_id, QUERY_TIMEOUT_S)
        return web.json_response(
            {"error": True, "message": f"Query timed out after {QUERY_TIMEOUT_S} seconds", "code": 504},
            status=504,
        )
    except Exception as e:
        log.error("[%s] Query failed: %s", request_id, e, exc_info=True)
        return web.json_response(
            {"error": True, "message": str(e), "code": 500},
            status=500,
        )
    finally:
        busy = False


async def handle_run_script(request: web.Request) -> web.Response:
    """POST /api/run-script — execute a Python script or inline code."""
    global busy
    request_id = uuid.uuid4().hex[:8]

    log.info("[%s] Run-script request received", request_id)

    if is_rate_limited():
        log.warning("[%s] Rate limited", request_id)
        return web.json_response(
            {"error": True, "message": "Rate limit exceeded.", "code": 429},
            status=429,
        )

    if busy:
        log.warning("[%s] Busy", request_id)
        return web.json_response(
            {"error": True, "message": "Another request is currently being processed.", "code": 503},
            status=503,
        )

    body_bytes = await request.content.read(MAX_BODY_BYTES + 1)
    if len(body_bytes) > MAX_BODY_BYTES:
        return web.json_response(
            {"error": True, "message": f"Request body exceeds {MAX_BODY_BYTES} byte limit", "code": 413},
            status=413,
        )

    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return web.json_response(
            {"error": True, "message": "Invalid JSON", "code": 400},
            status=400,
        )

    script = body.get("script")
    code = body.get("code")
    args = body.get("args", [])

    if not script and not code:
        return web.json_response(
            {"error": True, "message": "Provide 'script' or 'code' field", "code": 400},
            status=400,
        )

    if script and code:
        return web.json_response(
            {"error": True, "message": "Provide 'script' or 'code', not both", "code": 400},
            status=400,
        )

    log.info("[%s] Running: %s", request_id, script or "[inline code]")

    busy = True
    request_timestamps.append(time.time())

    try:
        result = await run_script(script, code, args)
        log.info("[%s] Script complete: exit_code=%d, duration=%dms", request_id, result["exit_code"], result["duration_ms"])
        return web.json_response(result)
    except FileNotFoundError as e:
        log.error("[%s] Script not found: %s", request_id, e)
        return web.json_response(
            {"error": True, "message": str(e), "code": 404},
            status=404,
        )
    except ValueError as e:
        log.error("[%s] Invalid script: %s", request_id, e)
        return web.json_response(
            {"error": True, "message": str(e), "code": 400},
            status=400,
        )
    except asyncio.TimeoutError:
        log.error("[%s] Script timed out after %ds", request_id, SCRIPT_TIMEOUT_S)
        return web.json_response(
            {"error": True, "message": f"Script timed out after {SCRIPT_TIMEOUT_S} seconds", "code": 504},
            status=504,
        )
    except Exception as e:
        log.error("[%s] Script failed: %s", request_id, e, exc_info=True)
        return web.json_response(
            {"error": True, "message": str(e), "code": 500},
            status=500,
        )
    finally:
        busy = False


# ---------------------------------------------------------------------------
# App Setup
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    """Create the aiohttp application."""
    app = web.Application()
    app.router.add_get("/api/health", handle_health)
    app.router.add_post("/api/query", handle_query)
    app.router.add_post("/api/run-script", handle_run_script)
    return app


if __name__ == "__main__":
    log.info("Starting Claude Terminal API server on %s:%d", API_HOST, API_PORT)
    log.info("Endpoints: POST /api/query, POST /api/run-script, GET /api/health")
    oauth = get_oauth_token()
    log.info("OAuth token: %s", "configured" if oauth else "not configured (using stored CLI session)")
    app = create_app()
    web.run_app(app, host=API_HOST, port=API_PORT, print=None)
```

- [ ] **Step 2: Verify Python syntax**

Run: `python3 -c "import ast; ast.parse(open('claude-terminal/scripts/api-server.py').read())" && echo "OK"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add claude-terminal/scripts/api-server.py
git commit -m "feat: add Python API server using Claude Agent SDK"
```

---

### Task 4: Write Python API Server Tests

**Files:**
- Create: `claude-terminal/tests/test-api-server.py`

- [ ] **Step 1: Create test file**

Create `claude-terminal/tests/test-api-server.py`:

```python
"""Tests for the Python API server."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

# Add scripts dir to path so we can import api-server
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


class TestBuildSystemPrompt:
    """Test system prompt builder."""

    def setup_method(self):
        # Import from api-server.py (has hyphen, use importlib)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "api_server",
            Path(__file__).parent.parent / "scripts" / "api-server.py",
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_conversation_prompt_includes_fields(self):
        prompt = self.mod.build_system_prompt({
            "source": "conversation",
            "user_name": "Abhik",
            "device_name": "Kitchen Speaker",
            "language": "en",
        })
        assert "conversation interface" in prompt
        assert "User: Abhik" in prompt
        assert "Kitchen Speaker" in prompt
        assert "concise and action-oriented" in prompt

    def test_ai_task_prompt(self):
        prompt = self.mod.build_system_prompt({
            "source": "ai_task",
            "task_name": "morning_briefing",
            "language": "en",
        })
        assert "AI Task interface" in prompt
        assert "Task: morning_briefing" in prompt
        assert "consumed by automations" in prompt

    def test_extra_system_prompt_appended(self):
        prompt = self.mod.build_system_prompt({
            "source": "conversation",
            "extra_system_prompt": "Only respond in Spanish",
        })
        assert "Only respond in Spanish" in prompt

    def test_empty_context(self):
        prompt = self.mod.build_system_prompt({})
        assert isinstance(prompt, str)
        assert "conversation interface" in prompt

    def test_none_context(self):
        prompt = self.mod.build_system_prompt(None)
        assert isinstance(prompt, str)

    def test_includes_current_time(self):
        prompt = self.mod.build_system_prompt({"source": "conversation"})
        # Should contain a date-like string
        assert "202" in prompt  # year


class TestRateLimiting:
    """Test rate limiting logic."""

    def setup_method(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "api_server",
            Path(__file__).parent.parent / "scripts" / "api-server.py",
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.mod.request_timestamps.clear()

    def test_not_limited_when_empty(self):
        assert self.mod.is_rate_limited() is False

    def test_limited_after_max(self):
        now = time.time()
        for _ in range(self.mod.RATE_LIMIT_MAX):
            self.mod.request_timestamps.append(now)
        assert self.mod.is_rate_limited() is True


class TestGetOAuthToken:
    """Test OAuth token resolution."""

    def setup_method(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "api_server",
            Path(__file__).parent.parent / "scripts" / "api-server.py",
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_env_var_takes_priority(self):
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}):
            assert self.mod.get_oauth_token() == "test-token"

    def test_returns_none_when_no_source(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                assert self.mod.get_oauth_token() is None


class TestScriptValidation:
    """Test script path validation."""

    def setup_method(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "api_server",
            Path(__file__).parent.parent / "scripts" / "api-server.py",
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    @pytest.mark.asyncio
    async def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid script path"):
            await self.mod.run_script("../../../etc/passwd", None, [])

    @pytest.mark.asyncio
    async def test_rejects_absolute_path(self):
        with pytest.raises(ValueError, match="Invalid script path"):
            await self.mod.run_script("/etc/passwd", None, [])

    @pytest.mark.asyncio
    async def test_rejects_non_python(self):
        with pytest.raises(ValueError, match="Invalid script path"):
            await self.mod.run_script("script.sh", None, [])

    @pytest.mark.asyncio
    async def test_rejects_missing_script(self):
        with pytest.raises(FileNotFoundError):
            await self.mod.run_script("nonexistent.py", None, [])

    @pytest.mark.asyncio
    async def test_requires_script_or_code(self):
        with pytest.raises(ValueError, match="Provide"):
            await self.mod.run_script(None, None, [])
```

- [ ] **Step 2: Run tests**

Run: `cd claude-terminal && PYTHONPATH=scripts python3 -m pytest tests/test-api-server.py -v`

Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add claude-terminal/tests/test-api-server.py
git commit -m "test: add Python API server tests"
```

---

### Task 5: Update run.sh to Use Python Server

**Files:**
- Modify: `claude-terminal/run.sh:449-470` (start_api_server function)

- [ ] **Step 1: Update start_api_server()**

Change the function to use Python instead of Node.js:

```bash
start_api_server() {
    local api_script="/opt/scripts/api-server.py"

    if [ ! -f "$api_script" ]; then
        bashio::log.warning "API server script not found at $api_script, skipping"
        return 0
    fi

    bashio::log.info "Starting Claude Terminal API server (Python)..."
    bashio::log.info "  Script: $api_script"
    bashio::log.info "  Port: 8099"
    bashio::log.info "  Rate limit: 10 requests/minute"
    bashio::log.info "  Endpoints: /api/query, /api/run-script, /api/health"

    # Start in background - output goes to container logs
    python3 "$api_script" &
    local api_pid=$!

    bashio::log.info "API server started (PID: $api_pid)"

    # Wait briefly and check it's still running
    sleep 2
    if kill -0 "$api_pid" 2>/dev/null; then
        bashio::log.info "API server is running (PID: $api_pid)"
    else
        bashio::log.error "API server failed to start! Check logs above for errors."
        bashio::log.error "Continuing without API server - conversation/AI Task will not work."
    fi
}
```

- [ ] **Step 2: Verify syntax**

Run: `bash -n claude-terminal/run.sh`

- [ ] **Step 3: Commit**

```bash
git add claude-terminal/run.sh
git commit -m "feat: switch API server from Node.js to Python"
```

---

### Task 6: Remove Old Node.js API Server and Tests

**Files:**
- Delete: `claude-terminal/scripts/api-server.js`
- Delete: `claude-terminal/tests/test-api-server.js`

- [ ] **Step 1: Remove files**

```bash
git rm claude-terminal/scripts/api-server.js claude-terminal/tests/test-api-server.js
```

- [ ] **Step 2: Commit**

```bash
git commit -m "chore: remove Node.js API server replaced by Python version"
```

---

### Task 7: Update E2E Tests

**Files:**
- Modify: `claude-terminal/tests/test-e2e.sh`

- [ ] **Step 1: Update API server syntax check**

Replace the Node.js syntax check with Python:

```bash
# Test 4: API server syntax
echo "Test 4: API server syntax"
API_FILE="$BASE_DIR/scripts/api-server.py"
if [ -f "$API_FILE" ]; then
    if python3 -c "import ast; ast.parse(open('$API_FILE').read())" 2>/dev/null; then
        pass "api-server.py has valid syntax"
    else
        fail "api-server.py has syntax errors"
    fi
else
    skip "api-server.py not found"
fi
```

- [ ] **Step 2: Update API server unit tests check**

Replace Node.js test run with Python:

```bash
# Test 5: API server unit tests
echo "Test 5: API server unit tests"
TEST_FILE="$BASE_DIR/tests/test-api-server.py"
if [ -f "$TEST_FILE" ]; then
    if (cd "$BASE_DIR" && python3 -m pytest tests/test-api-server.py -q) 2>/dev/null; then
        pass "API server tests pass"
    else
        fail "API server tests failed"
    fi
else
    skip "API server test file not found"
fi
```

- [ ] **Step 3: Run E2E tests**

Run: `bash claude-terminal/tests/test-e2e.sh`

Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add claude-terminal/tests/test-e2e.sh
git commit -m "test: update E2E tests for Python API server"
```

---

### Task 8: Version Bump, README, Manifest

**Files:**
- Modify: `claude-terminal/config.yaml:4`
- Modify: `claude-terminal/custom_components/claude_terminal/manifest.json`
- Modify: `claude-terminal/README.md`

- [ ] **Step 1: Bump versions**

`config.yaml`: `version: "2.4.0"`
`manifest.json`: `"version": "2.4.0"`

- [ ] **Step 2: Update README authentication section**

After the "AI Assistant Integration" Setup section, add:

```markdown
### Authentication

The add-on supports three authentication methods (checked in order):

1. **Add-on config (recommended):** Paste your Claude OAuth token in the add-on's **Configuration** tab under `claude_oauth_token`. Get the token by running `claude setup-token` in the terminal.

2. **Stored CLI session:** If you've logged in through the web terminal (`claude auth login`), the session is saved and reused automatically.

3. **secrets.yaml:** Scripts can read the token from `/config/secrets.yaml`:
   ```yaml
   claude_oauth_token: "sk-ant-oat01-..."
   ```

### Script Execution

Run Python scripts (like the irrigation advisor) from HA automations:

```yaml
shell_command:
  run_irrigation: >
    curl -s -X POST http://<addon-hostname>:8099/api/run-script
    -H "Content-Type: application/json"
    -d '{"script": "irrigation_advisor_claude.py"}'
```

Or run inline code:

```yaml
shell_command:
  quick_claude: >
    curl -s -X POST http://<addon-hostname>:8099/api/run-script
    -H "Content-Type: application/json"
    -d '{"code": "from claude_agent_sdk import query..."}'
```

Check the add-on logs for the actual hostname (look for "Add-on hostname for integration:").
```

- [ ] **Step 3: Commit**

```bash
git add claude-terminal/config.yaml claude-terminal/custom_components/claude_terminal/manifest.json claude-terminal/README.md
git commit -m "feat: bump to v2.4.0, update README with auth and script docs"
```

---

### Task 9: Build and Verify

- [ ] **Step 1: Run all Python tests**

```bash
cd claude-terminal
python3 -m pytest tests/test-api-server.py -v
PYTHONPATH=custom_components python3 -m pytest tests/test_integration/ -v
```

- [ ] **Step 2: Run E2E tests**

```bash
bash claude-terminal/tests/test-e2e.sh
```

- [ ] **Step 3: Build container**

```bash
podman build --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base:3.21 -t local/claude-terminal:test ./claude-terminal
```

- [ ] **Step 4: Verify SDK is available in container**

```bash
podman run --rm local/claude-terminal:test python3 -c "from claude_agent_sdk import query; print('Agent SDK OK')"
podman run --rm local/claude-terminal:test python3 -c "import aiohttp; print('aiohttp OK')"
podman run --rm local/claude-terminal:test python3 --check /opt/scripts/api-server.py
```

- [ ] **Step 5: Verify no secrets in committed files**

```bash
grep -rn 'sk-ant\|oat01' --include='*.py' --include='*.js' --include='*.md' --include='*.yaml' . | grep -v .git/
```

- [ ] **Step 6: Push**

```bash
git push
```
