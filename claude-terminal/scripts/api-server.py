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

_query_lock = asyncio.Lock()
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

    entities = context.get("exposed_entities", [])
    if entities:
        parts.append("")
        parts.append("## Exposed Home Assistant Entities")
        parts.append("These are the devices you can control (entity_id | name | current state):")
        for e in entities:
            parts.append(f"  - {e['entity_id']} | {e['name']} | {e['state']}")
        parts.append("")
        parts.append("## Tools Available")
        parts.append("You have access to the Home Assistant MCP server.")
        parts.append("Use it to read entity states and call services to control devices.")
        parts.append("When asked to control a device, call the appropriate service, then confirm what you did in one sentence.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Agent SDK Query
# ---------------------------------------------------------------------------

async def run_agent_query(prompt: str, context: dict | None, conversation_id: str | None) -> dict:
    """Run a query using the Claude Agent SDK."""
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage, SystemMessage

    system_prompt = build_system_prompt(context)
    oauth_token = get_oauth_token()

    options_kwargs = {
        "cli_path": "/usr/local/bin/claude",
        "max_turns": 10,
        "permission_mode": "bypassPermissions",
        "model": "haiku",
        "system_prompt": system_prompt,
    }

    # Pass OAuth token if explicitly available; otherwise let CLI use stored session
    if oauth_token:
        options_kwargs["env"] = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": oauth_token}

    if conversation_id:
        options_kwargs["resume"] = conversation_id

    options = ClaudeAgentOptions(**options_kwargs)

    result_text = ""
    session_id = None
    start_time = time.time()

    log.info("Starting Agent SDK query: prompt_length=%d, has_conversation_id=%s", len(prompt), bool(conversation_id))

    try:
        async for msg in query(prompt=prompt, options=options):
            msg_type = type(msg).__name__
            log.debug("SDK message: type=%s", msg_type)

            if isinstance(msg, AssistantMessage):
                # Primary path: SDK emits text via AssistantMessage.content[].text
                for block in msg.content:
                    if hasattr(block, 'text'):
                        result_text += block.text
                log.info("Got AssistantMessage: accumulated_length=%d", len(result_text))
            elif isinstance(msg, ResultMessage):
                # Fallback: use ResultMessage if it arrives (may not due to rate_limit_event)
                if msg.result:
                    result_text = msg.result
                log.info("Got ResultMessage: length=%d, stop_reason=%s", len(result_text), getattr(msg, 'stop_reason', 'unknown'))
            elif isinstance(msg, SystemMessage) and getattr(msg, 'subtype', '') == "init":
                session_id = getattr(msg, 'data', {}).get("session_id")
                log.info("Got session_id: %s", session_id)
    except Exception as e:
        # SDK may throw on unknown message types (e.g. rate_limit_event)
        # If we already have text, that's fine — the AI response came through
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
    return web.json_response({"status": "ok", "busy": _query_lock.locked()})


async def handle_query(request: web.Request) -> web.Response:
    """POST /api/query — run a Claude Agent SDK query."""
    request_id = uuid.uuid4().hex[:8]

    log.info("[%s] Query request received", request_id)

    if is_rate_limited():
        log.warning("[%s] Rate limited", request_id)
        return web.json_response(
            {"error": True, "message": "Rate limit exceeded. Max 10 requests per minute.", "code": 429},
            status=429,
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

    request_timestamps.append(time.time())

    async with _query_lock:
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
            # Stale session retry: if resume failed (e.g. terminal killed the session),
            # retry the same query without conversation_id (fresh session)
            if conversation_id and "exit code 1" in str(e):
                log.warning("[%s] Session resume failed (stale session), retrying without conversation_id: %s", request_id, e)
                try:
                    result = await asyncio.wait_for(
                        run_agent_query(query_text, context, None),
                        timeout=QUERY_TIMEOUT_S,
                    )
                    log.info("[%s] Fresh retry succeeded: session_id=%s, result_length=%d", request_id, result.get("session_id"), len(result.get("result", "")))
                    return web.json_response(result)
                except asyncio.TimeoutError:
                    log.error("[%s] Fresh retry timed out after %ds", request_id, QUERY_TIMEOUT_S)
                    return web.json_response(
                        {"error": True, "message": f"Query timed out after {QUERY_TIMEOUT_S} seconds", "code": 504},
                        status=504,
                    )
                except Exception as retry_e:
                    log.error("[%s] Fresh retry also failed: %s", request_id, retry_e, exc_info=True)
                    return web.json_response(
                        {"error": True, "message": str(retry_e), "code": 500},
                        status=500,
                    )
            log.error("[%s] Query failed: %s", request_id, e, exc_info=True)
            return web.json_response(
                {"error": True, "message": str(e), "code": 500},
                status=500,
            )


async def handle_run_script(request: web.Request) -> web.Response:
    """POST /api/run-script — execute a Python script or inline code."""
    request_id = uuid.uuid4().hex[:8]

    log.info("[%s] Run-script request received", request_id)

    if is_rate_limited():
        log.warning("[%s] Rate limited", request_id)
        return web.json_response(
            {"error": True, "message": "Rate limit exceeded.", "code": 429},
            status=429,
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

    request_timestamps.append(time.time())

    async with _query_lock:
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
