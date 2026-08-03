#!/usr/bin/env python3
"""Codex flap digest — the Portal Hub's "organic brain".

Wakes (interval / add-on start / manual flap-now), snapshots the house from
the Supervisor API, then lets Codex agentically decide the single most
valuable message for the split-flap board (5 rows x 17 cells = 85 chars)
and fires it as a portal_toast event.

Toast contract: Day2DayAgentHelp/Agent-Tooling/meta-portal/
portal-voice-states-and-toast-spec.md — coalesced by source_key, ambient
severity has a 45-min TTL so a periodic run naturally replaces itself.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ROWS = 5
COLS = 17
CHARSET = re.compile(r"[^A-Z0-9 %:'\-]")

STATE_ENTITIES = {
    "night_mode": "input_boolean.night_mode",
    "away_mode": "input_boolean.away_mode",
    "tv_mode": "input_boolean.tv_mode",
    "barbecue_mode": "input_boolean.barbecue_mode",
    "abhik": "person.abhikmitra89uk",
    "anushree": "person.anushree_bagchi",
    "malay": "person.malay_mitra",
    "volvo_location": "device_tracker.volvo_xc60_location",
    "volvo_battery_pct": "sensor.volvo_xc60_battery",
    "volvo_charging_power": "sensor.volvo_xc60_charging_power",
    "volvo_range_est": "sensor.volvo_xc60_distance_to_empty_battery",
    "volvo_charging_now": "binary_sensor.volvo_charging_started",
    "weather": "weather.forecast_home",
}

PROMPT_TEMPLATE = """\
You are the Portal Hub's organic brain — the flap-aware side of Jarvis, \
waking up inside the house's Home Assistant box for one job: decide what \
the living-room split-flap board should say right now, and say it.

WAKE REASON: {reason}
LOCAL TIME: {now}

THE DISPLAY: a physical Solari split-flap board on the family Portal. \
EXACTLY 5 rows x 17 boxes = 85 characters total budget. Uppercase. Only \
letters, digits, spaces and % : ' - survive; every other character is \
dropped before render. No single word longer than 17 characters. Text \
wraps word-safe across rows.

THE HOUSE, live snapshot:
{state}

RECENTLY SHOWN on the board — do NOT repeat any of these:
{recent}

You are running inside the Home Assistant config directory: explore the \
YAML here if it helps you understand a state (rooms, automations, what an \
entity means). Household: Abhik, Anushree, their toddler Tintin (the \
nursery is Tintin's room), and Malay (Abhik's father). The car is a Volvo \
XC60 EV. To-dos are already shown elsewhere on this screen.

Pick the SINGLE most valuable message, preferring in this order:
1. A warning or unusual state (charging fault, odd mode combo, something \
in the snapshot that looks wrong).
2. A meaningful transition: someone reached the office or got home, car \
started charging, charge complete with the %.
3. Something useful for whoever is OUT of the house right now.
4. A weather change worth acting on: rain coming, unusual temp swing.
5. A piece of news you genuinely know and are confident of — never \
invent, guess, or dress something up as breaking news.
6. Otherwise: a funny quip or an inspirational quote — vary the flavour \
run to run.

NEVER: to-dos, "everyone is home" or any other default-normal state, \
live claims you cannot verify (train delays, headlines you are unsure \
of), or numbers the dashboard already shows unchanged.

Reply with the board message text ONLY — no preamble, no quotes around it.\
"""


def _api(path: str):
    req = urllib.request.Request(
        f"http://supervisor/core/api/{path}",
        headers={"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def house_state() -> dict:
    out = {}
    for key, entity in STATE_ENTITIES.items():
        try:
            s = _api(f"states/{entity}")
            out[key] = s.get("state")
            if key == "weather":
                out["temp_c"] = (s.get("attributes") or {}).get("temperature")
        except Exception:
            out[key] = "unknown"
    return out


def recent_bodies() -> list[str]:
    try:
        s = _api("states/sensor.portal_toast_feed")
        return [t.get("body", "") for t in (s.get("attributes") or {}).get("toasts", [])][:5]
    except Exception:
        return []


def build_prompt(reason: str) -> str:
    state = house_state()
    recent = recent_bodies()
    return PROMPT_TEMPLATE.format(
        reason=reason,
        now=time.strftime("%A %d %B %Y, %H:%M"),
        state=json.dumps(state, indent=2),
        recent="\n".join(f"- {b}" for b in recent) or "- (nothing)",
    )


def ask_codex(prompt: str) -> str:
    cmd = [
        "codex", "-a", "never", "-s", "read-only",
        "exec", "--json", "--skip-git-repo-check",
        "-C", "/config",
        prompt,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    last = ""
    for line in out.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
            last = item["text"].strip()
        if ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text" and c.get("text"):
                    last = c["text"].strip()
    if not last:
        raise RuntimeError(f"no agent message (rc={out.returncode}): {out.stderr[-400:]}")
    return last


def fit_board(text: str) -> str:
    """Word-safe pack into ROWS x COLS (mirrors the card's _fitRows), then
    re-join — guarantees the card can render every word un-cut."""
    words = CHARSET.sub("", text.upper()).split()
    lines, line = [], ""
    for w in words:
        if len(w) > COLS:
            continue
        cand = f"{line} {w}".strip()
        if len(cand) <= COLS:
            line = cand
        elif len(lines) < ROWS - 1:
            lines.append(line)
            line = w
        else:
            break
    if line:
        lines.append(line)
    return " ".join(lines[:ROWS])


def post_toast(body: str) -> None:
    req = urllib.request.Request(
        "http://supervisor/core/api/events/portal_toast",
        data=json.dumps({
            "source_key": "codex_flap",
            "severity": "ambient",
            "category": "CODEX",
            "body": body,
            "icon": "mdi:robot-outline",
        }).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def main() -> int:
    reason = sys.argv[1] if len(sys.argv) > 1 else "manual flap-now"
    body = fit_board(ask_codex(build_prompt(reason)))
    if not body:
        print("flap: empty after sanitize, skipping", file=sys.stderr)
        return 1
    post_toast(body)
    print(f"flap: posted: {body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
