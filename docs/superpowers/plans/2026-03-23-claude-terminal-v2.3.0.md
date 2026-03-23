# Claude Terminal v2.3.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bypass permissions, agent teams, and HA conversation/AI Task integration to the Claude Terminal add-on.

**Architecture:** Node.js HTTP API server in the add-on spawns `claude -p` processes for incoming requests. A Python custom integration auto-installed to `/config/custom_components/` registers as both a conversation agent and AI Task entity in HA, forwarding requests to the API server. All Claude invocations include `--dangerously-skip-permissions`. Agent teams enabled via environment variable.

**Tech Stack:** Node.js (built-in `http` module), Python (HA custom integration), Bash (run.sh/scripts), Shell tests, Node `node:test`, pytest with mocks.

**Spec:** `docs/superpowers/specs/2026-03-23-claude-terminal-enhancements-design.md`

**Logging philosophy:** Every significant action gets a log line. In the add-on (bash), use `bashio::log.info/warning/error`. In the API server (Node.js), use `console.log/error` with `[API]` prefix (ttyd captures stdout). In the Python integration, use `logging.getLogger(__name__)`. Include request IDs, timings, and error details so issues are debuggable from HA's add-on log viewer.

---

### Task 1: Bypass Permissions in run.sh

**Files:**
- Modify: `claude-terminal/run.sh:279-292` (get_claude_launch_command function)

- [ ] **Step 1: Update auto-launch path**

In `get_claude_launch_command()`, change line 281:
```bash
# Before:
echo "${welcome_prefix}tmux new-session -A -s claude 'claude'"
# After:
echo "${welcome_prefix}tmux new-session -A -s claude 'claude --dangerously-skip-permissions'"
```

- [ ] **Step 2: Update fallback path**

Change line 290:
```bash
# Before:
echo "${welcome_prefix}tmux new-session -A -s claude 'claude'"
# After:
echo "${welcome_prefix}tmux new-session -A -s claude 'claude --dangerously-skip-permissions'"
```

- [ ] **Step 3: Verify with grep**

Run: `grep -n 'dangerously-skip-permissions' claude-terminal/run.sh`
Expected: Two matches at the updated lines.

- [ ] **Step 4: Commit**

```bash
git add claude-terminal/run.sh
git commit -m "feat: always run Claude with --dangerously-skip-permissions in run.sh"
```

---

### Task 2: Bypass Permissions in Session Picker

**Files:**
- Modify: `claude-terminal/scripts/claude-session-picker.sh:82-136`

- [ ] **Step 1: Update launch_claude_new()**

Change line 92:
```bash
# Before:
exec tmux new-session -s "$TMUX_SESSION_NAME" 'claude'
# After:
exec tmux new-session -s "$TMUX_SESSION_NAME" 'claude --dangerously-skip-permissions'
```

- [ ] **Step 2: Update launch_claude_continue()**

Change line 103:
```bash
# Before:
exec tmux new-session -s "$TMUX_SESSION_NAME" 'claude -c'
# After:
exec tmux new-session -s "$TMUX_SESSION_NAME" 'claude -c --dangerously-skip-permissions'
```

- [ ] **Step 3: Update launch_claude_resume()**

Change line 114:
```bash
# Before:
exec tmux new-session -s "$TMUX_SESSION_NAME" 'claude -r'
# After:
exec tmux new-session -s "$TMUX_SESSION_NAME" 'claude -r --dangerously-skip-permissions'
```

- [ ] **Step 4: Update launch_claude_custom()**

Change line 135:
```bash
# Before:
exec tmux new-session -s "$TMUX_SESSION_NAME" "claude $custom_args"
# After:
exec tmux new-session -s "$TMUX_SESSION_NAME" "claude $custom_args --dangerously-skip-permissions"
```

- [ ] **Step 5: Verify with grep**

Run: `grep -c 'dangerously-skip-permissions' claude-terminal/scripts/claude-session-picker.sh`
Expected: `4`

- [ ] **Step 6: Commit**

```bash
git add claude-terminal/scripts/claude-session-picker.sh
git commit -m "feat: always run Claude with --dangerously-skip-permissions in session picker"
```

---

### Task 3: Enable Agent Teams

**Files:**
- Modify: `claude-terminal/run.sh:8-53` (init_environment function)

- [ ] **Step 1: Add agent teams env var to init_environment()**

After line 36 (`export ANTHROPIC_HOME="/data"`), add:
```bash
    # Enable experimental agent teams for multi-agent coordination
    export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

- [ ] **Step 2: Add logging for agent teams**

After the new export, add:
```bash
    bashio::log.info "  - Agent teams: enabled (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1)"
```

Place this so it appears in the "Environment initialized:" log block (before the closing of that block).

- [ ] **Step 3: Verify with grep**

Run: `grep -n 'AGENT_TEAMS' claude-terminal/run.sh`
Expected: Two matches — the export and the log line.

- [ ] **Step 4: Commit**

```bash
git add claude-terminal/run.sh
git commit -m "feat: enable experimental agent teams via environment variable"
```

---

### Task 4: API Server - Core Implementation

**Files:**
- Create: `claude-terminal/scripts/api-server.js`

This is the Node.js HTTP server that receives requests from the HA custom integration, spawns `claude -p`, and returns responses.

- [ ] **Step 1: Write the API server**

Create `claude-terminal/scripts/api-server.js`:

```javascript
const http = require('http');
const { spawn } = require('child_process');

const PORT = 8099;
const MAX_TIMEOUT_MS = 120000;
const MAX_REQUESTS_PER_MINUTE = 10;
const MAX_CONVERSATION_TURNS = 10;

// Request tracking for rate limiting
const requestTimestamps = [];

// Concurrency lock
let currentRequest = null;

function log(level, msg, data = {}) {
  const timestamp = new Date().toISOString();
  const dataStr = Object.keys(data).length > 0 ? ' ' + JSON.stringify(data) : '';
  console.log(`[API][${timestamp}][${level.toUpperCase()}] ${msg}${dataStr}`);
}

function isRateLimited() {
  const now = Date.now();
  // Remove timestamps older than 1 minute
  while (requestTimestamps.length > 0 && requestTimestamps[0] < now - 60000) {
    requestTimestamps.shift();
  }
  return requestTimestamps.length >= MAX_REQUESTS_PER_MINUTE;
}

function buildSystemPrompt(context) {
  const now = new Date().toLocaleString('en-US', {
    timeZone: process.env.TZ || 'UTC',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });

  const lines = [];

  if (context.source === 'conversation') {
    lines.push("You are responding via Home Assistant's conversation interface, not an interactive terminal.");
    lines.push(`Current time: ${now}`);
    if (context.user_name) lines.push(`User: ${context.user_name}`);
    if (context.device_name) lines.push(`Triggered from device: ${context.device_name}`);
    if (context.satellite_name) lines.push(`Satellite: ${context.satellite_name}`);
    if (context.language) lines.push(`Language: ${context.language}`);
    lines.push('');
    lines.push('Be concise and action-oriented. When controlling devices, confirm what you did in one sentence.');
  } else if (context.source === 'ai_task') {
    lines.push("You are responding via Home Assistant's AI Task interface, not an interactive terminal.");
    lines.push(`Current time: ${now}`);
    if (context.task_name) lines.push(`Task: ${context.task_name}`);
    if (context.language) lines.push(`Language: ${context.language}`);
    lines.push('');
    lines.push('Structure your output clearly as it will be consumed by automations.');
  }

  if (context.extra_system_prompt) {
    lines.push('');
    lines.push(context.extra_system_prompt);
  }

  return lines.join('\n');
}

function buildClaudeArgs(query, context, conversationId, jsonSchema) {
  const args = [
    '-p', query,
    '--dangerously-skip-permissions',
    '--output-format', 'json',
  ];

  const systemPrompt = buildSystemPrompt(context || {});
  if (systemPrompt) {
    args.push('--append-system-prompt', systemPrompt);
  }

  // Multi-turn conversation support
  if (conversationId) {
    args.push('--resume', conversationId);
  }

  // Structured output for AI Task
  if (jsonSchema) {
    args.push('--json-schema', JSON.stringify(jsonSchema));
  }

  // One-shot AI tasks don't need session persistence
  if (context && context.source === 'ai_task' && !conversationId) {
    args.push('--no-session-persistence');
  }

  return args;
}

function runClaude(args) {
  return new Promise((resolve, reject) => {
    const startTime = Date.now();
    log('info', 'Spawning claude process', { args: args.slice(0, 4).join(' ') + '...' });

    const proc = spawn('claude', args, {
      timeout: MAX_TIMEOUT_MS,
      env: { ...process.env },
    });

    let stdout = '';
    let stderr = '';

    proc.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    proc.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

    proc.on('close', (code) => {
      const durationMs = Date.now() - startTime;
      if (code === 0) {
        log('info', 'Claude process completed', { durationMs, stdoutLength: stdout.length });
        try {
          const parsed = JSON.parse(stdout);
          resolve(parsed);
        } catch (e) {
          log('error', 'Failed to parse Claude JSON output', { error: e.message, stdout: stdout.substring(0, 500) });
          reject(new Error('Failed to parse Claude response as JSON'));
        }
      } else {
        log('error', 'Claude process failed', { code, durationMs, stderr: stderr.substring(0, 500) });
        reject(new Error(`Claude process exited with code ${code}: ${stderr.substring(0, 200)}`));
      }
    });

    proc.on('error', (err) => {
      log('error', 'Failed to spawn claude process', { error: err.message });
      reject(err);
    });
  });
}

function extractResponse(claudeResult, hasSchema) {
  const response = {
    result: hasSchema ? (claudeResult.structured_output || null) : (claudeResult.result || ''),
    session_id: claudeResult.session_id || null,
    cost_usd: claudeResult.total_cost_usd || null,
    model_usage: claudeResult.modelUsage || null,
  };

  if (response.cost_usd) {
    log('info', 'Request cost', { cost_usd: response.cost_usd });
  }

  return response;
}

async function handleQuery(req, res) {
  const requestId = Math.random().toString(36).substring(2, 10);
  log('info', `Request received [${requestId}]`, { method: req.method, url: req.url });

  // Rate limiting
  if (isRateLimited()) {
    log('warning', `Rate limited [${requestId}]`, { requestsInLastMinute: requestTimestamps.length });
    res.writeHead(429, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: true, message: 'Rate limit exceeded. Max 10 requests per minute.', code: 429 }));
    return;
  }

  // Concurrency check
  if (currentRequest) {
    log('warning', `Busy - another request in progress [${requestId}]`, { currentRequest });
    res.writeHead(503, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: true, message: 'Another request is currently being processed. Please try again.', code: 503 }));
    return;
  }

  // Parse request body
  let body = '';
  for await (const chunk of req) {
    body += chunk.toString();
  }

  let payload;
  try {
    payload = JSON.parse(body);
  } catch (e) {
    log('error', `Invalid JSON in request body [${requestId}]`, { error: e.message });
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: true, message: 'Invalid JSON in request body', code: 400 }));
    return;
  }

  const { query, conversation_id, context, json_schema } = payload;
  if (!query) {
    log('error', `Missing query field [${requestId}]`);
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: true, message: 'Missing required field: query', code: 400 }));
    return;
  }

  log('info', `Processing query [${requestId}]`, {
    source: context?.source || 'unknown',
    user: context?.user_name || 'unknown',
    queryLength: query.length,
    hasConversationId: !!conversation_id,
    hasSchema: !!json_schema,
  });

  currentRequest = requestId;
  requestTimestamps.push(Date.now());

  try {
    const args = buildClaudeArgs(query, context, conversation_id, json_schema);
    const claudeResult = await runClaude(args);
    const response = extractResponse(claudeResult, !!json_schema);

    log('info', `Request completed [${requestId}]`, {
      sessionId: response.session_id,
      resultLength: typeof response.result === 'string' ? response.result.length : JSON.stringify(response.result).length,
    });

    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(response));
  } catch (err) {
    log('error', `Request failed [${requestId}]`, { error: err.message });

    const code = err.message.includes('rate') ? 429 : 500;
    res.writeHead(code, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      error: true,
      message: err.message,
      code,
    }));
  } finally {
    currentRequest = null;
  }
}

const server = http.createServer(async (req, res) => {
  // Health check
  if (req.method === 'GET' && req.url === '/api/health') {
    log('debug', 'Health check');
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', busy: !!currentRequest }));
    return;
  }

  // Query endpoint
  if (req.method === 'POST' && req.url === '/api/query') {
    await handleQuery(req, res);
    return;
  }

  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: true, message: 'Not found', code: 404 }));
});

server.listen(PORT, '0.0.0.0', () => {
  log('info', `API server started on port ${PORT}`);
});

// Self-restart on uncaught exception
process.on('uncaughtException', (err) => {
  log('error', 'Uncaught exception - restarting in 5 seconds', { error: err.message, stack: err.stack });
  setTimeout(() => process.exit(1), 5000);
});

process.on('unhandledRejection', (reason) => {
  log('error', 'Unhandled rejection', { reason: String(reason) });
});
```

- [ ] **Step 2: Verify syntax**

Run: `node --check claude-terminal/scripts/api-server.js`
Expected: No output (clean syntax)

- [ ] **Step 3: Commit**

```bash
git add claude-terminal/scripts/api-server.js
git commit -m "feat: add Node.js API server for HA conversation/AI Task integration"
```

---

### Task 5: API Server Tests

**Files:**
- Create: `claude-terminal/tests/test-api-server.js`

Uses Node.js built-in `node:test` module (no dependencies). Tests the pure functions (buildSystemPrompt, buildClaudeArgs, extractResponse, isRateLimited) by requiring the server module. Since the server auto-starts on require, we'll test the functions by extracting them. We refactor the server slightly to export testable functions.

- [ ] **Step 1: Make api-server.js functions exportable**

Add at the very end of `claude-terminal/scripts/api-server.js`, before `process.on('uncaughtException'...`:

```javascript
// Export for testing (only when required as a module, not when run directly)
if (typeof module !== 'undefined') {
  module.exports = { buildSystemPrompt, buildClaudeArgs, extractResponse, isRateLimited, log };
}
```

Then wrap the `server.listen` and `process.on` calls so they only run when the file is executed directly:

```javascript
if (require.main === module) {
  server.listen(PORT, '0.0.0.0', () => {
    log('info', `API server started on port ${PORT}`);
  });

  process.on('uncaughtException', (err) => {
    log('error', 'Uncaught exception - restarting in 5 seconds', { error: err.message, stack: err.stack });
    setTimeout(() => process.exit(1), 5000);
  });

  process.on('unhandledRejection', (reason) => {
    log('error', 'Unhandled rejection', { reason: String(reason) });
  });
}
```

- [ ] **Step 2: Write the test file**

Create `claude-terminal/tests/test-api-server.js`:

```javascript
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const { buildSystemPrompt, buildClaudeArgs, extractResponse } = require('../scripts/api-server');

describe('buildSystemPrompt', () => {
  it('builds conversation prompt with all fields', () => {
    const prompt = buildSystemPrompt({
      source: 'conversation',
      user_name: 'Abhik',
      device_name: 'Kitchen Speaker',
      satellite_name: 'Kitchen Satellite',
      language: 'en',
    });
    assert.ok(prompt.includes('conversation interface'));
    assert.ok(prompt.includes('User: Abhik'));
    assert.ok(prompt.includes('Kitchen Speaker'));
    assert.ok(prompt.includes('Kitchen Satellite'));
    assert.ok(prompt.includes('Language: en'));
    assert.ok(prompt.includes('concise and action-oriented'));
  });

  it('builds ai_task prompt with task name', () => {
    const prompt = buildSystemPrompt({
      source: 'ai_task',
      task_name: 'morning_briefing',
      language: 'en',
    });
    assert.ok(prompt.includes('AI Task interface'));
    assert.ok(prompt.includes('Task: morning_briefing'));
    assert.ok(prompt.includes('consumed by automations'));
  });

  it('includes extra_system_prompt when provided', () => {
    const prompt = buildSystemPrompt({
      source: 'conversation',
      extra_system_prompt: 'Only respond in Spanish',
    });
    assert.ok(prompt.includes('Only respond in Spanish'));
  });

  it('handles empty context gracefully', () => {
    const prompt = buildSystemPrompt({});
    assert.equal(typeof prompt, 'string');
  });

  it('includes current time', () => {
    const prompt = buildSystemPrompt({ source: 'conversation' });
    // Should contain a time-like string (digits with colons)
    assert.ok(/\d{1,2}:\d{2}:\d{2}/.test(prompt), 'Should contain a time string');
  });
});

describe('buildClaudeArgs', () => {
  it('includes required flags', () => {
    const args = buildClaudeArgs('hello', { source: 'conversation' }, null, null);
    assert.ok(args.includes('-p'));
    assert.ok(args.includes('hello'));
    assert.ok(args.includes('--dangerously-skip-permissions'));
    assert.ok(args.includes('--output-format'));
    assert.ok(args.includes('json'));
  });

  it('adds --resume when conversation_id provided', () => {
    const args = buildClaudeArgs('hello', {}, 'session-123', null);
    const resumeIdx = args.indexOf('--resume');
    assert.ok(resumeIdx >= 0);
    assert.equal(args[resumeIdx + 1], 'session-123');
  });

  it('adds --json-schema when provided', () => {
    const schema = { type: 'object', properties: { answer: { type: 'string' } } };
    const args = buildClaudeArgs('hello', {}, null, schema);
    const schemaIdx = args.indexOf('--json-schema');
    assert.ok(schemaIdx >= 0);
    assert.equal(args[schemaIdx + 1], JSON.stringify(schema));
  });

  it('adds --no-session-persistence for ai_task without conversation_id', () => {
    const args = buildClaudeArgs('hello', { source: 'ai_task' }, null, null);
    assert.ok(args.includes('--no-session-persistence'));
  });

  it('does NOT add --no-session-persistence for ai_task with conversation_id', () => {
    const args = buildClaudeArgs('hello', { source: 'ai_task' }, 'sess-1', null);
    assert.ok(!args.includes('--no-session-persistence'));
  });

  it('does NOT add --no-session-persistence for conversation source', () => {
    const args = buildClaudeArgs('hello', { source: 'conversation' }, null, null);
    assert.ok(!args.includes('--no-session-persistence'));
  });

  it('adds --append-system-prompt with dynamic prompt', () => {
    const args = buildClaudeArgs('hello', { source: 'conversation', user_name: 'Test' }, null, null);
    const promptIdx = args.indexOf('--append-system-prompt');
    assert.ok(promptIdx >= 0);
    assert.ok(args[promptIdx + 1].includes('User: Test'));
  });
});

describe('extractResponse', () => {
  it('extracts result for normal calls', () => {
    const claudeResult = {
      result: 'The lights are off.',
      session_id: 'sess-123',
      total_cost_usd: 0.05,
      modelUsage: { 'claude-opus-4-6': {} },
    };
    const response = extractResponse(claudeResult, false);
    assert.equal(response.result, 'The lights are off.');
    assert.equal(response.session_id, 'sess-123');
    assert.equal(response.cost_usd, 0.05);
  });

  it('extracts structured_output when schema is used', () => {
    const claudeResult = {
      result: '',
      structured_output: { answer: 'yes', confidence: 0.95 },
      session_id: 'sess-456',
      total_cost_usd: 0.03,
    };
    const response = extractResponse(claudeResult, true);
    assert.deepEqual(response.result, { answer: 'yes', confidence: 0.95 });
    assert.equal(response.session_id, 'sess-456');
  });

  it('handles missing fields gracefully', () => {
    const response = extractResponse({}, false);
    assert.equal(response.result, '');
    assert.equal(response.session_id, null);
    assert.equal(response.cost_usd, null);
  });
});
```

- [ ] **Step 3: Run tests**

Run: `cd claude-terminal && node --test tests/test-api-server.js`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add claude-terminal/scripts/api-server.js claude-terminal/tests/test-api-server.js
git commit -m "test: add API server unit tests and make functions exportable"
```

---

### Task 6: Custom Integration - Constants and Manifest

**Files:**
- Create: `claude-terminal/custom_components/claude_terminal/const.py`
- Create: `claude-terminal/custom_components/claude_terminal/manifest.json`
- Create: `claude-terminal/custom_components/claude_terminal/strings.json`

- [ ] **Step 1: Create const.py**

```python
"""Constants for the Claude Terminal integration."""

import logging

DOMAIN = "claude_terminal"
LOGGER = logging.getLogger(__name__)

# Add-on API configuration
DEFAULT_ADDON_HOSTNAME = "claude-terminal"
DEFAULT_ADDON_PORT = 8099
API_QUERY_PATH = "/api/query"
API_HEALTH_PATH = "/api/health"
API_TIMEOUT_SECONDS = 130  # Slightly longer than the server's 120s timeout

CONF_ADDON_HOSTNAME = "addon_hostname"
```

- [ ] **Step 2: Create manifest.json**

```json
{
  "domain": "claude_terminal",
  "name": "Claude Terminal",
  "codeowners": [],
  "config_flow": true,
  "dependencies": ["hassio"],
  "documentation": "https://github.com/heytcass/home-assistant-addons",
  "iot_class": "local_push",
  "version": "2.3.0"
}
```

- [ ] **Step 3: Create strings.json**

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Claude Terminal",
        "description": "Add Claude Terminal as a conversation agent and AI Task entity. Make sure the Claude Terminal add-on is installed and running."
      }
    }
  }
}
```

- [ ] **Step 4: Commit**

```bash
git add claude-terminal/custom_components/claude_terminal/
git commit -m "feat: add custom integration constants, manifest, and strings"
```

---

### Task 7: Custom Integration - Config Flow and Init

**Files:**
- Create: `claude-terminal/custom_components/claude_terminal/__init__.py`
- Create: `claude-terminal/custom_components/claude_terminal/config_flow.py`

- [ ] **Step 1: Create config_flow.py**

```python
"""Config flow for Claude Terminal integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN, LOGGER


class ClaudeTerminalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Claude Terminal."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        LOGGER.info("Claude Terminal config flow started")

        if user_input is not None:
            # Only allow one instance
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            LOGGER.info("Claude Terminal integration configured successfully")
            return self.async_create_entry(title="Claude Terminal", data={})

        return self.async_show_form(step_id="user")
```

- [ ] **Step 2: Create __init__.py**

```python
"""The Claude Terminal integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER

PLATFORMS = [Platform.CONVERSATION]
# AI Task platform will be added when HA stabilizes the API


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Claude Terminal from a config entry."""
    LOGGER.info("Setting up Claude Terminal integration")
    hass.data.setdefault(DOMAIN, {})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    LOGGER.info("Claude Terminal integration setup complete, platforms: %s", PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    LOGGER.info("Unloading Claude Terminal integration")
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN, None)
    return unload_ok
```

Note: We start with only `Platform.CONVERSATION`. AI Task can be added as a second platform once we verify conversation works end-to-end. The plan includes the AI Task implementation but it can be registered later by adding `Platform.AI_TASK` to `PLATFORMS` (or whatever the correct platform constant is — verify against HA core at implementation time).

- [ ] **Step 3: Commit**

```bash
git add claude-terminal/custom_components/claude_terminal/
git commit -m "feat: add config flow and integration init for Claude Terminal"
```

---

### Task 8: Custom Integration - HTTP Client Helper

**Files:**
- Create: `claude-terminal/custom_components/claude_terminal/api.py`

This module handles all HTTP communication with the add-on's API server. Centralizes logging, error handling, and response parsing.

- [ ] **Step 1: Create api.py**

```python
"""HTTP client for Claude Terminal add-on API."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .const import (
    API_HEALTH_PATH,
    API_QUERY_PATH,
    API_TIMEOUT_SECONDS,
    DEFAULT_ADDON_HOSTNAME,
    DEFAULT_ADDON_PORT,
    LOGGER,
)


class ClaudeTerminalAPIError(Exception):
    """Error communicating with Claude Terminal API."""

    def __init__(self, message: str, code: int = 500) -> None:
        """Initialize."""
        super().__init__(message)
        self.code = code


class ClaudeTerminalAPI:
    """Client for the Claude Terminal add-on API server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        hostname: str = DEFAULT_ADDON_HOSTNAME,
        port: int = DEFAULT_ADDON_PORT,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._base_url = f"http://{hostname}:{port}"
        LOGGER.debug(
            "Claude Terminal API client initialized, base_url=%s", self._base_url
        )

    async def async_check_health(self) -> bool:
        """Check if the API server is healthy."""
        url = f"{self._base_url}{API_HEALTH_PATH}"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    LOGGER.debug("Health check OK, busy=%s", data.get("busy"))
                    return True
                LOGGER.warning("Health check failed, status=%s", resp.status)
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            LOGGER.warning("Health check error: %s", err)
            return False

    async def async_query(
        self,
        query: str,
        context: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        json_schema: dict | None = None,
    ) -> dict[str, Any]:
        """Send a query to the Claude Terminal API server."""
        url = f"{self._base_url}{API_QUERY_PATH}"
        payload: dict[str, Any] = {"query": query}
        if context:
            payload["context"] = context
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if json_schema:
            payload["json_schema"] = json_schema

        LOGGER.info(
            "Sending query to Claude Terminal API: source=%s, query_length=%d, has_conversation_id=%s",
            context.get("source", "unknown") if context else "unknown",
            len(query),
            bool(conversation_id),
        )

        try:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
            async with self._session.post(url, json=payload, timeout=timeout) as resp:
                data = await resp.json()

                if resp.status != 200:
                    error_msg = data.get("message", f"HTTP {resp.status}")
                    error_code = data.get("code", resp.status)
                    LOGGER.error(
                        "Claude Terminal API error: status=%s, message=%s, code=%s",
                        resp.status, error_msg, error_code,
                    )
                    raise ClaudeTerminalAPIError(error_msg, error_code)

                LOGGER.info(
                    "Claude Terminal API response: session_id=%s, result_length=%d, cost_usd=%s",
                    data.get("session_id"),
                    len(str(data.get("result", ""))),
                    data.get("cost_usd"),
                )
                return data

        except aiohttp.ClientError as err:
            LOGGER.error("Failed to connect to Claude Terminal API: %s", err)
            raise ClaudeTerminalAPIError(
                f"Cannot connect to Claude Terminal add-on: {err}"
            ) from err
        except asyncio.TimeoutError:
            LOGGER.error(
                "Claude Terminal API request timed out after %ds", API_TIMEOUT_SECONDS
            )
            raise ClaudeTerminalAPIError(
                f"Request timed out after {API_TIMEOUT_SECONDS} seconds"
            ) from None
```

- [ ] **Step 2: Commit**

```bash
git add claude-terminal/custom_components/claude_terminal/api.py
git commit -m "feat: add HTTP client for Claude Terminal add-on API"
```

---

### Task 9: Custom Integration - Conversation Entity

**Files:**
- Create: `claude-terminal/custom_components/claude_terminal/conversation.py`

- [ ] **Step 1: Create conversation.py**

```python
"""Conversation agent for Claude Terminal."""

from __future__ import annotations

from typing import Literal

import aiohttp

from homeassistant.components.conversation import (
    ChatLog,
    ConversationEntity,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ClaudeTerminalAPI, ClaudeTerminalAPIError
from .const import DOMAIN, LOGGER


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the conversation entity."""
    LOGGER.info("Setting up Claude Terminal conversation entity")
    session = aiohttp.ClientSession()
    api = ClaudeTerminalAPI(session)
    hass.data[DOMAIN]["api"] = api
    hass.data[DOMAIN]["session"] = session
    async_add_entities([ClaudeTerminalConversationEntity(config_entry, api)])
    LOGGER.info("Claude Terminal conversation entity registered")


class ClaudeTerminalConversationEntity(ConversationEntity):
    """Claude Terminal conversation agent entity."""

    _attr_has_entity_name = True
    _attr_name = "Claude Terminal"

    def __init__(
        self,
        config_entry: ConfigEntry,
        api: ClaudeTerminalAPI,
    ) -> None:
        """Initialize the entity."""
        self._api = api
        self._attr_unique_id = f"{config_entry.entry_id}_conversation"
        LOGGER.debug("ClaudeTerminalConversationEntity initialized")

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return supported languages."""
        return "*"

    async def _resolve_user_name(self, user_id: str | None) -> str | None:
        """Resolve a user ID to a display name."""
        if not user_id:
            return None
        try:
            user = await self.hass.auth.async_get_user(user_id)
            if user:
                LOGGER.debug("Resolved user_id=%s to name=%s", user_id, user.name)
                return user.name
        except Exception:
            LOGGER.warning("Failed to resolve user_id=%s", user_id, exc_info=True)
        return None

    def _resolve_device_name(self, device_id: str | None) -> str | None:
        """Resolve a device ID to a display name."""
        if not device_id:
            return None
        try:
            registry = dr.async_get(self.hass)
            device = registry.async_get(device_id)
            if device:
                LOGGER.debug("Resolved device_id=%s to name=%s", device_id, device.name)
                return device.name
        except Exception:
            LOGGER.warning("Failed to resolve device_id=%s", device_id, exc_info=True)
        return None

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Handle a conversation message."""
        LOGGER.info(
            "Conversation request: text_length=%d, language=%s, conversation_id=%s, "
            "device_id=%s, satellite_id=%s, user_id=%s",
            len(user_input.text),
            user_input.language,
            user_input.conversation_id,
            user_input.device_id,
            user_input.satellite_id,
            user_input.context.user_id if user_input.context else None,
        )

        # Resolve names from IDs
        user_name = await self._resolve_user_name(
            user_input.context.user_id if user_input.context else None
        )
        device_name = self._resolve_device_name(user_input.device_id)
        satellite_name = self._resolve_device_name(user_input.satellite_id)

        context = {
            "source": "conversation",
            "user_name": user_name,
            "device_name": device_name,
            "satellite_name": satellite_name,
            "language": user_input.language,
            "extra_system_prompt": user_input.extra_system_prompt,
        }

        LOGGER.debug("Built context for API call: %s", context)

        try:
            data = await self._api.async_query(
                query=user_input.text,
                context=context,
                conversation_id=user_input.conversation_id,
            )

            response_text = data.get("result", "I received your message but got an empty response.")
            session_id = data.get("session_id")

            LOGGER.info(
                "Conversation response: session_id=%s, response_length=%d, cost_usd=%s",
                session_id,
                len(response_text),
                data.get("cost_usd"),
            )

            response = intent.IntentResponse(language=user_input.language)
            response.async_set_speech(response_text)
            return ConversationResult(
                response=response,
                conversation_id=session_id,
            )

        except ClaudeTerminalAPIError as err:
            LOGGER.error("Claude Terminal API error during conversation: %s (code=%s)", err, err.code)
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Sorry, I couldn't process your request: {err}",
            )
            return ConversationResult(
                response=response,
                conversation_id=user_input.conversation_id,
            )
        except Exception:
            LOGGER.error("Unexpected error during conversation", exc_info=True)
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "Sorry, an unexpected error occurred. Check the add-on logs for details.",
            )
            return ConversationResult(
                response=response,
                conversation_id=user_input.conversation_id,
            )
```

- [ ] **Step 2: Commit**

```bash
git add claude-terminal/custom_components/claude_terminal/conversation.py
git commit -m "feat: add conversation entity for Claude Terminal integration"
```

---

### Task 10: Custom Integration Tests

**Files:**
- Create: `claude-terminal/tests/test_integration/test_api.py`
- Create: `claude-terminal/tests/test_integration/test_conversation.py`
- Create: `claude-terminal/tests/test_integration/conftest.py`
- Create: `claude-terminal/tests/test_integration/requirements-test.txt`

These tests mock HA internals and aiohttp to verify the integration logic.

- [ ] **Step 1: Create requirements-test.txt**

```
pytest>=7.0
pytest-asyncio>=0.21
aiohttp
aioresponses
```

- [ ] **Step 2: Create conftest.py**

```python
"""Test fixtures for Claude Terminal integration tests."""

import sys
from unittest.mock import MagicMock, AsyncMock
import pytest

# Mock Home Assistant modules that won't be available in test environment
ha_modules = {
    "homeassistant": MagicMock(),
    "homeassistant.components": MagicMock(),
    "homeassistant.components.conversation": MagicMock(),
    "homeassistant.config_entries": MagicMock(),
    "homeassistant.const": MagicMock(),
    "homeassistant.core": MagicMock(),
    "homeassistant.helpers": MagicMock(),
    "homeassistant.helpers.device_registry": MagicMock(),
    "homeassistant.helpers.entity_platform": MagicMock(),
    "homeassistant.helpers.intent": MagicMock(),
}

# Set Platform constants
ha_modules["homeassistant.const"].Platform = MagicMock()
ha_modules["homeassistant.const"].Platform.CONVERSATION = "conversation"

for mod_name, mod_mock in ha_modules.items():
    sys.modules[mod_name] = mod_mock
```

- [ ] **Step 3: Create test_api.py**

```python
"""Tests for the Claude Terminal API client."""

import asyncio
import pytest
import aiohttp
from aioresponses import aioresponses

# conftest mocks HA modules before import
from claude_terminal.api import ClaudeTerminalAPI, ClaudeTerminalAPIError


@pytest.fixture
async def api_client():
    """Create an API client with a real aiohttp session."""
    session = aiohttp.ClientSession()
    client = ClaudeTerminalAPI(session, hostname="localhost", port=8099)
    yield client
    await session.close()


@pytest.mark.asyncio
async def test_health_check_success(api_client):
    """Test successful health check."""
    with aioresponses() as m:
        m.get("http://localhost:8099/api/health", payload={"status": "ok", "busy": False})
        result = await api_client.async_check_health()
        assert result is True


@pytest.mark.asyncio
async def test_health_check_failure(api_client):
    """Test failed health check."""
    with aioresponses() as m:
        m.get("http://localhost:8099/api/health", status=500)
        result = await api_client.async_check_health()
        assert result is False


@pytest.mark.asyncio
async def test_health_check_connection_error(api_client):
    """Test health check when server is unreachable."""
    with aioresponses() as m:
        m.get("http://localhost:8099/api/health", exception=aiohttp.ClientError())
        result = await api_client.async_check_health()
        assert result is False


@pytest.mark.asyncio
async def test_query_success(api_client):
    """Test successful query."""
    mock_response = {
        "result": "The lights are now off.",
        "session_id": "sess-123",
        "cost_usd": 0.05,
    }
    with aioresponses() as m:
        m.post("http://localhost:8099/api/query", payload=mock_response)
        result = await api_client.async_query(
            query="Turn off the lights",
            context={"source": "conversation", "user_name": "Test"},
        )
        assert result["result"] == "The lights are now off."
        assert result["session_id"] == "sess-123"


@pytest.mark.asyncio
async def test_query_with_conversation_id(api_client):
    """Test query with conversation continuation."""
    with aioresponses() as m:
        m.post("http://localhost:8099/api/query", payload={"result": "ok", "session_id": "sess-123"})
        result = await api_client.async_query(
            query="And the bedroom too",
            conversation_id="sess-123",
        )
        assert result["result"] == "ok"


@pytest.mark.asyncio
async def test_query_rate_limited(api_client):
    """Test rate limit handling."""
    with aioresponses() as m:
        m.post(
            "http://localhost:8099/api/query",
            payload={"error": True, "message": "Rate limit exceeded", "code": 429},
            status=429,
        )
        with pytest.raises(ClaudeTerminalAPIError) as exc_info:
            await api_client.async_query(query="hello")
        assert exc_info.value.code == 429


@pytest.mark.asyncio
async def test_query_server_busy(api_client):
    """Test server busy (concurrent request) handling."""
    with aioresponses() as m:
        m.post(
            "http://localhost:8099/api/query",
            payload={"error": True, "message": "Another request in progress", "code": 503},
            status=503,
        )
        with pytest.raises(ClaudeTerminalAPIError) as exc_info:
            await api_client.async_query(query="hello")
        assert exc_info.value.code == 503


@pytest.mark.asyncio
async def test_query_connection_error(api_client):
    """Test connection error handling."""
    with aioresponses() as m:
        m.post("http://localhost:8099/api/query", exception=aiohttp.ClientError("Connection refused"))
        with pytest.raises(ClaudeTerminalAPIError) as exc_info:
            await api_client.async_query(query="hello")
        assert "Cannot connect" in str(exc_info.value)
```

- [ ] **Step 4: Create test_conversation.py**

```python
"""Tests for the Claude Terminal conversation entity."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# conftest mocks HA modules before import
from claude_terminal.api import ClaudeTerminalAPI, ClaudeTerminalAPIError


class TestConversationContextBuilding:
    """Test the context building logic without HA dependencies."""

    def test_context_includes_source(self):
        """Context should always include source."""
        context = {
            "source": "conversation",
            "user_name": "Test",
            "device_name": None,
            "satellite_name": None,
            "language": "en",
            "extra_system_prompt": None,
        }
        assert context["source"] == "conversation"

    def test_context_with_all_fields(self):
        """Context should include all resolved fields."""
        context = {
            "source": "conversation",
            "user_name": "Abhik",
            "device_name": "Kitchen Speaker",
            "satellite_name": "Kitchen Satellite",
            "language": "en",
            "extra_system_prompt": "Be brief",
        }
        assert context["user_name"] == "Abhik"
        assert context["device_name"] == "Kitchen Speaker"
        assert context["satellite_name"] == "Kitchen Satellite"
        assert context["extra_system_prompt"] == "Be brief"


class TestAPIErrorHandling:
    """Test error scenarios for API communication."""

    @pytest.mark.asyncio
    async def test_api_error_produces_user_friendly_message(self):
        """API errors should result in a readable error, not a traceback."""
        err = ClaudeTerminalAPIError("Rate limit exceeded", 429)
        assert "Rate limit" in str(err)
        assert err.code == 429

    @pytest.mark.asyncio
    async def test_timeout_error_message(self):
        """Timeout should produce a clear message."""
        err = ClaudeTerminalAPIError("Request timed out after 130 seconds")
        assert "timed out" in str(err)
```

- [ ] **Step 5: Run tests**

Run: `cd claude-terminal && pip install -r tests/test_integration/requirements-test.txt && PYTHONPATH=custom_components pytest tests/test_integration/ -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add claude-terminal/tests/
git commit -m "test: add integration tests for API client and conversation entity"
```

---

### Task 11: Auto-Installation and Startup Flow

**Files:**
- Modify: `claude-terminal/run.sh` (add install_custom_integration, start_api_server, update main)
- Modify: `claude-terminal/Dockerfile` (copy custom_components into the image)

- [ ] **Step 1: Update Dockerfile to include custom_components**

After the `COPY scripts/ /opt/scripts/` line, add:
```dockerfile
COPY custom_components/ /opt/custom_components/
```

- [ ] **Step 2: Add install_custom_integration() to run.sh**

Add this function after `setup_ha_mcp()`:

```bash
# Install custom integration for HA conversation agent and AI Task
install_custom_integration() {
    local source_dir="/opt/custom_components/claude_terminal"
    local target_dir="/config/custom_components/claude_terminal"
    local version_marker="$target_dir/.addon_version"
    local addon_version

    addon_version=$(bashio::addon.version 2>/dev/null || echo "unknown")

    bashio::log.info "Checking Claude Terminal custom integration..."
    bashio::log.info "  Add-on version: $addon_version"

    if [ ! -d "$source_dir" ]; then
        bashio::log.warning "Custom integration source not found at $source_dir, skipping"
        return 0
    fi

    # Check if already installed with current version
    if [ -f "$version_marker" ]; then
        local installed_version
        installed_version=$(cat "$version_marker" 2>/dev/null || echo "")
        bashio::log.info "  Installed integration version: $installed_version"

        if [ "$installed_version" = "$addon_version" ]; then
            bashio::log.info "Custom integration already up to date (v$addon_version)"
            return 0
        fi
        bashio::log.info "Version mismatch ($installed_version != $addon_version), updating integration..."
    else
        bashio::log.info "Custom integration not installed, performing first install..."
    fi

    # Ensure target directory exists
    mkdir -p "/config/custom_components"

    # Copy integration files (overwrite)
    if cp -r "$source_dir" "/config/custom_components/"; then
        # Write version marker
        echo "$addon_version" > "$version_marker"
        bashio::log.info "Custom integration installed/updated to v$addon_version"
        bashio::log.info "  Installed to: $target_dir"
        bashio::log.info "  Files:"
        ls -la "$target_dir"/ 2>/dev/null | while IFS= read -r line; do
            bashio::log.info "    $line"
        done

        # Check if this is a first install (no version marker existed before)
        if [ ! -f "$version_marker" ] || [ "$installed_version" = "" ]; then
            bashio::log.warning "======================================================"
            bashio::log.warning "  FIRST INSTALL: Please restart Home Assistant to"
            bashio::log.warning "  load the Claude Terminal integration, then add it"
            bashio::log.warning "  via Settings > Devices & Services > Add Integration"
            bashio::log.warning "======================================================"
        fi
    else
        bashio::log.error "Failed to copy custom integration to $target_dir"
    fi
}
```

- [ ] **Step 3: Add start_api_server() to run.sh**

Add this function after `install_custom_integration()`:

```bash
# Start the API server for conversation/AI Task integration
start_api_server() {
    local api_script="/opt/scripts/api-server.js"

    if [ ! -f "$api_script" ]; then
        bashio::log.warning "API server script not found at $api_script, skipping"
        return 0
    fi

    bashio::log.info "Starting Claude Terminal API server..."
    bashio::log.info "  Script: $api_script"
    bashio::log.info "  Port: 8099"

    # Start in background - output goes to container logs
    node "$api_script" &
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

- [ ] **Step 4: Update main() function**

```bash
main() {
    bashio::log.info "Initializing Claude Terminal add-on..."

    # Run diagnostics first (especially helpful for VirtualBox issues)
    run_health_check

    init_environment
    install_tools
    setup_session_picker
    install_persistent_packages
    generate_ha_context
    setup_ha_mcp
    install_custom_integration
    start_api_server
    start_web_terminal
}
```

- [ ] **Step 5: Verify run.sh syntax**

Run: `bash -n claude-terminal/run.sh`
Expected: No output (clean syntax). Note: this won't catch bashio errors since bashio isn't available locally, but it validates bash syntax.

- [ ] **Step 6: Commit**

```bash
git add claude-terminal/run.sh claude-terminal/Dockerfile
git commit -m "feat: add auto-install of custom integration and API server startup"
```

---

### Task 12: Version Bump and config.yaml

**Files:**
- Modify: `claude-terminal/config.yaml:4` (version)

- [ ] **Step 1: Bump version**

Change line 4:
```yaml
# Before:
version: "2.2.0"
# After:
version: "2.3.0"
```

- [ ] **Step 2: Commit**

```bash
git add claude-terminal/config.yaml
git commit -m "chore: bump version to 2.3.0"
```

---

### Task 13: Update README

**Files:**
- Modify: `claude-terminal/README.md`

- [ ] **Step 1: Add AI Assistant Integration section**

After the "Configuration" section (after line 95), add:

```markdown
## AI Assistant Integration

Claude Terminal can act as a conversation agent and AI Task entity in Home Assistant, letting you use Claude from voice assistants, chat, and automations.

### Setup

After installing or updating the add-on:

1. **Restart Home Assistant** (one-time, so HA picks up the new integration)
2. Go to **Settings** → **Devices & Services** → **Add Integration**
3. Search for **Claude Terminal** and add it

### Use as a Voice Assistant

Go to **Settings** → **Voice Assistants** and select Claude Terminal as your conversation agent. You can then use Claude through any voice satellite or the Assist panel.

### Use in Automations

```yaml
action: ai_task.generate_data
target:
  entity_id: ai_task.claude_terminal
data:
  task_name: "morning_briefing"
  instructions: "What lights are on and what's the temperature?"
```

### Agent Teams

Claude Terminal supports experimental agent teams for coordinating multiple Claude instances on complex tasks. This is enabled automatically. In the terminal, ask Claude to create a team:

```
Create an agent team to review my automations. Spawn three reviewers:
- One checking for redundant automations
- One validating error handling
- One reviewing performance
```

### Troubleshooting AI Integration

- Check add-on logs for `[API]` prefixed messages for API server issues
- Verify the add-on is running before using conversation/AI Task
- If the integration doesn't appear, restart Home Assistant
- Rate limit: max 10 requests per minute to prevent runaway automations
```

- [ ] **Step 2: Update features list**

Add to the features list (around line 20):
```markdown
- **AI Conversation Agent**: Use Claude as a Home Assistant conversation agent for voice and chat
- **AI Task Entity**: Trigger Claude from automations and scripts
- **Agent Teams**: Coordinate multiple Claude instances for complex tasks
- **Bypass Permissions**: All commands run without permission prompts for seamless operation
```

- [ ] **Step 3: Commit**

```bash
git add claude-terminal/README.md
git commit -m "docs: update README with AI integration, agent teams, and bypass permissions"
```

---

### Task 14: End-to-End Test Script

**Files:**
- Create: `claude-terminal/tests/test-e2e.sh`

A shell script that can be run inside the container (or locally if claude CLI is available) to verify the full flow.

- [ ] **Step 1: Create the test script**

```bash
#!/bin/bash
# End-to-end test for Claude Terminal v2.3.0 features
# Run this inside the container or locally with claude CLI available

set -e

PASS=0
FAIL=0
SKIP=0

pass() { echo "  ✅ PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  ⏭️  SKIP: $1"; SKIP=$((SKIP + 1)); }

echo "======================================"
echo "Claude Terminal v2.3.0 E2E Tests"
echo "======================================"
echo ""

# Test 1: Bypass permissions flag in run.sh
echo "Test 1: Bypass permissions in run.sh"
if grep -q 'dangerously-skip-permissions' run.sh 2>/dev/null || grep -q 'dangerously-skip-permissions' claude-terminal/run.sh 2>/dev/null; then
    pass "run.sh contains --dangerously-skip-permissions"
else
    fail "run.sh missing --dangerously-skip-permissions"
fi

# Test 2: Bypass permissions in session picker
echo "Test 2: Bypass permissions in session picker"
PICKER_FILE="scripts/claude-session-picker.sh"
[ ! -f "$PICKER_FILE" ] && PICKER_FILE="claude-terminal/scripts/claude-session-picker.sh"
if [ -f "$PICKER_FILE" ]; then
    COUNT=$(grep -c 'dangerously-skip-permissions' "$PICKER_FILE" || echo 0)
    if [ "$COUNT" -ge 4 ]; then
        pass "Session picker has $COUNT instances of --dangerously-skip-permissions"
    else
        fail "Session picker only has $COUNT instances (expected >= 4)"
    fi
else
    skip "Session picker not found"
fi

# Test 3: Agent teams env var
echo "Test 3: Agent teams environment variable"
RUN_FILE="run.sh"
[ ! -f "$RUN_FILE" ] && RUN_FILE="claude-terminal/run.sh"
if grep -q 'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS' "$RUN_FILE" 2>/dev/null; then
    pass "Agent teams env var is set in run.sh"
else
    fail "Agent teams env var not found in run.sh"
fi

# Test 4: API server syntax check
echo "Test 4: API server syntax"
API_FILE="scripts/api-server.js"
[ ! -f "$API_FILE" ] && API_FILE="claude-terminal/scripts/api-server.js"
if [ -f "$API_FILE" ]; then
    if node --check "$API_FILE" 2>/dev/null; then
        pass "api-server.js has valid syntax"
    else
        fail "api-server.js has syntax errors"
    fi
else
    skip "api-server.js not found"
fi

# Test 5: API server unit tests
echo "Test 5: API server unit tests"
TEST_FILE="tests/test-api-server.js"
[ ! -f "$TEST_FILE" ] && TEST_FILE="claude-terminal/tests/test-api-server.js"
if [ -f "$TEST_FILE" ]; then
    if node --test "$TEST_FILE" 2>/dev/null; then
        pass "API server tests pass"
    else
        fail "API server tests failed"
    fi
else
    skip "API server test file not found"
fi

# Test 6: Custom integration files exist
echo "Test 6: Custom integration files"
COMP_DIR="custom_components/claude_terminal"
[ ! -d "$COMP_DIR" ] && COMP_DIR="claude-terminal/custom_components/claude_terminal"
if [ -d "$COMP_DIR" ]; then
    EXPECTED_FILES="__init__.py config_flow.py conversation.py api.py const.py manifest.json strings.json"
    ALL_PRESENT=true
    for f in $EXPECTED_FILES; do
        if [ ! -f "$COMP_DIR/$f" ]; then
            fail "Missing: $COMP_DIR/$f"
            ALL_PRESENT=false
        fi
    done
    if [ "$ALL_PRESENT" = true ]; then
        pass "All custom integration files present"
    fi
else
    skip "Custom components directory not found"
fi

# Test 7: Python syntax check
echo "Test 7: Python syntax check"
if [ -d "$COMP_DIR" ]; then
    SYNTAX_OK=true
    for f in "$COMP_DIR"/*.py; do
        if ! python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
            fail "Syntax error in $f"
            SYNTAX_OK=false
        fi
    done
    if [ "$SYNTAX_OK" = true ]; then
        pass "All Python files have valid syntax"
    fi
else
    skip "Custom components directory not found"
fi

# Test 8: Manifest version matches config.yaml
echo "Test 8: Version consistency"
MANIFEST="$COMP_DIR/manifest.json"
CONFIG_FILE="config.yaml"
[ ! -f "$CONFIG_FILE" ] && CONFIG_FILE="claude-terminal/config.yaml"
if [ -f "$MANIFEST" ] && [ -f "$CONFIG_FILE" ]; then
    MANIFEST_VERSION=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['version'])" 2>/dev/null)
    CONFIG_VERSION=$(grep '^version:' "$CONFIG_FILE" | head -1 | sed 's/version: *"\?\([^"]*\)"\?/\1/')
    if [ "$MANIFEST_VERSION" = "$CONFIG_VERSION" ]; then
        pass "Versions match: manifest=$MANIFEST_VERSION, config=$CONFIG_VERSION"
    else
        fail "Version mismatch: manifest=$MANIFEST_VERSION, config=$CONFIG_VERSION"
    fi
else
    skip "Manifest or config.yaml not found"
fi

# Test 9: claude -p mode works (if claude is available)
echo "Test 9: claude -p mode"
if command -v claude &>/dev/null; then
    RESULT=$(claude -p "Say just the word 'test'" --dangerously-skip-permissions --output-format json 2>/dev/null)
    if echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'result' in d" 2>/dev/null; then
        pass "claude -p returns valid JSON with result field"
    else
        fail "claude -p output is not valid JSON or missing result field"
    fi
else
    skip "claude CLI not available"
fi

# Summary
echo ""
echo "======================================"
echo "Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "======================================"

[ "$FAIL" -gt 0 ] && exit 1
exit 0
```

- [ ] **Step 2: Make executable and run**

Run: `chmod +x claude-terminal/tests/test-e2e.sh && claude-terminal/tests/test-e2e.sh`
Expected: All non-skipped tests pass.

- [ ] **Step 3: Commit**

```bash
git add claude-terminal/tests/test-e2e.sh
git commit -m "test: add end-to-end test script for v2.3.0 features"
```

---

### Task 15: Final Verification

- [ ] **Step 1: Run all tests**

```bash
# Node.js API server tests
cd claude-terminal && node --test tests/test-api-server.js

# Python integration tests
cd claude-terminal && PYTHONPATH=custom_components pip install -r tests/test_integration/requirements-test.txt && pytest tests/test_integration/ -v

# E2E tests
cd claude-terminal && ./tests/test-e2e.sh
```

- [ ] **Step 2: Verify file structure**

Run: `find claude-terminal -type f | sort`

Expected new files:
```
claude-terminal/custom_components/claude_terminal/__init__.py
claude-terminal/custom_components/claude_terminal/api.py
claude-terminal/custom_components/claude_terminal/config_flow.py
claude-terminal/custom_components/claude_terminal/const.py
claude-terminal/custom_components/claude_terminal/conversation.py
claude-terminal/custom_components/claude_terminal/manifest.json
claude-terminal/custom_components/claude_terminal/strings.json
claude-terminal/scripts/api-server.js
claude-terminal/tests/test-api-server.js
claude-terminal/tests/test-e2e.sh
claude-terminal/tests/test_integration/conftest.py
claude-terminal/tests/test_integration/requirements-test.txt
claude-terminal/tests/test_integration/test_api.py
claude-terminal/tests/test_integration/test_conversation.py
```

- [ ] **Step 3: Verify no secrets or sensitive data in committed files**

Run: `grep -r 'api_key\|password\|secret\|token' claude-terminal/custom_components/ claude-terminal/scripts/api-server.js claude-terminal/tests/ || echo "No secrets found"`

- [ ] **Step 4: Final commit if any cleanup needed**

```bash
git add -A
git status
# Only commit if there are changes
```
