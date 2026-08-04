#!/usr/bin/env python3
"""Small LAN bridge that lets Home Assistant trigger allowlisted Codex tasks.

The bridge runs on Abhik's Mac and reuses the existing authenticated Codex CLI.
Home Assistant only gets named allowlisted tasks, not arbitrary shell access.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = os.environ.get("CODEX_HA_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CODEX_HA_BRIDGE_PORT", "8766"))
ALLOWED_CLIENTS = {
    item.strip()
    for item in os.environ.get(
        "CODEX_HA_ALLOWED_CLIENTS", "127.0.0.1,::1,192.168.4.62"
    ).split(",")
    if item.strip()
}

CODEX_BIN = Path(os.environ.get("CODEX_BIN", "/Users/abhikmitra/.local/bin/codex"))
HA_CONFIG_REPO = Path(
    os.environ.get("HA_CONFIG_REPO", "/Users/abhikmitra/Github/home-assistant-configs")
)
HA_SSH_HOST = os.environ.get("HA_SSH_HOST", "root@192.168.4.62")
HASS_SERVER = os.environ.get("HASS_SERVER", "http://192.168.4.62:8123").rstrip("/")
HASS_TOKEN = os.environ.get("HASS_TOKEN", "")
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_HA_TIMEOUT_SECONDS", "420"))
MAX_OUTPUT_CHARS = int(os.environ.get("CODEX_HA_MAX_OUTPUT_CHARS", "12000"))
CODEX_REASONING_EFFORT = os.environ.get("CODEX_HA_REASONING_EFFORT", "low")

RUN_LOCK = threading.Lock()

CANARY_RESULT_ENTITY = "input_text.codex_agent_last_result"
CANARY_SCRIPT_ENTITY = "script.codex_bridge_trigger_canary"
CODEX_A2A_CORRELATION_ENTITY = "input_text.codex_a2a_last_correlation_id"
CODEX_A2A_STATUS_ENTITY = "input_text.codex_a2a_last_status"
CODEX_A2A_RESULT_ENTITY = "input_text.codex_a2a_last_result"
PORTAL_CODEX_LAST_QUERY_ENTITY = "input_text.portal_codex_last_query"
PORTAL_CODEX_LAST_SPEECH_ENTITY = "input_text.portal_codex_last_speech"
PORTAL_CODEX_VIEW_ENTITY = "input_text.portal_codex_view"
HA_SSH_HOST = os.environ.get("CODEX_HA_SSH", "root@192.168.4.62")
PORTAL_CODEX_LAST_SCREEN_ENTITIES = [
    "input_text.portal_codex_last_screen_1",
    "input_text.portal_codex_last_screen_2",
    "input_text.portal_codex_last_screen_3",
]
BLOCKED_SERVICE_DOMAINS = {"alarm_control_panel"}
BLOCKED_ENTITY_PREFIXES = ("alarm_control_panel.",)


SELECTED_STATE_ENTITIES = [
    "alarm_control_panel.verisure_alarm",
    "lock.front_door",
    "input_boolean.away_mode",
    "input_boolean.night_mode",
    "input_boolean.tv_mode",
    "input_boolean.work_mode",
    "sensor.master_bedroom_temperature",
    "sensor.tintins_room_temperature",
    "sensor.gym_temperature",
    "climate.living_room",
    "climate.study",
    "switch.driveway_garden_water_controller",
    "switch.back_garden_water_controller",
    "sensor.front_garden_soil_sensor_soil_moisture",
    "sensor.back_garden_left_soil_sensor_soil_moisture",
    "sensor.back_garden_right_soil_sensor_soil_moisture",
    "device_tracker.kr70ubg_device_tracker",
    "sensor.kr70ubg_fuel_level_percent",
]


def run_command(args: list[str], timeout: int = 20) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-MAX_OUTPUT_CHARS:],
            "stderr": proc.stderr[-MAX_OUTPUT_CHARS:],
            "duration_ms": int((time.time() - started) * 1000),
        }
    except Exception as exc:
        return {
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "duration_ms": int((time.time() - started) * 1000),
        }


def ha_get(path: str, timeout: int = 15) -> Any:
    if not HASS_TOKEN:
        return {"error": "HASS_TOKEN not available to bridge process"}
    request = urllib.request.Request(
        f"{HASS_SERVER}/api{path}",
        headers={"Authorization": f"Bearer {HASS_TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def ha_post(path: str, data: dict[str, Any], timeout: int = 15) -> Any:
    if not HASS_TOKEN:
        return {"error": "HASS_TOKEN not available to bridge process"}
    payload = json.dumps(data).encode("utf-8")
    request = urllib.request.Request(
        f"{HASS_SERVER}/api{path}",
        data=payload,
        headers={
            "Authorization": f"Bearer {HASS_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8")
        return json.loads(text) if text else {}


def get_selected_states() -> dict[str, Any]:
    states: dict[str, Any] = {}
    for entity_id in SELECTED_STATE_ENTITIES:
        try:
            data = ha_get(f"/states/{entity_id}")
            states[entity_id] = {
                "state": data.get("state"),
                "attributes": {
                    key: value
                    for key, value in data.get("attributes", {}).items()
                    if key
                    in {
                        "friendly_name",
                        "current_temperature",
                        "temperature",
                        "hvac_action",
                        "battery_level",
                        "unit_of_measurement",
                    }
                },
            }
        except Exception as exc:
            states[entity_id] = {"error": str(exc)}
    return states


def get_light_count() -> dict[str, Any]:
    try:
        all_states = ha_get("/states")
        lights_on = [
            item["entity_id"]
            for item in all_states
            if item.get("entity_id", "").startswith("light.")
            and item.get("state") == "on"
        ]
        return {"count": len(lights_on), "entities": lights_on[:30]}
    except Exception as exc:
        return {"error": str(exc)}


def get_repo_evidence() -> dict[str, Any]:
    return {
        "status": run_command(["git", "-C", str(HA_CONFIG_REPO), "status", "--short", "--branch"]),
        "recent_commits": run_command(
            ["git", "-C", str(HA_CONFIG_REPO), "log", "--oneline", "-5"]
        ),
    }


def get_ha_logs(lines: int = 220) -> dict[str, Any]:
    return run_command(
        ["/usr/bin/ssh", HA_SSH_HOST, f"ha core logs | tail -{int(lines)}"],
        timeout=45,
    )


def build_evidence(task: str, payload: dict[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "task": task,
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "repo": get_repo_evidence(),
    }
    if task in {"home_status", "debug_recent_automation", "ask_jarvis", "portal_voice_codex"}:
        evidence["states"] = get_selected_states()
        evidence["lights_on"] = get_light_count()
    if task in {"smart_log_analysis", "debug_recent_automation"}:
        evidence["ha_logs"] = get_ha_logs()
    if task == "debug_recent_automation":
        question = str(payload.get("question", ""))[:500]
        evidence["question"] = question or "Debug the most recent relevant automation issue."
    if task == "ask_jarvis":
        evidence["question"] = str(payload.get("question", ""))[:1000]
        evidence["context"] = {
            key: str(payload.get(key, ""))[:200]
            for key in ("user_name", "device_name", "satellite_name", "language")
            if payload.get(key)
        }
    if task == "portal_voice_codex":
        evidence["question"] = str(payload.get("question", ""))[:1000]
        evidence["context"] = {
            key: str(payload.get(key, ""))[:200]
            for key in (
                "user_name",
                "device_name",
                "satellite_name",
                "language",
                "satellite_entity_id",
            )
            if payload.get(key)
        }
    if task == "codex_a2a_echo":
        evidence["correlation_id"] = str(payload.get("correlation_id", ""))[:200]
        evidence["question"] = str(payload.get("question") or "hello")[:500]
        evidence["source"] = str(payload.get("source") or "ha_dashboard")[:100]
    return evidence


def build_prompt(task: str, evidence: dict[str, Any]) -> str:
    if task == "codex_echo":
        return (
            'Return only this JSON object: {"ok": true, "task": "codex_echo", '
            '"summary": "Codex bridge is working"}'
        )

    if task == "codex_a2a_echo":
        correlation_id = str(evidence.get("correlation_id") or "").strip()
        question = str(evidence.get("question") or "hello").strip()
        return f"""Home Assistant A2A E2E test.

Rules:
- This request started in Home Assistant, not Telegram.
- Return ONLY valid JSON.
- Preserve the correlation ID exactly.
- Do not call tools or change Home Assistant.

Required response shape:
{{
  "ok": true,
  "task": "codex_a2a_echo",
  "correlation_id": {json.dumps(correlation_id)},
  "summary": "HA A2A CODEX REPLY OK {correlation_id}",
  "route": "HA UI -> Mac Codex bridge -> local Codex JSONL -> HA result"
}}

Correlation ID: {correlation_id}
User-visible prompt: {question}
Exact phrase required in summary: HA A2A CODEX REPLY OK

Evidence JSON:
{json.dumps(evidence, indent=2, sort_keys=True)}
"""

    if task == "portal_voice_codex":
        return f"""You are Codex running behind a Home Assistant voice portal.

Rules:
- Read-only analysis only.
- Use only the evidence provided below.
- Do not ask for secrets.
- Do not claim that you changed Home Assistant.
- Write for spoken household use.
- Keep `speech_text` concise and natural for TTS.
- Keep `screen_text` concise enough to fit a household dashboard card split across a few short lines.
- Return ONLY valid JSON with this shape:
  {{
    "ok": true,
    "task": "portal_voice_codex",
    "summary": "one short summary",
    "speech_text": "short spoken answer",
    "screen_text": "slightly longer screen answer",
    "follow_up_suggestions": ["short follow-up", "..."],
    "risk": "low|medium|high",
    "actions": []
  }}

Task:
Answer the user's spoken request for the Portal. If the answer is uncertain, say so clearly.
Prefer practical household reasoning over broad exposition. `actions` must stay empty.

Evidence JSON:
{json.dumps(evidence, indent=2, sort_keys=True)}
"""

    task_instructions = {
        "home_status": (
            "Summarize the current home status for a Home Assistant dashboard. "
            "Flag anomalies in security, modes, lights, temperatures, garden, car, "
            "and repo cleanliness. Do not suggest device control unless necessary."
        ),
        "smart_log_analysis": (
            "Analyze the Home Assistant logs. Filter known routine noise. "
            "Return only actionable issues, likely causes, and suggested next steps. "
            "Do not propose edits unless there is clear evidence."
        ),
        "debug_recent_automation": (
            "Use the supplied states, logs, and question to explain what likely happened. "
            "Focus on automations/scripts and produce concise five-why style reasoning "
            "when useful."
        ),
        "ask_jarvis": (
            "Answer the user's Home Assistant question conversationally for the Ask Jarvis "
            "dashboard card. Use only the supplied evidence. This bridge is currently "
            "read-only: if the user asks to change a device, say you can inspect state "
            "from this card but cannot change devices from this path yet."
        ),
    }

    return f"""You are Codex running from a Home Assistant dashboard bridge.

Rules:
- Read-only analysis only.
- Use only the evidence provided below.
- Do not ask for secrets.
- Do not claim that you changed Home Assistant.
- Return ONLY valid JSON with this shape:
  {{
    "ok": true,
    "task": "{task}",
    "summary": "one short dashboard-safe summary",
    "findings": ["short finding", "..."],
    "next_actions": ["short next action", "..."],
    "risk": "low|medium|high"
  }}

Task:
{task_instructions[task]}

Evidence JSON:
{json.dumps(evidence, indent=2, sort_keys=True)}
"""


def extract_json_object(output: str) -> dict[str, Any] | None:
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    in_string = False
    escaped = False

    for idx, char in enumerate(output):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            stack.append(idx)
        elif char == "}" and stack:
            start = stack.pop()
            if not stack:
                spans.append((start, idx + 1))

    for start, end in reversed(spans):
        try:
            return json.loads(output[start:end])
        except json.JSONDecodeError:
            continue
    return None


def run_codex(prompt: str, persist_session: bool = False) -> dict[str, Any]:
    started = time.time()
    cmd = [
        str(CODEX_BIN),
        "-a",
        "never",
        "-s",
        "read-only",
        "-c",
        f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
        "exec",
    ]
    if not persist_session:
        cmd.append("--ephemeral")
    cmd.extend(
        [
        "-C",
        str(HA_CONFIG_REPO),
        "-",
        ]
    )
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        timeout=CODEX_TIMEOUT_SECONDS,
        env=os.environ.copy(),
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    parsed = extract_json_object(output)
    return {
        "ok": proc.returncode == 0 and parsed is not None,
        "exit_code": proc.returncode,
        "duration_ms": int((time.time() - started) * 1000),
        "data": parsed,
        "raw_tail": output[-MAX_OUTPUT_CHARS:],
    }


def chunk_text(text: str, chunk_size: int = 240, max_chunks: int = 3) -> list[str]:
    compact = " ".join(str(text).split()).strip()
    if not compact:
        return [""] * max_chunks

    chunks: list[str] = []
    remaining = compact
    while remaining and len(chunks) < max_chunks:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            remaining = ""
            break

        split_at = remaining.rfind(" ", 0, chunk_size + 1)
        if split_at <= 0:
            split_at = chunk_size
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining and chunks:
        available = max(0, chunk_size - 1)
        chunks[-1] = (chunks[-1][:available].rstrip() + "…") if available else "…"

    while len(chunks) < max_chunks:
        chunks.append("")
    return chunks[:max_chunks]


def publish_to_ha(task: str, result: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
    data = result.get("data") or {}
    payload = payload or {}
    summary = str(data.get("summary") or "Codex task completed")[:240]
    title = f"Codex: {task.replace('_', ' ').title()}"
    try:
        ha_post(
            "/services/persistent_notification/create",
            {
                "title": title,
                "message": summary,
                "notification_id": f"codex_{task}",
            },
        )
    except Exception:
        pass

    if task == "codex_a2a_echo":
        correlation_id = str(
            data.get("correlation_id") or payload.get("correlation_id") or ""
        )[:240]
        route = str(
            data.get("route")
            or "HA UI -> Mac Codex bridge -> local Codex JSONL -> HA result"
        )
        status_prefix = "PASS" if result.get("data") and data.get("ok") is not False else "FAIL"
        status = f"{status_prefix}: {route}"[:240]
        a2a_result = str(data.get("summary") or summary)[:240]

        for entity_id, value in [
            (CODEX_A2A_CORRELATION_ENTITY, correlation_id),
            (CODEX_A2A_STATUS_ENTITY, status),
            (CODEX_A2A_RESULT_ENTITY, a2a_result),
            (CANARY_RESULT_ENTITY, a2a_result),
        ]:
            try:
                ha_post(
                    "/services/input_text/set_value",
                    {"entity_id": entity_id, "value": value},
                )
            except Exception:
                pass
        return

    if task != "portal_voice_codex":
        return

    query = str(payload.get("question") or "")[:240]
    speech_text = str(data.get("speech_text") or summary)[:240]
    screen_text = str(data.get("screen_text") or speech_text)

    for entity_id, value in [
        (PORTAL_CODEX_LAST_QUERY_ENTITY, query),
        (PORTAL_CODEX_LAST_SPEECH_ENTITY, speech_text),
    ]:
        try:
            ha_post(
                "/services/input_text/set_value",
                {"entity_id": entity_id, "value": value},
            )
        except Exception:
            pass

    for entity_id, value in zip(PORTAL_CODEX_LAST_SCREEN_ENTITIES, chunk_text(screen_text)):
        try:
            ha_post(
                "/services/input_text/set_value",
                {"entity_id": entity_id, "value": value},
            )
        except Exception:
            pass

    satellite_entity_id = str(payload.get("satellite_entity_id") or "").strip()
    if satellite_entity_id:
        try:
            ha_post(
                "/services/assist_satellite/announce",
                {
                    "entity_id": satellite_entity_id,
                    "message": speech_text,
                    "preannounce": False,
                },
            )
        except Exception:
            pass

    try:
        ha_post(
            "/services/input_text/set_value",
            {
                "entity_id": CANARY_RESULT_ENTITY,
                "value": summary,
            },
        )
    except Exception:
        pass


def canary_value(payload: dict[str, Any], default_prefix: str) -> str:
    value = str(payload.get("value") or "").strip()
    if not value:
        value = f"{default_prefix} {int(time.time())}"
    return value[:240]


def valid_service_part(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char == "_" for char in value)


def dict_payload(payload: dict[str, Any], key: str) -> tuple[dict[str, Any] | None, str | None]:
    value = payload.get(key)
    if value is None:
        return {}, None
    if isinstance(value, dict):
        return value, None
    return None, f"{key} must be an object"


def contains_blocked_alarm_reference(value: Any) -> bool:
    if isinstance(value, str):
        return any(value.startswith(prefix) for prefix in BLOCKED_ENTITY_PREFIXES)
    if isinstance(value, list):
        return any(contains_blocked_alarm_reference(item) for item in value)
    if isinstance(value, dict):
        return any(contains_blocked_alarm_reference(item) for item in value.values())
    return False


def handle_ha_service_call(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    domain = str(payload.get("domain") or "").strip()
    service = str(payload.get("service") or "").strip()
    if not valid_service_part(domain) or not valid_service_part(service):
        return 400, {"ok": False, "error": "domain and service are required"}
    if domain in BLOCKED_SERVICE_DOMAINS:
        return 400, {"ok": False, "error": "alarm_control_panel services are blocked"}

    target, error = dict_payload(payload, "target")
    if error:
        return 400, {"ok": False, "error": error}
    data_key = "data" if "data" in payload else "service_data"
    service_data, error = dict_payload(payload, data_key)
    if error:
        return 400, {"ok": False, "error": error}

    assert target is not None
    assert service_data is not None
    if contains_blocked_alarm_reference(target) or contains_blocked_alarm_reference(service_data):
        return 400, {
            "ok": False,
            "error": "alarm_control_panel entity targets are blocked",
        }

    ha_payload = {**service_data, **target}
    try:
        ha_response = ha_post(f"/services/{domain}/{service}", ha_payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-1000:]
        return exc.code, {
            "ok": False,
            "error": f"Home Assistant rejected {domain}.{service}",
            "detail": detail,
        }
    except Exception as exc:
        return 502, {
            "ok": False,
            "error": f"Home Assistant service call failed: {exc}",
        }

    response_count = len(ha_response) if isinstance(ha_response, list) else None
    return 200, {
        "ok": True,
        "task": "ha_service_call",
        "mode": "apply",
        "data": {
            "summary": f"Called {domain}.{service}",
            "service": f"{domain}.{service}",
            "target": target,
            "response_count": response_count,
        },
    }


def handle_apply_task(task: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if task == "ha_service_call":
        return handle_ha_service_call(payload)

    if task == "apply_canary":
        value = canary_value(payload, "Codex apply canary")
        ha_post(
            "/services/input_text/set_value",
            {"entity_id": CANARY_RESULT_ENTITY, "value": value},
        )
        return 200, {
            "ok": True,
            "task": task,
            "mode": "apply",
            "data": {
                "summary": f"Set {CANARY_RESULT_ENTITY}",
                "changed_entity": CANARY_RESULT_ENTITY,
                "value": value,
            },
        }

    if task == "trigger_canary_script":
        marker = canary_value(payload, "Codex trigger canary")
        ha_post(
            "/services/script/codex_bridge_trigger_canary",
            {"marker": marker},
        )
        return 200, {
            "ok": True,
            "task": task,
            "mode": "apply",
            "data": {
                "summary": f"Triggered {CANARY_SCRIPT_ENTITY}",
                "triggered_entity": CANARY_SCRIPT_ENTITY,
                "marker": marker,
            },
        }

    return 400, {"ok": False, "error": "unknown apply task"}


def capture_codex_view(question: str) -> Path | None:
    """Picture of what Codex is looking at when the Portal delegates to it.

    Primary: a real screenshot of the Mac (Codex's working environment).
    Fallback (no screen-recording TCC in the launchd context): a rendered
    status card with the question + working dir, so the Portal always gets
    a truthful view image rather than nothing.
    """
    out = Path("/tmp/portal-codex-view.jpg")
    try:
        proc = subprocess.run(
            ["screencapture", "-x", "-t", "jpg", str(out)],
            capture_output=True,
            timeout=10,
        )
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 60_000:
            # Portal webview is 1280px wide — a full-Retina 5MB+ jpg is waste
            subprocess.run(
                ["sips", "--resampleWidth", "1280", str(out)],
                capture_output=True,
                timeout=10,
            )
            return out
    except Exception:
        pass
    try:
        from PIL import Image, ImageDraw

        im = Image.new("RGB", (1280, 800), (14, 17, 20))
        d = ImageDraw.Draw(im)
        d.text((40, 60), "CODEX VIEW — Mac screen unavailable to bridge", fill=(245, 165, 36))
        d.text((40, 110), time.strftime("%Y-%m-%d %H:%M:%S"), fill=(154, 170, 187))
        d.text((40, 160), f"Working dir: {HA_CONFIG_REPO}", fill=(230, 236, 240))
        d.text((40, 220), "Question:", fill=(46, 200, 200))
        for row, i in enumerate(range(0, min(len(question), 400), 56)):
            d.text((40, 260 + row * 34), question[i : i + 56], fill=(230, 236, 240))
        im.save(out, quality=85)
        return out
    except Exception:
        return None


def publish_codex_view(question: str) -> None:
    img = capture_codex_view(question)
    value = ""
    if img is not None:
        try:
            ssh_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
            subprocess.run(
                ["ssh", *ssh_opts, HA_SSH_HOST, "mkdir -p /config/www/portal-codex"],
                capture_output=True,
                timeout=15,
            )
            up = subprocess.run(
                ["scp", *ssh_opts, str(img), f"{HA_SSH_HOST}:/config/www/portal-codex/view.jpg"],
                capture_output=True,
                timeout=25,
            )
            if up.returncode == 0:
                value = f"/local/portal-codex/view.jpg?t={int(time.time())}"
        except Exception:
            value = ""
    try:
        ha_post(
            "/services/input_text/set_value",
            {"entity_id": PORTAL_CODEX_VIEW_ENTITY, "value": value},
        )
    except Exception:
        pass


def handle_task(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    task = str(payload.get("task", "")).strip()
    mode = str(payload.get("mode", "read_only")).strip()

    read_only_tasks = {
        "ask_jarvis",
        "codex_a2a_echo",
        "codex_echo",
        "home_status",
        "portal_voice_codex",
        "smart_log_analysis",
        "debug_recent_automation",
    }
    apply_tasks = {
        "apply_canary",
        "ha_service_call",
        "trigger_canary_script",
    }
    allowed = read_only_tasks | apply_tasks
    if task not in allowed:
        return 400, {"ok": False, "error": "unknown task", "allowed_tasks": sorted(allowed)}
    if mode == "read_only" and task not in read_only_tasks:
        return 400, {"ok": False, "error": "task does not support read_only mode"}
    if mode == "apply" and task not in apply_tasks:
        return 400, {"ok": False, "error": "task does not support apply mode"}
    if mode not in {"read_only", "apply"}:
        return 400, {"ok": False, "error": "unsupported mode"}
    if not RUN_LOCK.acquire(blocking=False):
        return 503, {"ok": False, "error": "busy"}

    try:
        if mode == "apply":
            return handle_apply_task(task, payload)
        if task == "codex_a2a_echo":
            sys.stderr.write(
                "%s A2A task received correlation_id=%s source=%s\n"
                % (
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    str(payload.get("correlation_id", ""))[:200],
                    str(payload.get("source", ""))[:100],
                )
            )
        if task == "portal_voice_codex":
            publish_codex_view(str(payload.get("question") or ""))
        evidence = {} if task == "codex_echo" else build_evidence(task, payload)
        result = run_codex(
            build_prompt(task, evidence),
            persist_session=(task in {"codex_a2a_echo", "portal_voice_codex"}),
        )
        response = {
            "ok": result["ok"],
            "task": task,
            "mode": mode,
            "duration_ms": result["duration_ms"],
            "data": result["data"],
            "codex_exit_code": result["exit_code"],
        }
        publish_to_ha(task, result, payload)
        return (200 if result["ok"] else 502), response
    finally:
        RUN_LOCK.release()


def health_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "busy": RUN_LOCK.locked(),
        "host": HOST,
        "port": PORT,
        "allowed_clients": sorted(ALLOWED_CLIENTS),
        "codex_bin_exists": CODEX_BIN.exists(),
        "ha_config_repo_exists": HA_CONFIG_REPO.exists(),
        "hass_server": HASS_SERVER,
        "hass_token_available": bool(HASS_TOKEN),
        "codex_reasoning_effort": CODEX_REASONING_EFFORT,
        "tasks": [
            "apply_canary",
            "ask_jarvis",
            "codex_a2a_echo",
            "codex_echo",
            "ha_service_call",
            "home_status",
            "portal_voice_codex",
            "smart_log_analysis",
            "debug_recent_automation",
            "trigger_canary_script",
        ],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexHABridge/0.1"

    def _client_allowed(self) -> bool:
        return self.client_address[0] in ALLOWED_CLIENTS

    def _send(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._client_allowed():
            self._send(403, {"ok": False, "error": "forbidden"})
            return
        if self.path == "/api/health":
            self._send(200, health_payload())
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._client_allowed():
            self._send(403, {"ok": False, "error": "forbidden"})
            return
        if self.path != "/api/run-task":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 8192:
                self._send(413, {"ok": False, "error": "payload too large"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as exc:
            self._send(400, {"ok": False, "error": f"invalid json: {exc}"})
            return
        status, data = handle_task(payload)
        self._send(status, data)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(
            "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--health", action="store_true", help="print health JSON and exit")
    args = parser.parse_args()
    if args.health:
        print(json.dumps(health_payload(), indent=2))
        return 0

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Codex HA bridge listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
