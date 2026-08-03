#!/usr/bin/env python3
"""Codex flap digest — the Portal Hub's "organic brain", agentic edition.

Wakes (interval / add-on start / manual flap-now) and hands Codex the keys:
it must QUERY Home Assistant itself (multiple curl tool calls against the
core API), decide the single most valuable message for the split-flap board
(5 rows x 17 cells = 85 chars), and reply with it. This script only
sanitizes the reply and fires the portal_toast event.

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

ROWS = 3
COLS = 17
CHARSET = re.compile(r"[^A-Z0-9 %:'\-]")

MODEL = "gpt-5.6-terra"
REASONING = "max"

# Base URL for the HA core REST API. In the add-on this is the Supervisor
# proxy; override with FLAP_HA_API + FLAP_HA_TOKEN to test from a dev box.
HA_API = os.environ.get("FLAP_HA_API", "http://supervisor/core/api")


def _token() -> str:
    return os.environ.get("FLAP_HA_TOKEN") or os.environ["SUPERVISOR_TOKEN"]


PROMPT_TEMPLATE = """\
You are the Portal Hub's organic brain — the flap-aware side of Jarvis, \
waking up inside the house's Home Assistant box for one job: investigate \
the house, then decide what the living-room split-flap board should say \
right now, and say it.

WAKE REASON: {reason}
DATE AND TIME AT THE HOUSE: {now}
HOUSE LOCATION: a family home in North-West London (Pinner/Harrow area), \
United Kingdom.

THE DISPLAY: a physical Solari split-flap board on the family Portal, \
17 boxes per row. The board is SHARED: other messages (quotes, TV and car \
events) stack on it too, and whatever does not fit the leftover rows gets \
cut off with an ellipsis. So your message must stay short enough to \
survive being pushed down: AIM FOR 40 CHARACTERS OR FEWER, never more \
than 51 (3 rows). Uppercase. Only letters, digits, spaces and % : ' - \
survive; every other character is dropped before render. No single word \
longer than 17 characters. Text wraps word-safe across rows.

YOU MUST INVESTIGATE BEFORE YOU WRITE. You have shell access. Query the \
Home Assistant REST API — several calls, not one:

  curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
    {ha_api}/states/<entity_id>          # one entity
  curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
    {ha_api}/states                      # everything (large; filter with jq/grep)
  curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
    "{ha_api}/history/period/<iso-start>?filter_entity_id=<entity_id>"

Starting points (verify live, then dig wherever looks interesting):
  input_boolean.night_mode / away_mode / tv_mode / barbecue_mode
  person.abhikmitra89uk (Abhik)  person.anushree_bagchi (Anushree)  \
person.malay_mitra (Malay)
  device_tracker.volvo_xc60_location  sensor.volvo_xc60_battery  \
sensor.volvo_xc60_charging_power  sensor.volvo_xc60_distance_to_empty_battery
  weather.forecast_home (state + attributes hold the forecast)
  sensor.portal_toast_feed (attributes.toasts = what the board recently showed)

You are also inside the Home Assistant config directory — read the YAML if \
an entity's meaning is unclear. Household: Abhik, Anushree, their toddler \
Tintin (the nursery is Tintin's room), and Malay (Abhik's father). The car \
is a Volvo XC60 EV. To-dos are already shown elsewhere on this screen.

RECENTLY SHOWN on the board (with times) — do NOT repeat any of these:
{recent}

Pick the SINGLE most valuable message, preferring in this order:
1. A warning or unusual state (charging fault, odd mode combo, something \
that looks wrong).
2. A meaningful transition: someone reached the office or got home, car \
started charging, charge complete with the %.
3. Something useful for whoever is OUT of the house right now.
4. A weather change worth acting on: rain coming, unusual temp swing — \
check the forecast attributes, not just the current state.
5. A pattern or anomaly YOU noticed while exploring (history is available) \
— surprises we did not think to automate are welcome.
6. A piece of news you genuinely know and are confident of — never invent \
or dress something up as breaking news.
7. Otherwise: a funny quip or an inspirational quote — vary the flavour \
run to run.

STYLE: say ONE thing only, as one plain simple-English sentence. Do not \
string several facts together with dashes or AND. Simple words beat clever \
compression.

NEVER: to-dos, "everyone is home" or any other default-normal state, live \
claims you cannot verify (train delays, headlines you are unsure of), or \
numbers the dashboard already shows unchanged.

Reply with the board message text ONLY — no preamble, no quotes around it.\
"""


def _api(path: str):
    req = urllib.request.Request(
        f"{HA_API}/{path}",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def recent_with_times() -> str:
    try:
        s = _api("states/sensor.portal_toast_feed")
        toasts = (s.get("attributes") or {}).get("toasts", [])[:5]
        lines = []
        for t in toasts:
            hhmm = (t.get("ts") or "")[11:16] or "??:??"
            lines.append(f"- [{hhmm}] {t.get('body', '')}")
        return "\n".join(lines) or "- (nothing)"
    except Exception:
        return "- (feed unavailable)"


def build_prompt(reason: str) -> str:
    return PROMPT_TEMPLATE.format(
        reason=reason,
        now=time.strftime("%A %d %B %Y, %H:%M %Z"),
        ha_api=HA_API,
        recent=recent_with_times(),
    )


def ask_codex(prompt: str) -> str:
    cmd = [
        "codex", "-a", "never", "-s", "danger-full-access",
        "-m", MODEL, "-c", f'model_reasoning_effort="{REASONING}"',
        "exec", "--json", "--skip-git-repo-check",
        "-C", "/config",
        prompt,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    last = ""
    tool_calls = 0
    for line in out.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if isinstance(item, dict):
            if item.get("type") == "command_execution":
                tool_calls += 1
            if item.get("type") == "agent_message" and item.get("text"):
                last = item["text"].strip()
        if ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text" and c.get("text"):
                    last = c["text"].strip()
    if not last:
        raise RuntimeError(f"no agent message (rc={out.returncode}): {out.stderr[-400:]}")
    print(f"flap: codex made {tool_calls} tool calls", file=sys.stderr)
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
        f"{HA_API}/events/portal_toast",
        data=json.dumps({
            "source_key": "codex_flap",
            "severity": "ambient",
            "category": "CODEX",
            "body": body,
            "icon": "mdi:robot-outline",
        }).encode(),
        headers={
            "Authorization": f"Bearer {_token()}",
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
