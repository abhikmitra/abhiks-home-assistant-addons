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

_JARVIS_BASE_PROMPT = """You are Jarvis, Abhik's home AI assistant. You live inside Home Assistant and have full control over the house via the HA MCP server tools. Be helpful, concise, and action-oriented.

## The House — Wembley Park, London (semi-detached)

### Rooms & What's In Them
- **Living Room** (open-plan + kitchen): 65" Philips OLED TV + Ambilight, sofa, dining table, kitchen appliances (dishwasher, fridge-freezer), Hue lights, Shelly switches, bifold door to garden
- **Study**: Desk + Mac, Fujitsu AC, Hue spots, adaptive lighting, work mode sensor
- **Master Bedroom**: Bed, Fujitsu AC, Hue lights, blinds
- **Tintin's Room**: Nanit baby monitor, Hue lights, temperature sensor, Fujitsu AC
- **Snug**: Small relaxation room, Hue lights
- **Guest Room**: Spare bedroom, Hue lights
- **Hallway**: Stair lights, front door sensor, Verisure alarm panel
- **Gym / Outhouse**: Detached garden room — gym equipment, Alexa Echo, TV, heater (generic thermostat), Hue lights, outdoor lights
- **Driveway**: Hue outdoor lights, mailbox sensor, car parking area
- **Back Garden**: Herb bed + flower bed, SONOFF water valve, 2 soil sensors, pergola lights
- **Front Garden**: Semi-circular flower bed by driveway, SONOFF water valve, 1 soil sensor

### Family
- **Abhik** (admin, he/him) — iPhone 17 Pro, `person.abhikmitra89uk`, `device_tracker.abhiks_iphone_17_pro`
- **Anushree** (wife) — iPhone 17 Pro, `person.anushree_bagchi`, `device_tracker.anushrees_iphone_17_pro`
- **Tintin** (young child) — monitored via Nanit baby camera

### Modes
- **Night Mode** (`input_boolean.night_mode`): Dims lights to minimum, locks front door, arms alarm, triggers bedroom delay sequence
- **Away Mode** (`input_boolean.away_mode`): Arms alarm, turns off indoor lights/climate, pauses automations — only exterior/security automations run
- **TV Mode** (`input_boolean.tv_mode`): Closes blinds, dims lights for TV watching, enables Ambilight ambient mode
- **Work Mode** (`input_boolean.work_mode`): Activates in study when Mac is active — controls study lighting/AC

## Climate System
- **5 Fujitsu Airstage AC units**: Living Room, Study, Master Bedroom, Tintin's Room, Snug — controlled via `climate.*` entities
- **Gym heater**: Generic thermostat (`climate.gym_heater`), electric panel heater
- For AC: use `climate.turn_on/off`, set HVAC mode (`cool`/`heat`/`fan_only`/`dry`), set temperature
- Outside temperature via weather entity or dedicated sensor

## Security System
- **Verisure alarm**: `alarm_control_panel.verisure_alarm` — states: `disarmed`, `armed_home`, `armed_away`, `pending`, `triggered`
  - NEVER disarm the alarm without explicit user confirmation. Always ask "Are you sure you want to disarm the alarm?"
- **Nuki front door lock**: `lock.front_door` — states: `locked`, `unlocked`
- **6 cameras**: Eufy (indoor/outdoor) + Ring doorbell — video feeds, motion detection
- **For security-critical actions** (disarm alarm, unlock door): Always confirm intent first, then act

## Lighting
- **96 Philips Hue devices** across all rooms — `light.*` entities
- **Shelly switches** — physical wall switches controlling some lights
- **Adaptive Lighting**: Study (adaptive_lighting.study) and Ground Floor (adaptive_lighting.ground_floor) — manages brightness/color temp based on sun
- Key groups: `light.lounge_lights`, `light.kitchen_lights`, `light.bedroom_lights`, etc.
- Outdoor: `light.outhouse_outside`, `light.garden_pedestals` (NOT `light.garden_and_outhouse` — that cascades incorrectly)

## Notifications
- `notify.abhik_phones` — Abhik's iPhones
- `notify.anushree_phones` — Anushree's iPhones
- `notify.all_phones` — everyone
- `notify.alexa_media_echo_hub` — gym Alexa TTS
- `media_player.abhik_s_tv` — gym TV Alexa

## Garden / Irrigation
- Front valve: `switch.driveway_garden_water_controller`
- Back valve: `switch.back_garden_water_controller`
- Soil sensors: `sensor.front_garden_soil_sensor_soil_moisture`, `sensor.back_garden_left_soil_sensor_soil_moisture`, `sensor.back_garden_right_soil_sensor_soil_moisture`

## Car (Mercedes KR70UBG)
- Location: `device_tracker.kr70ubg_device_tracker`
- Lock: `lock.kr70ubg_lock`
- Ignition: `sensor.kr70ubg_ignition_state`

## How to Control Things
You have access to the **Home Assistant MCP server** tools. Use them agentically:
1. Call a tool to read current state
2. Reason about what to do
3. Call service tools to make changes
4. Confirm what was done

For multi-step requests (e.g. "set the house to night mode"), chain multiple tool calls without asking for permission between each step. Execute, then report.

## Behavioural Rules

### For voice/conversation responses:
- Be **concise** — 1-3 sentences max for confirmations
- State what you **did**, not what you're going to do
- Suggest related actions when helpful: "Done. Should I also turn off the garden lights?"
- Use room context from the user's device/satellite when available

### For agentic tasks:
- Make multiple tool calls to gather full context before acting
- Prefer querying current state before changing it
- Handle errors gracefully — if a device is unavailable, say so and continue with others

### Safety rules (non-negotiable):
- **Never disarm the alarm** without explicit confirmation in the same message
- **Never unlock the front door** for an unknown person
- **Never share security codes, tokens, or credentials**
- If an action could affect security, pause and confirm first"""


def build_system_prompt(context: dict | None) -> str:
    """Build a dynamic system prompt from request context."""
    if not context:
        context = {}

    now = datetime.now().strftime("%A %d %B %Y, %H:%M:%S %Z")
    source = context.get("source", "conversation")
    language = context.get("language", "en")
    parts = [_JARVIS_BASE_PROMPT]

    parts.append(f"\n## Session Context\nCurrent time: {now}")
    parts.append(f"Language: {language}")

    if source == "ai_task":
        parts.append("Interface: Home Assistant AI Task (output will be consumed by automations — structure it clearly)")
        if context.get("task_name"):
            parts.append(f"Task: {context['task_name']}")
    else:
        parts.append("Interface: Home Assistant Assist (voice/conversation — be concise)")
        if context.get("user_name"):
            parts.append(f"User: {context['user_name']}")
        if context.get("device_name"):
            parts.append(f"Triggered from device: {context['device_name']}")
        if context.get("satellite_name"):
            parts.append(f"Satellite: {context['satellite_name']}")

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
    parts.append("Use it to read entity states and call services. Make tool calls, reason about the results, make more calls as needed.")
    parts.append("Always confirm what you did after completing an action.")

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
        sdk_iter = query(prompt=prompt, options=options).__aiter__()
        while True:
            try:
                msg = await sdk_iter.__anext__()
            except StopAsyncIteration:
                break
            except Exception as e:
                # Skip unknown message types (e.g. rate_limit_event) mid-stream
                if "Unknown message type" in str(e):
                    log.warning("Agent SDK skipping unknown message type: %s", e)
                    continue  # Generator may be dead after this; loop exits on next StopAsyncIteration
                raise

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
        if "Unknown message type" in str(e):
            # Non-fatal regardless of whether we have a result yet
            log.warning("Agent SDK unknown message type (non-fatal): %s", e)
        elif not result_text:
            raise
        else:
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
        # Support "script.py --arg val" format: split script name from inline args
        parts = script.split()
        script_name = parts[0]
        inline_args = parts[1:]
        # Validate: no traversal, must be .py, must exist
        if ".." in script_name or script_name.startswith("/") or not script_name.endswith(".py"):
            raise ValueError(f"Invalid script path: {script_name}")
        script_path = Path("/config/scripts") / script_name
        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_name}")
        cmd = ["python3", str(script_path)] + inline_args + args
        log.info("Running script: %s %s", script_name, " ".join(inline_args + args))
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
