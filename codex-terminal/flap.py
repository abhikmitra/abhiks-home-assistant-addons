#!/usr/bin/env python3
"""Codex flap digest: ask Codex for one interesting thing, fit it to the
Portal split-flap board (6 rows x 17 cells),
and fire a portal_toast event via the Supervisor API.

Toast contract: Day2DayAgentHelp/Agent-Tooling/meta-portal/
portal-voice-states-and-toast-spec.md — coalesced by source_key, ambient
severity has a 45-min TTL so a periodic run naturally replaces itself.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

ROWS = 6
COLS = 17
CHARSET = re.compile(r"[^A-Z0-9 %:'\-]")

PROMPT = (
    "You write one message for a household split-flap display (like an airport "
    "departure board). Give me ONE interesting thing right now: a notable news "
    "headline you know of, a striking quote, or a surprising fact. Vary the type "
    "between runs. Constraints: plain text, max 100 characters total, only "
    "letters, digits, spaces, percent, colon, apostrophe, hyphen. No word longer "
    "than 17 characters. No preamble, no quotes around it — reply with the "
    "message text ONLY."
)


def ask_codex() -> str:
    cmd = [
        "codex", "-a", "never", "-s", "read-only",
        "exec", "--json", "--skip-git-repo-check",
        "-C", os.environ.get("HOME", "/data/home"),
        PROMPT,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
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
    body = fit_board(ask_codex())
    if not body:
        print("flap: empty after sanitize, skipping", file=sys.stderr)
        return 1
    post_toast(body)
    print(f"flap: posted: {body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
