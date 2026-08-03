#!/usr/bin/env python3
"""
portal_chatgpt_bridge.py — Second-assistant lane for Meta Portal -> ChatGPT web.

This daemon runs on the Mac (not on the Portal) and drives the Portal's Chrome
browser via ADB + Chrome DevTools Protocol (CDP). It exposes a tiny HTTP API
that Home Assistant (and local callers) can use to open a ChatGPT voice session
on the Portal, then reliably tear it down.

Lanes:
- Existing lanes (Jarvis/VACA/Codex) continue on VACA app (com.msp1974.vacompanion).
- This lane uses org.chromium.chrome to show https://chatgpt.com/ in fullscreen kiosk.

Exit-chip / console-signal design:
- When the lane starts we inject a floating pill button (#portal-exit-chip) into the
  live chatgpt.com DOM and also via Page.addScriptToEvaluateOnNewDocument so it
  survives SPA navigations.
- The chip is fixed bottom-left, high z-index (2147483647), 56px+ tall, 18px font,
  dark pill with white X + "Exit to Portal". It logs PORTAL_EXIT to console on tap.
- A self-healing interval (2s) re-adds the chip if React re-renders remove it.
- A persistent CDP websocket subscribed to Runtime.consoleAPICalled watches for
  that log. When seen it runs the same logic as /api/stop (idempotent):
  exit fullscreen, force-stop Chrome, relaunch VACA, clear input_boolean, mark
  lane inactive.
- Because the exit signal travels as a CDP console event, it works without extra
  Portal APKs and without trusting DOM mutation observers from the Mac side.

Idle watchdog:
- While lane active, every 10s check:
  * foreground activity still org.chromium.chrome
  * no lane activity for IDLE_TIMEOUT_SEC (default 600s)
  If either fails, auto-stop.

Voice button:
- Best-effort, non-fatal. Tries selectors in order and clicks via Runtime.evaluate
  with userGesture=true. Missing selectors is expected when logged out.

Config via env (defaults in parentheses):
  HOST (0.0.0.0), PORT (8767), ALLOWED_CLIENTS (127.0.0.1,192.168.4.62),
  ADB (/opt/homebrew/bin/adb), PORTAL_SERIAL (192.168.4.71:5555),
  CDP_PORT (9333), CHATGPT_URL (https://chatgpt.com/),
  IDLE_TIMEOUT_SEC (600), HA_URL, HASS_TOKEN/HASS_SERVER, VERSION.

Deps: stdlib + websocket-client (python3 -m pip install websocket-client).
"""
from __future__ import annotations
import base64
import datetime
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8767"))
ALLOWED_CLIENTS_RAW = os.environ.get("ALLOWED_CLIENTS", "127.0.0.1,192.168.4.62")
ADB = os.environ.get("ADB", "/opt/homebrew/bin/adb")
PORTAL_SERIAL = os.environ.get("PORTAL_SERIAL", "192.168.4.71:5555")
CDP_PORT = int(os.environ.get("CDP_PORT", "9333"))
CHATGPT_URL = os.environ.get("CHATGPT_URL", "https://chatgpt.com/")
IDLE_TIMEOUT_SEC = int(os.environ.get("IDLE_TIMEOUT_SEC", "600"))
# Silence, in seconds, that ends a voice conversation. Measured on real audio
# (see portal_chatgpt_kiosk.js), so ChatGPT thinking for 20s is not "idle".
VOICE_IDLE_SEC = int(os.environ.get("VOICE_IDLE_SEC", "60"))
# A freshly opened lane is never idle: opening takes ~35s and the user has not
# had a chance to speak yet.
LANE_GRACE_SEC = int(os.environ.get("LANE_GRACE_SEC", "45"))
HA_URL = os.environ.get("HA_URL", os.environ.get("HASS_SERVER", "http://192.168.4.62:8123"))
def _load_hass_token() -> str:
    """The LaunchAgent plist is committed to the repo, so the token must never
    live in it. Fall back to the export in ~/.zshrc, which is where every other
    tool on this Mac already reads it from."""
    tok = os.environ.get("HASS_TOKEN", "")
    if tok:
        return tok
    try:
        with open(os.path.expanduser("~/.zshrc"), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export HASS_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""

HASS_TOKEN = _load_hass_token()
VERSION = os.environ.get("VERSION", "1.0.0-chatgpt-bridge")

ALLOWED_CLIENTS = [s.strip() for s in ALLOWED_CLIENTS_RAW.split(",") if s.strip()]

START_TIME = time.time()

# ---------------------------------------------------------------------------
# Lane state (thread-safe)
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_lane: Dict[str, Any] = {
    "active": False,
    "chatgpt_tab_url": None,
    "chatgpt_tab_id": None,
    "ws_debugger_url": None,
    "last_activity_epoch": None,
    "start_epoch": None,
}
_events: List[Dict[str, Any]] = []
_events_lock = threading.Lock()
_MAX_EVENTS = 100

_exit_watcher_thread: Optional[threading.Thread] = None
_exit_watcher_stop = threading.Event()
_idle_watcher_thread: Optional[threading.Thread] = None
_idle_watcher_stop = threading.Event()
_cdp_ws_lock = threading.Lock()
_cdp_ws = None  # kept for stop idempotency

# Serializes every start/stop against every other start/stop. Without this a
# tap on the exit chip during the ~9s opening window raced the in-flight
# /api/start: the stop ran while `active` was still False (it only flips True
# at the END of handle_start), so the ha_ws off-handler's `elif new=="off"
# and active` guard silently dropped it, and the start's own
# ha_input_boolean_on at the end flipped the boolean back ON — a lane the
# user explicitly closed reopened itself. Two rapid HA-boolean flips (double
# tap, or voice trigger racing the dock pill) hit the same hole from the
# other side: two concurrent handle_start calls drove the SAME tab with two
# CDP sessions, and whichever set _lane's ws_debugger_url last silently
# orphaned the other's watcher. Every stop now blocks until any in-flight
# start has fully finished (start always terminates well under 60s, so this
# never wedges), then proceeds — "end" always wins, it just may be a few
# seconds late instead of lost.
_op_lock = threading.Lock()

def _now_iso_local() -> str:
    return datetime.datetime.now().astimezone().isoformat()

def _log_event(kind: str, detail: Any = None):
    rec = {"ts": _now_iso_local(), "kind": kind, "detail": detail}
    with _events_lock:
        _events.append(rec)
        if len(_events) > _MAX_EVENTS:
            _events[:] = _events[-_MAX_EVENTS:]
    print(f"[{rec['ts']}] {kind}: {detail}", flush=True)

def _lane_snapshot() -> Dict[str, Any]:
    with _state_lock:
        return dict(_lane)

def _set_lane(**kwargs):
    with _state_lock:
        _lane.update(kwargs)

# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------
def adb_cmd(args: List[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run ADB with PORTAL_SERIAL scoping if needed. Timeout always."""
    full = [ADB]
    # if args start with -s we trust caller; else inject -s PORTAL_SERIAL for shell/monkey etc
    if args and args[0] in ("-s", "connect", "devices", "forward"):
        full += args
    else:
        # most commands should be scoped to Portal serial
        # but 'connect' and forward list handling differs
        full += ["-s", PORTAL_SERIAL] + args
    return subprocess.run(full, capture_output=True, text=True, timeout=timeout)

def adb_check_connected() -> tuple[bool, str]:
    try:
        cp = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10)
        out = cp.stdout
        # device present if line contains PORTAL_SERIAL or its bare serial 818PGF02P123XH05 with "device"
        ok = (PORTAL_SERIAL in out or "818PGF02P123XH05" in out) and "device" in out
        # also check direct
        if not ok:
            # try -s check
            cp2 = subprocess.run([ADB, "-s", PORTAL_SERIAL, "get-state"], capture_output=True, text=True, timeout=10)
            ok = cp2.stdout.strip() == "device"
            out = out + "\n" + cp2.stdout + cp2.stderr
        return ok, out.strip()
    except Exception as e:
        return False, f"adb devices error: {e}"

def get_foreground_activity() -> tuple[Optional[str], str]:
    """Return (activity/package, raw) from dumpsys window."""
    try:
        cp = adb_cmd(["shell", "dumpsys", "window"], timeout=15)
        out = cp.stdout + cp.stderr
        activity = None
        for line in out.splitlines():
            if "mCurrentFocus=" in line or "mFocusedApp=" in line:
                # example: Window{... com.msp1974.vacompanion/com.msp1974.vacompanion.MainActivity}
                # extract last token containing /
                # heuristic: find com. substring
                # try to pull package/activity
                # Use simple parsing
                if "com.msp1974.vacompanion" in line:
                    # try exact
                    import re
                    m = re.search(r"(com\.[^\s/]+/[^\s\}]+)", line)
                    if m:
                        activity = m.group(1)
                        break
                if "org.chromium.chrome" in line:
                    import re
                    m = re.search(r"(org\.chromium\.chrome/[^\s\}]+)", line)
                    if m:
                        activity = m.group(1)
                        break
        # Fallback: get most precise CurrentFocus
        if activity is None:
            import re
            for line in out.splitlines():
                if "mCurrentFocus=" in line:
                    m = re.search(r"Window\{.*?\s+([A-Za-z0-9\._]+/[A-Za-z0-9\._]+)", line)
                    if m:
                        activity = m.group(1)
                        break
        return activity, out[:2000]
    except Exception as e:
        return None, f"error: {e}"

def get_foreground_package() -> Optional[str]:
    act, _ = get_foreground_activity()
    if act and "/" in act:
        return act.split("/")[0]
    return act

def is_portal_awake() -> tuple[bool, bool, str]:
    """Returns (awake, is_dreaming, detail). Awake derived from dumpsys power + window."""
    try:
        cp = adb_cmd(["shell", "dumpsys", "power"], timeout=10)
        out_pow = cp.stdout
        awake = "mWakefulness=Awake" in out_pow or "mWakefulness=Dreaming" not in out_pow or "mWakefulness=Awake" in out_pow
        # more precise:
        dreaming = "mWakefulness=Dreaming" in out_pow
        # window manager extra
        cp2 = adb_cmd(["shell", "dumpsys", "window"], timeout=10)
        out_win = cp2.stdout
        # if dreaming flag present elsewhere
        if "mShowingDream=true" in out_win or "mDreamingLockscreen=true" in out_win:
            dreaming = True
        # awake if screen on:
        # Use power's mWakefulness
        import re
        m = re.search(r"mWakefulness=([A-Za-z]+)", out_pow)
        state = m.group(1) if m else "unknown"
        is_awake = state in ("Awake", "Dreaming")  # Dreaming still has screen partially; we treat as needs wake
        # For our purpose portal_awake = screen on
        detail = f"wakefulness={state}"
        return (state != "Asleep", dreaming, detail + " | " + out_pow[:500])
    except Exception as e:
        return False, False, f"error {e}"

# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------
def cdp_forward() -> tuple[bool, str]:
    try:
        # MUST be scoped with -s: an unscoped `adb forward` is ambiguous (and
        # fails with "adb: more than one device/emulator") the moment a second
        # adb device is attached to this Mac — e.g. a phone plugged in over
        # USB alongside the Portal's network adb connection. Reproduced live
        # 2026-08-03: start_step:cdp_forward failed this way while a Pixel was
        # plugged in, and the lane was left stuck "on" with no Chrome and no
        # orb (see the fail-closed cleanup added to _handle_start_locked).
        cp = subprocess.run([ADB, "-s", PORTAL_SERIAL, "forward", f"tcp:{CDP_PORT}", "localabstract:chrome_devtools_remote"],
                            capture_output=True, text=True, timeout=10)
        return cp.returncode == 0, (cp.stdout + cp.stderr).strip() or "forward ok"
    except Exception as e:
        return False, str(e)

def cdp_list_tabs() -> tuple[bool, Any]:
    try:
        url = f"http://127.0.0.1:{CDP_PORT}/json/list"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return True, data
    except Exception as e:
        return False, str(e)

def cdp_find_chatgpt_tab(tabs) -> Optional[Dict[str, Any]]:
    if not isinstance(tabs, list):
        return None
    # Prefer chatgpt.com page tabs
    for t in tabs:
        u = t.get("url", "") or ""
        if "chatgpt.com" in u and t.get("type") == "page":
            return t
    return None

def cdp_get_chrome_version() -> Optional[str]:
    try:
        url = f"http://127.0.0.1:{CDP_PORT}/json/version"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return data.get("Browser") or data.get("User-Agent") or None
    except:
        return None

# Minimal CDP session wrapper over websocket-client
class CDPSession:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._ws = None
        self._id = 1
        self._lock = threading.Lock()
        self._pending: Dict[int, Any] = {}
        self._event_callbacks: List[Any] = []
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def connect(self, timeout=10):
        import websocket
        # suppress_origin True proven to bypass 403
        self._ws = websocket.create_connection(self.ws_url, suppress_origin=True, timeout=timeout)
        self._stop.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self):
        while not self._stop.is_set():
            try:
                msg = self._ws.recv()
                if not msg:
                    continue
                data = json.loads(msg)
                # dispatch
                if "id" in data:
                    with self._lock:
                        self._pending[data["id"]] = data
                else:
                    # event
                    for cb in list(self._event_callbacks):
                        try:
                            cb(data)
                        except:
                            pass
            except Exception as e:
                if self._stop.is_set():
                    break
                time.sleep(0.2)

    def on_event(self, cb):
        self._event_callbacks.append(cb)

    def send(self, method: str, params: Dict[str, Any] = None, timeout=10) -> Dict[str, Any]:
        with self._lock:
            cur_id = self._id
            self._id += 1
        payload = {"id": cur_id, "method": method}
        if params:
            payload["params"] = params
        raw = json.dumps(payload)
        with self._lock:
            # ensure no stale
            self._pending.pop(cur_id, None)
        self._ws.send(raw)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if cur_id in self._pending:
                    return self._pending.pop(cur_id)
            time.sleep(0.05)
        raise TimeoutError(f"CDP timeout for {method} id={cur_id}")

    def evaluate(self, expression: str, user_gesture: bool = False, await_promise: bool = False, timeout: int = 10) -> Dict[str, Any]:
        params = {"expression": expression}
        if user_gesture:
            params["userGesture"] = True
        if await_promise:
            params["awaitPromise"] = True
        return self.send("Runtime.evaluate", params, timeout=timeout)

    def close(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except:
            pass

# ---------------------------------------------------------------------------
# Exit chip JS
# ---------------------------------------------------------------------------
EXIT_CHIP_JS = r"""
(function(){
  const ID='portal-exit-chip';
  function ensureChip(){
    if(document.getElementById(ID)) return true;
    try{
      const btn=document.createElement('button');
      btn.id=ID;
      btn.setAttribute('aria-label','Exit to Portal');
      btn.textContent='✕ Exit to Portal';
      // Inline styles for reliability (ignore page CSS)
      Object.assign(btn.style,{
        position:'fixed',
        left:'16px',
        bottom:'16px',
        zIndex:'2147483647',
        display:'flex',
        alignItems:'center',
        justifyContent:'center',
        minHeight:'56px',
        minWidth:'180px',
        padding:'12px 20px',
        fontSize:'18px',
        fontWeight:'700',
        fontFamily:'-apple-system,system-ui,sans-serif',
        lineHeight:'1.2',
        color:'#ffffff',
        background:'#1a1a1a',
        border:'2px solid #ffffff',
        borderRadius:'28px',
        boxShadow:'0 4px 16px rgba(0,0,0,0.6)',
        cursor:'pointer',
        pointerEvents:'auto',
        userSelect:'none',
        WebkitUserSelect:'none',
        touchAction:'manipulation',
      });
      btn.addEventListener('click', function(e){
        e.preventDefault(); e.stopPropagation();
        console.log('PORTAL_EXIT');
      }, {capture:true});
      btn.addEventListener('touchend', function(e){
        e.preventDefault(); e.stopPropagation();
        console.log('PORTAL_EXIT');
      }, {capture:true, passive:false});
      (document.body||document.documentElement).appendChild(btn);
      return true;
    }catch(err){ console.warn('exit-chip inject failed', err); return false; }
  }
  ensureChip();
  // Self-heal every 2s
  if(!window.__portalExitChipInterval){
    window.__portalExitChipInterval = setInterval(ensureChip, 2000);
  }
  return 'chip-ok-'+(document.getElementById(ID)?'present':'missing');
})();
"""

# Script for Page.addScriptToEvaluateOnNewDocument — must be self-contained string
EXIT_CHIP_NEW_DOC_JS = r"""
(function(){
  const ID='portal-exit-chip';
  function ensureChip(){
    if(!document.body) return false;
    if(document.getElementById(ID)) return true;
    try{
      const btn=document.createElement('button');
      btn.id=ID;
      btn.setAttribute('aria-label','Exit to Portal');
      btn.textContent='✕ Exit to Portal';
      Object.assign(btn.style,{
        position:'fixed',left:'16px',bottom:'16px',zIndex:'2147483647',
        display:'flex',alignItems:'center',justifyContent:'center',
        minHeight:'56px',minWidth:'180px',padding:'12px 20px',
        fontSize:'18px',fontWeight:'700',fontFamily:'-apple-system,system-ui,sans-serif',
        lineHeight:'1.2',color:'#ffffff',background:'#1a1a1a',
        border:'2px solid #ffffff',borderRadius:'28px',
        boxShadow:'0 4px 16px rgba(0,0,0,0.6)',cursor:'pointer',
        pointerEvents:'auto',userSelect:'none',touchAction:'manipulation'
      });
      btn.addEventListener('click', function(e){ e.preventDefault(); e.stopPropagation(); console.log('PORTAL_EXIT'); }, {capture:true});
      btn.addEventListener('touchend', function(e){ e.preventDefault(); e.stopPropagation(); console.log('PORTAL_EXIT'); }, {capture:true, passive:false});
      document.body.appendChild(btn);
      return true;
    }catch(_){return false;}
  }
  function boot(){
    ensureChip();
    if(!window.__portalExitChipInterval){
      window.__portalExitChipInterval=setInterval(ensureChip,2000);
    }
  }
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
  // also attempt on body observer
  const mo = new MutationObserver(()=>{ ensureChip(); });
  try{ mo.observe(document.documentElement,{childList:true,subtree:true}); }catch(_){}
})();
"""

# Voice MODE only — deliberately not "Start dictation", which is speech-to-text
# into the composer and leaves you tapping Send. This lane wants ChatGPT's
# conversational voice pipeline, which is the whole reason it exists.
VOICE_SELECTORS = [
    '[data-testid="composer-speech-button"]',
    '[data-testid="voice-mode-button"]',
    'button[aria-label*="voice mode" i]',
    'button[aria-label*="Start voice" i]',
    'button[aria-label*="voice" i]',
]

# ---------------------------------------------------------------------------
# HA helper
# ---------------------------------------------------------------------------
def ha_set_input_boolean(entity_id: str, on: bool) -> tuple[bool, str]:
    if not HA_URL or not HASS_TOKEN:
        return False, "HA_URL or HASS_TOKEN missing, skipping"
    service = "turn_on" if on else "turn_off"
    url = f"{HA_URL.rstrip('/')}/api/services/input_boolean/{service}"
    body = json.dumps({"entity_id": entity_id}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {HASS_TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            txt = resp.read().decode()[:500]
            return resp.status in (200, 201), txt
    except urllib.error.HTTPError as e:
        b = e.read().decode()[:800] if hasattr(e, 'read') else str(e)
        # helper may not exist — treat as non-fatal
        if e.code == 404 or "not found" in b.lower():
            return False, f"helper not found (404) — ok to ignore: {b[:200]}"
        return False, f"HTTP {e.code}: {b[:400]}"
    except Exception as ex:
        return False, f"HA call error: {ex}"

VACA_MUTE_SWITCH = "switch.vaca_54e9e343e_mute"

def ha_set_switch(entity_id: str, on: bool) -> tuple[bool, str]:
    """Same shape as ha_set_input_boolean but for the switch domain."""
    if not HA_URL or not HASS_TOKEN:
        return False, "HA_URL or HASS_TOKEN missing, skipping"
    service = "turn_on" if on else "turn_off"
    url = f"{HA_URL.rstrip('/')}/api/services/switch/{service}"
    body = json.dumps({"entity_id": entity_id}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {HASS_TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status in (200, 201), resp.read().decode()[:300]
    except Exception as ex:
        return False, f"HA call error: {ex}"

# ---------------------------------------------------------------------------
# Core lane logic (start / stop)
# ---------------------------------------------------------------------------
def do_adb_connect() -> tuple[bool, str]:
    try:
        # Connect is idempotent
        cp = subprocess.run([ADB, "connect", PORTAL_SERIAL],
                            capture_output=True, text=True, timeout=15)
        # then check device
        ok, detail = adb_check_connected()
        return ok, f"connect:{(cp.stdout+cp.stderr)[:300]} | devices:{detail[:300]}"
    except Exception as e:
        return False, str(e)

def do_wakeup_and_dismiss() -> tuple[bool, str]:
    try:
        awake, dreaming, detail = is_portal_awake()
        log = [detail]
        # Always send wakeup keycode (non-harmful)
        try:
            p = adb_cmd(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], timeout=5)
            log.append(f"wakeup:{(p.stdout+p.stderr)[:200]}")
        except Exception as ex:
            log.append(f"wakeup-err:{ex}")
        if dreaming:
            try:
                # Tap at 640 400 to dismiss screensaver/dream
                p2 = adb_cmd(["shell", "input", "tap", "640", "400"], timeout=5)
                log.append(f"tap dismiss:{(p2.stdout+p2.stderr)[:200]}")
                time.sleep(0.8)
            except Exception as ex:
                log.append(f"tap-err:{ex}")
        # re-check
        awake2, dreaming2, detail2 = is_portal_awake()
        log.append(f"after:{detail2}")
        return True, " | ".join(log)
    except Exception as e:
        return False, f"{e}\n{traceback.format_exc()[:500]}"

def do_forward() -> tuple[bool, str]:
    ok, detail = cdp_forward()
    return ok, detail

def prune_chatgpt_tabs() -> str:
    """Keep exactly one chatgpt.com tab and close everything else.

    Chrome restores its tab set after the force-stop that ends a lane, so
    without this every lane leaves another chatgpt.com tab behind. Duplicates
    are not cosmetic: the bridge can attach to a stale tab and drive a page the
    user is not looking at, which is how the 13:07 run ended up with the kiosk
    installed on the wrong tab.
    """
    ok, tabs = cdp_list_tabs()
    if not ok or not isinstance(tabs, list):
        return "tab-list-unavailable"
    pages = [t for t in tabs if t.get("type") == "page"]
    gpt = [t for t in pages if "chatgpt.com" in (t.get("url") or "")]
    keep = gpt[0] if gpt else None
    closed = 0
    for t in pages:
        if keep and t.get("id") == keep.get("id"):
            continue
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json/close/{t['id']}", timeout=4).read()
            closed += 1
        except Exception:
            pass
    if keep:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json/activate/{keep['id']}", timeout=4).read()
        except Exception:
            pass
    return f"kept={(keep or {}).get('url','none')[:40]} closed={closed}"

def do_launch_chrome() -> tuple[bool, str]:
    """Bring Chrome forward on a ChatGPT tab, reusing one if it already exists."""
    try:
        do_forward()
        ok, tabs = cdp_list_tabs()
        has_tab = ok and isinstance(tabs, list) and any(
            t.get("type") == "page" and "chatgpt.com" in (t.get("url") or "") for t in tabs)
        if has_tab:
            pruned = prune_chatgpt_tabs()
            # REORDER_TO_FRONT so an existing Chrome task is raised rather than
            # re-created; a re-created activity reloads the page.
            cp = adb_cmd(["shell", "am", "start", "-f", "0x10020000", "-n",
                          "org.chromium.chrome/com.google.android.apps.chrome.Main"], timeout=15)
            return cp.returncode == 0, f"reused existing tab ({pruned})"
        cp = adb_cmd(["shell", "am", "start", "-a", "android.intent.action.VIEW",
                      "-d", CHATGPT_URL,
                      "-n", "org.chromium.chrome/com.google.android.apps.chrome.Main"], timeout=15)
        return cp.returncode == 0, "opened new tab: " + (cp.stdout + cp.stderr)[:300]
    except Exception as e:
        return False, str(e)

def do_poll_chatgpt_tab(timeout_sec=20) -> tuple[bool, Any]:
    """Poll at 250ms. Every 1s of granularity here is 1s the household spends
    looking at a spinner, and the tab usually appears almost immediately when
    Chrome was left warm."""
    deadline = time.time() + timeout_sec
    last = None
    while time.time() < deadline:
        ok, data = cdp_list_tabs()
        if ok:
            tab = cdp_find_chatgpt_tab(data)
            if tab:
                return True, tab
            last = f"no chatgpt tab in {len(data)} tabs: {[t.get('url','')[:80] for t in data[:5]]}"
        else:
            last = data
        time.sleep(0.25)
    return False, last or "timeout"

KIOSK_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "portal_chatgpt_kiosk.js")

def load_kiosk_js() -> str:
    """Read from disk every time so editing the kiosk layer needs no restart."""
    with open(KIOSK_JS_PATH, "r", encoding="utf-8") as fh:
        return fh.read()

def js_value(res: Dict[str, Any]) -> Any:
    return res.get("result", {}).get("result", {}).get("value")

def do_inject_chip(session: CDPSession) -> tuple[bool, str]:
    """Install the in-page kiosk layer (orb-only view, overlay, audio taps, chip)."""
    try:
        for dom in ("Runtime", "Page"):
            try:
                session.send(f"{dom}.enable", {}, timeout=5)
            except Exception:
                pass
        src = load_kiosk_js()
        # Registered on THIS session, which is the long-lived watcher session —
        # a registration made by a session that later closes is silently lost.
        try:
            session.send("Page.addScriptToEvaluateOnNewDocument",
                         {"source": src}, timeout=5)
        except Exception as e:
            _log_event("kiosk_newdoc_warn", str(e)[:300])
        res = session.evaluate(src, user_gesture=True, timeout=10)
        if res.get("result", {}).get("exceptionDetails"):
            return False, f"exception:{res['result']['exceptionDetails']}"
        return True, str(js_value(res))[:300]
    except Exception as e:
        return False, f"{e}\n{traceback.format_exc()[:400]}"

def kiosk_call(session: CDPSession, expr: str, gesture: bool = True, timeout: int = 8) -> Any:
    """Run an expression against window.__portalKiosk, reinstalling it if the
    page navigated out from under us."""
    try:
        res = session.evaluate(
            f"(window.__portalKiosk ? ({expr}) : 'kiosk-missing')",
            user_gesture=gesture, timeout=timeout)
        val = js_value(res)
        if val == "kiosk-missing":
            session.evaluate(load_kiosk_js(), user_gesture=True, timeout=10)
            res = session.evaluate(f"({expr})", user_gesture=gesture, timeout=timeout)
            val = js_value(res)
        return val
    except Exception as e:
        return f"err:{e}"

def grant_mic_permission() -> str:
    """Grant chatgpt.com the microphone at the browser level, every open.

    Without this the lane depends on a site permission a stray tap can revoke:
    once "Never allow" is set, voice mode opens, the mic button does nothing,
    and getUserMedia answers NotAllowedError — which looks exactly like a
    broken call. Granting over CDP is deterministic and also removes the
    permission prompt from the startup path entirely.
    """
    ws = None
    try:
        ver = json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=5))
        from websocket import create_connection
        ws = create_connection(ver["webSocketDebuggerUrl"], timeout=8, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Browser.grantPermissions",
                            "params": {"origin": CHATGPT_URL.rstrip("/"),
                                       "permissions": ["audioCapture"]}}))
        resp = json.loads(ws.recv())
        return "granted" if "result" in resp else json.dumps(resp)[:120]
    except Exception as e:
        return f"err:{e}"[:120]
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass

# Chrome's page-info panel, in screen coordinates on the Portal's 1280x800
# panel with Chrome NOT fullscreen. Coordinates rather than an accessibility
# lookup because uiautomator cannot see Chrome's window on this device at all
# ("null root node returned by UiTestAutomationBridge"), so there is no tree to
# query. Brittleness is contained by verifying the outcome with getUserMedia
# rather than trusting the taps.
PAGE_INFO_BUTTON = (167, 68)
PAGE_INFO_PERMISSIONS_ROW = (640, 455)
PAGE_INFO_MIC_TOGGLE = (878, 433)
PAGE_INFO_CLOSE = (903, 274)

def mic_permission_ok(session: CDPSession) -> Optional[bool]:
    """Ask the page itself whether it can actually open the microphone."""
    try:
        val = js_value(session.evaluate(
            "navigator.mediaDevices.getUserMedia({audio:true})"
            ".then(function(s){var n=s.getAudioTracks().length;"
            "s.getTracks().forEach(function(t){t.stop()});return 'ok';})"
            ".catch(function(e){return 'fail:'+e.name;})",
            await_promise=True, user_gesture=True, timeout=12))
        if val == "ok":
            return True
        if isinstance(val, str) and val.startswith("fail:"):
            _log_event("mic_permission_check", val)
            return False
        return None
    except Exception as e:
        _log_event("mic_permission_check_error", str(e)[:150])
        return None

def repair_mic_permission(session: CDPSession) -> str:
    """Turn chatgpt.com's microphone back on through Chrome's page-info UI.

    Chrome embargoes a site's microphone after the permission prompt is
    dismissed enough times ("Automatically blocked"), which is easy to trigger
    when the lane restarts the browser a lot. Once embargoed the lane looks
    healthy but never hears anything, and Browser.grantPermissions over CDP
    does NOT lift it. There is no root and Chrome is not debuggable, so its
    Preferences file is unreachable — driving the UI is the only route.
    """
    try:
        session.evaluate("window.__portalKiosk&&window.__portalKiosk.release()", timeout=5)
        session.evaluate("try{document.exitFullscreen()}catch(e){}", timeout=5)
    except Exception:
        pass
    time.sleep(1.5)
    for x, y in (PAGE_INFO_BUTTON, PAGE_INFO_PERMISSIONS_ROW,
                 PAGE_INFO_MIC_TOGGLE, PAGE_INFO_CLOSE):
        adb_cmd(["shell", "input", "tap", str(x), str(y)], timeout=8)
        time.sleep(1.2)
    ok = mic_permission_ok(session)
    _log_event("mic_permission_repair", f"granted={ok}")
    return "repaired" if ok else "repair-failed"

def ensure_mic_permission(session: CDPSession, add_step) -> bool:
    """Check, and self-heal once if the microphone is blocked."""
    state = mic_permission_ok(session)
    if state is True:
        add_step("mic_permission", True, "granted")
        return True
    if state is None:
        add_step("mic_permission", True, "unknown (page busy) — continuing")
        return True
    result = repair_mic_permission(session)
    add_step("mic_permission", result == "repaired",
             "was blocked by Chrome; " + result)
    return result == "repaired"

def ensure_chrome_foreground(timeout: float = 6.0) -> bool:
    """Bring Chrome to the front and wait until it really is there.

    REORDER_TO_FRONT so an existing task is raised rather than re-created — a
    re-created activity reloads the page and kills a live call.
    """
    try:
        if get_foreground_package() == "org.chromium.chrome":
            return True
        adb_cmd(["shell", "am", "start", "-f", "0x10020000", "-n",
                 "org.chromium.chrome/com.google.android.apps.chrome.Main"], timeout=10)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.4)
            if get_foreground_package() == "org.chromium.chrome":
                _log_event("chrome_refronted_during_open", None)
                return True
        return False
    except Exception as e:
        _log_event("ensure_fg_error", str(e)[:150])
        return False

def tap_label(session: CDPSession, label_regex: str) -> str:
    """Tap a ChatGPT control with a REAL touch event via ADB.

    A synthetic .click() is enough for most of this UI, but not for the
    microphone: re-acquiring getUserMedia needs a genuine user gesture, and a
    CDP click leaves the button stuck on "Turn on microphone" forever with no
    capture session ever opening. Reading the element's rect and tapping those
    coordinates is indistinguishable from a finger.
    """
    try:
        rect = js_value(session.evaluate(
            "(function(){var b=document.querySelectorAll('button');"
            f"var re={label_regex};"
            "for(var i=0;i<b.length;i++){var a=b[i].getAttribute('aria-label')||'';"
            "if(re.test(a)){var r=b[i].getBoundingClientRect();"
            "if(r.width<2||r.height<2) return null;"
            "return JSON.stringify([Math.round(r.x+r.width/2),Math.round(r.y+r.height/2)]);}}"
            "return null;})()", timeout=6))
        if not rect:
            return "not-found"
        x, y = json.loads(rect)
        adb_cmd(["shell", "input", "tap", str(x), str(y)], timeout=8)
        return f"tapped@{x},{y}"
    except Exception as e:
        return f"err:{e}"[:80]

def enter_voice_sequence(session: CDPSession, add_step) -> Dict[str, Any]:
    """Drive chatgpt.com from a signed-in home screen to a live, orb-only voice
    call. Each phase narrates itself on the loading overlay, because on a wall
    display a 15-second unexplained pause reads as a crash."""
    # 0. Wait for the page to be READY, not for a fixed number of seconds.
    #    Dropping the old sleep(2) made the bridge attach to a tab that had not
    #    hydrated yet: the Start Voice control did not exist, the click missed,
    #    and the lane came up dead. Polling for the control itself is both
    #    correct and fast — it returns the instant chatgpt.com is usable.
    # Chrome MUST own the screen for the whole open. Backgrounded on Android 9
    # it loses the microphone and its timers are throttled, so the WebRTC call
    # silently never connects and the sequence times out at 30s with a dead
    # orb. VACA's relaunch from a previous teardown is what usually steals the
    # front, and it lands late enough to hit us mid-open.
    ensure_chrome_foreground()

    # A blocked microphone presents as a call that never connects, so check it
    # before spending 30s failing to start one.
    ensure_mic_permission(session, add_step)
    ensure_chrome_foreground()
    try:
        do_fullscreen(session)
    except Exception:
        pass

    # 2026-08-03: ChatGPT shipped a new web UI (Chat/Work homepage) that has NO
    # "Start voice" button anywhere — voice mode is now entered by navigating
    # to chatgpt.com/voice, and a live call is signalled by the "Voice mode
    # selector" control instead of "End voice". Both UIs must keep working
    # (the rollout may flip back), so every check below accepts either signal.
    ready = False
    deadline = time.time() + 30
    while time.time() < deadline:
        state = kiosk_call(
            session,
            "(function(){var k=window.__portalKiosk;"
            "if(document.readyState!=='complete') return 'loading';"
            "if(k.byLabel(/end voice/i)||k.byLabel(/voice mode selector/i)) return 'live';"
            "if(k.byLabel(/start voice/i)) return 'ready';"
            "if(k.byLabel(/start dictation/i)) return 'ready-new-ui';"
            "return 'hydrating';})()", gesture=False)
        if state in ("ready", "ready-new-ui", "live"):
            ready = True
            break
        time.sleep(0.25)
    add_step("page_ready", ready,
             f"voice entry present ({state})" if ready else "page never hydrated")

    # 1. Voice mode. Establishing the WebRTC call is the single slowest step,
    #    so it is kicked off before anything cosmetic and left to negotiate
    #    while we set up the overlay and the kiosk. "Start dictation" is a
    #    different button (speech-to-text into the composer) and is
    #    deliberately never used.
    clicked = kiosk_call(
        session,
        "(function(){var k=window.__portalKiosk;"
        "k.overlay('Starting voice','Opening the live conversation');"
        "if(k.byLabel(/end voice/i)||k.byLabel(/voice mode selector/i)) return 'already-live';"
        "var s=k.byLabel(/start voice/i);"
        "if(s){s.click(); return 'clicked';}"
        # New UI: no button exists; the /voice URL is the entry point.
        "location.assign('https://chatgpt.com/voice'); return 'nav-voice-url';})()")
    add_step("voice_start", clicked in ("clicked", "already-live", "nav-voice-url"), str(clicked))
    if clicked == "nav-voice-url":
        # The navigation reloads the SPA; give it a beat before polling, and
        # the poll loop below tolerates evaluate errors while it settles.
        time.sleep(4.0)

    # 2. Unmute AND wait for the call, in one loop.
    #
    #    Order matters and cost an hour to find: voice mode can open with the
    #    microphone MUTED, and the call only connects once it is on. Waiting for
    #    "End Voice" before unmuting therefore waits forever — the control never
    #    appears, the sequence times out at 30s and the lane comes up dead. So
    #    every pass clicks "Turn on microphone" if it is offered, then checks
    #    whether the call has come up.
    live = False
    mic_clicks = 0
    start = time.time()
    deadline = start + 30
    last_tap = 0.0
    while time.time() < deadline:
        try:
            state = kiosk_call(
                session,
                "(function(){var k=window.__portalKiosk;"
                "if(k.byLabel(/end voice/i)||k.byLabel(/voice mode selector/i)) return 'live';"
                "if(k.byLabel(/turn on microphone/i)) return 'muted';"
                "return 'waiting';})()", gesture=False)
        except Exception:
            # A mid-open navigation (the /voice entry, or ChatGPT's own / ->
            # /c/<id> hop) can briefly kill Runtime.evaluate; keep polling.
            time.sleep(0.5)
            continue
        if state == "live":
            live = True
            break
        if state == "muted" and time.time() - last_tap > 2.0:
            # Real touch, not a synthetic click — see tap_label().
            tap_label(session, "/turn on microphone/i")
            mic_clicks += 1
            last_tap = time.time()
        # Re-assert the front every ~6s: a call that is not connecting is very
        # often a Chrome that quietly lost the screen.
        if int(time.time() - start) % 6 == 0:
            ensure_chrome_foreground()
        time.sleep(0.25)
    add_step("voice_live", live,
             f"End Voice present after {mic_clicks} mic unmute(s)" if live
             else f"call never came up ({mic_clicks} unmute attempts)")

    mic = kiosk_call(
        session,
        "(function(){var k=window.__portalKiosk;"
        "var m=k.byLabel(/turn on microphone/i);"
        "if(m){m.click();return 'unmuted';} "
        "if(k.byLabel(/turn off microphone/i)) return 'already-on';"
        # New UI: no mic toggle exists; the mic starts hot. A live captured
        # getUserMedia track (see kiosk _micStream) is the on-signal.
        "if(k._micStream&&k._micStream.getAudioTracks().some(function(t){"
        "return t.readyState==='live'&&t.enabled;})) return 'track-live';"
        "return 'no-mic-control';})()")
    add_step("mic_on", mic in ("unmuted", "already-on", "track-live"), str(mic))

    # 4. Focus mode — ChatGPT's own orb-forward view.
    focus = kiosk_call(
        session,
        "(function(){if(window.__portalKiosk.byLabel(/exit focus mode/i))return 'already';"
        "var f=window.__portalKiosk.byLabel(/enter focus mode/i);"
        "if(!f) return 'no-focus-control'; f.click(); return 'entered';})()")
    add_step("focus_mode", focus in ("entered", "already"), str(focus))

    # 5. Strip the page to the orb and reveal it. The orb canvas animates in
    # after focus mode, so apply() can legitimately report no-orb for a moment
    # — poll fast rather than giving up on the first miss or waiting a full
    # second between tries.
    applied = "no-orb"
    deadline = time.time() + 20
    while time.time() < deadline:
        applied = kiosk_call(session, "window.__portalKiosk.apply()")
        if applied == "kiosk-applied":
            break
        time.sleep(0.25)
    add_step("kiosk_orb_only", applied == "kiosk-applied", str(applied))
    kiosk_call(session, "window.__portalKiosk.overlayDone()")
    kiosk_call(session, "window.__portalKiosk.markLive()", gesture=False)

    snap = kiosk_call(session, "JSON.stringify(window.__portalKiosk.snapshot())", gesture=False)
    try:
        snap = json.loads(snap)
    except Exception:
        snap = {"raw": str(snap)[:300]}
    add_step("voice_snapshot", bool(snap.get("voiceLive")), json.dumps(snap)[:400])
    return snap

def enter_voice_verified(session: CDPSession, add_step) -> Dict[str, Any]:
    """Run the open sequence and prove the result, retrying once from a clean
    page if it did not actually come up live.

    Speed made a new failure possible: a warm tab can still hold the spent
    voice UI, so the sequence can 'succeed' in 5s against stale DOM and leave
    a dead orb on the wall. A lane is only open if the call is live AND the
    microphone is on.
    """
    snap = enter_voice_sequence(session, add_step)
    if snap.get("voiceLive") and snap.get("micOn"):
        return snap
    add_step("voice_verify_failed", False, json.dumps(snap)[:200] + " -> reloading clean")
    try:
        session.send("Page.navigate", {"url": CHATGPT_URL}, timeout=8)
    except Exception:
        pass
    # Wait for the reload to settle before driving the fresh page.
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(0.25)
        if kiosk_call(session, "!!window.__portalKiosk", gesture=False) is True:
            break
    do_inject_chip(session)
    do_fullscreen(session)
    return enter_voice_sequence(session, add_step)

def do_fullscreen(session: CDPSession) -> tuple[bool, str]:
    try:
        # Use awaitPromise=true to wait for the fullscreen promise to resolve
        js = "document.documentElement.requestFullscreen().then(()=> 'fs-ok').catch(e=> 'fs-err:'+e)"
        res = session.evaluate(js, user_gesture=True, await_promise=True, timeout=8)
        val = res.get("result", {}).get("result", {}).get("value") or json.dumps(res)[:600]
        ok = "fs-ok" in str(val)
        return ok, str(val)[:600]
    except Exception as e:
        return False, str(e)

def do_voice_button(session: CDPSession) -> tuple[Optional[str], str]:
    """Best-effort, non-fatal. Returns (matched_selector_or_None, detail)"""
    for sel in VOICE_SELECTORS:
        try:
            # Escape selector for JS string: use json dumps for quoting
            js_sel = json.dumps(sel)
            js = f"""
(() => {{
  const sel={js_sel};
  const el=document.querySelector(sel);
  if(!el) return 'not-found';
  el.click();
  return 'clicked:'+sel;
}})()
"""
            res = session.evaluate(js, user_gesture=True, timeout=6)
            r = res.get("result", {}).get("result", {}).get("value", "")
            if isinstance(r, str) and r.startswith("clicked:"):
                return sel, r
            # else continue
        except Exception as ex:
            _log_event("voice_selector_err", f"{sel} -> {ex}")
            continue
    # ChatGPT renames these controls often, so on a miss report what the page
    # actually offers instead of just "not found". Logged out you only get
    # "Start dictation" (speech-to-text into the box) — voice mode, the thing
    # this lane exists for, appears once the account is signed in.
    labels = []
    try:
        res = session.evaluate(
            "JSON.stringify([...document.querySelectorAll('button')]"
            ".map(b=>b.getAttribute('aria-label')).filter(Boolean))",
            timeout=6)
        labels = res.get("result", {}).get("result", {}).get("value", "")
    except Exception:
        pass
    return None, f"no selector matched (expected if logged out); page buttons={labels}"

def _mic_released_by_chrome() -> bool:
    """True when Chrome is no longer holding a capture session.

    The whole warm-tab optimisation rests on this: if ending the call really
    frees the microphone we can leave Chrome running, and Jarvis still gets its
    ears back. If it does not, we must force-stop, or the household is left
    with a deaf Jarvis — the exact failure this guard prevents.
    """
    try:
        cp = adb_cmd(["shell", "dumpsys", "audio"], timeout=12)
        out = cp.stdout + cp.stderr
        block = out.split("RecordActivityMonitor")[-1].split("Audio event log")[0]
        return "org.chromium.chrome" not in block
    except Exception:
        return False

def _graceful_end_call() -> Dict[str, Any]:
    """Click ChatGPT's own End Voice, release the kiosk, leave the tab loaded."""
    lane = _lane_snapshot()
    ws_url = lane.get("ws_debugger_url")
    if not ws_url:
        ok, tabs = cdp_list_tabs()
        tab = cdp_find_chatgpt_tab(tabs) if ok else None
        ws_url = (tab or {}).get("webSocketDebuggerUrl")
    if not ws_url:
        return {"ok": False, "detail": "no tab"}
    sess = None
    try:
        sess = CDPSession(ws_url)
        sess.connect(timeout=6)
        sess.send("Runtime.enable", {}, timeout=4)
        val = js_value(sess.evaluate(
            "(function(){var b=document.querySelectorAll('button'),e=null,f=null,v=false;"
            "for(var i=0;i<b.length;i++){var a=b[i].getAttribute('aria-label')||'';"
            "if(/end voice/i.test(a))e=b[i]; if(/exit focus mode/i.test(a))f=b[i];"
            "if(/voice mode selector/i.test(a))v=true;}"
            "if(f)f.click(); if(e){e.click(); return 'ended';}"
            # New UI has no End-voice button: leaving voice = navigating home.
            # Force-stop remains the guarantee either way.
            "if(v){location.assign('https://chatgpt.com/'); return 'ended';}"
            "return 'no-end-button';})()",
            user_gesture=True, timeout=6))
        try:
            sess.evaluate("window.__portalKiosk&&window.__portalKiosk.release()", timeout=5)
            sess.evaluate("try{document.exitFullscreen()}catch(e){}", timeout=5)
        except Exception:
            pass
        time.sleep(1.2)   # let the peer connection tear down and free the mic
        return {"ok": val == "ended", "detail": str(val)[:80]}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:120]}
    finally:
        if sess:
            try:
                sess.close()
            except Exception:
                pass

def stop_lane_impl(reason: str = "api_stop") -> Dict[str, Any]:
    """Idempotent stop. Never throws. Blocks until any in-flight start has
    finished — see _op_lock — so a stop requested mid-open is never lost."""
    _op_lock.acquire()
    try:
        return _stop_lane_locked(reason)
    finally:
        _op_lock.release()

def _stop_lane_locked(reason: str) -> Dict[str, Any]:
    _log_event("stop_start", reason)
    results: Dict[str, Any] = {"reason": reason}
    # Mark inactive BEFORE flipping the HA boolean off, for the same reason
    # the start path marks active before flipping it on (see the comment at
    # handle_start's ha_input_boolean_on step): otherwise the HA websocket
    # event for "off" can reach _ha_ws_loop while local `active` is still
    # True, and its `new=="off" and active` guard fires a redundant duplicate
    # stop_lane_impl() that queues on _op_lock and force-stops Chrome again
    # moments after the next start.
    _set_lane(active=False)
    # Kill exit watcher first to prevent loops
    global _cdp_ws
    with _cdp_ws_lock:
        if _cdp_ws:
            try:
                _cdp_ws.close()
            except: pass
            _cdp_ws = None

    _exit_watcher_stop.set()
    # Try to exit fullscreen via CDP if we still have session info
    try:
        lane = _lane_snapshot()
        ws_url = lane.get("ws_debugger_url")
        if ws_url:
            try:
                sess = CDPSession(ws_url)
                sess.connect(timeout=5)
                try:
                    sess.evaluate("try{document.exitFullscreen()}catch(e){}", timeout=5)
                except: pass
                sess.close()
            except: pass
    except: pass
    results["exit_fullscreen"] = "attempted"

    # Always force-stop Chrome.
    #
    # Keeping the tab warm was tried (2026-08-02) because it cut startup from
    # 19s to 8.8s: end the call, leave Chrome running, reuse the signed-in tab.
    # It was rolled back. A reused tab reaches the next open in an
    # unpredictable state — twice in a row the sequence "succeeded" against
    # leftover voice DOM and put a DEAD orb on the wall, live=false mic=false,
    # in under 5 seconds. Navigating the tab home on exit did not fix it.
    # A fast lane that sometimes does not listen is worse than a slower one
    # that always does, so the browser starts clean every time.
    try:
        _graceful_end_call()   # releases the mic promptly; force-stop is the guarantee
    except Exception:
        pass
    try:
        cp = adb_cmd(["shell", "am", "force-stop", "org.chromium.chrome"], timeout=10)
        results["force_stop_chrome"] = {"ok": cp.returncode == 0, "out": (cp.stdout+cp.stderr)[:200]}
    except Exception as e:
        results["force_stop_chrome"] = {"ok": False, "err": str(e)[:300]}

    # Relaunch VACA
    try:
        cp = adb_cmd(["shell", "monkey", "-p", "com.msp1974.vacompanion", "-c", "android.intent.category.LAUNCHER", "1"], timeout=15)
        # Block until VACA is really in front. Returning early is what lets a
        # late relaunch land in the middle of the NEXT open and steal the
        # screen from Chrome.
        deadline = time.time() + 12
        settled = False
        while time.time() < deadline:
            time.sleep(0.4)
            if get_foreground_package() == "com.msp1974.vacompanion":
                settled = True
                break
        results["relaunch_vaca"] = {"ok": True, "settled": settled}
    except Exception as e:
        results["relaunch_vaca"] = {"ok": False, "err": str(e)[:300]}

    # HA off
    try:
        ok, detail = ha_set_input_boolean("input_boolean.portal_chatgpt_active", False)
        results["ha_off"] = {"ok": ok, "detail": detail[:400]}
    except Exception as e:
        results["ha_off"] = {"ok": False, "err": str(e)[:300]}

    # Give Jarvis its ears back — the start path muted VACA to free the mic.
    try:
        ok, detail = ha_set_switch(VACA_MUTE_SWITCH, False)
        results["vaca_mic_unmuted"] = {"ok": ok, "detail": detail[:300]}
    except Exception as e:
        results["vaca_mic_unmuted"] = {"ok": False, "err": str(e)[:300]}

    _set_lane(active=False, chatgpt_tab_url=None, chatgpt_tab_id=None, ws_debugger_url=None)
    _log_event("stop_complete", results)
    return results

# ---------------------------------------------------------------------------
# Exit watcher + idle watchdog threads
# ---------------------------------------------------------------------------
def _exit_watcher_loop():
    _log_event("exit_watcher_started", None)
    consecutive_failures = 0
    while not _exit_watcher_stop.is_set():
        lane = _lane_snapshot()
        if not lane.get("active"):
            time.sleep(1)
            continue
        ws_url = lane.get("ws_debugger_url")
        if not ws_url:
            time.sleep(2)
            continue
        try:
            sess = CDPSession(ws_url)
            sess.connect(timeout=8)
            sess.send("Runtime.enable", {}, timeout=5)
            # The kiosk layer has to be owned by THIS session.
            # addScriptToEvaluateOnNewDocument registrations die with the session
            # that made them, so anything injected by the short-lived /api/start
            # session disappears the moment chatgpt.com navigates. Re-register
            # here and re-assert on every pass below.
            try:
                sess.send("Page.enable", {}, timeout=5)
                sess.send("Page.addScriptToEvaluateOnNewDocument",
                          {"source": load_kiosk_js()}, timeout=5)
            except Exception as ex:
                _log_event("exit_watcher_newdoc_register_failed", str(ex)[:200])
            # closure captures stop logic
            def on_console(msg):
                try:
                    if msg.get("method") == "Runtime.consoleAPICalled":
                        params = msg.get("params", {})
                        args = params.get("args", [])
                        # Look for PORTAL_EXIT in any arg
                        for a in args:
                            v = a.get("value", "") or a.get("description", "") or ""
                            # Also check deep: if object preview contains string
                            txt = str(v)
                            # Some consoleAPICalled uses args with value directly
                            # fallback: check entire params json string
                            if "PORTAL_EXIT" in txt or "PORTAL_EXIT" in json.dumps(params):
                                _log_event("console_PORTAL_EXIT_received", params)
                                _set_lane(last_activity_epoch=time.time())
                                # Run stop
                                stop_lane_impl(reason="exit_chip_PORTAL_EXIT")
                                return
                        # also check stringified params for safety
                        if "PORTAL_EXIT" in json.dumps(params):
                            _log_event("console_PORTAL_EXIT_received_fallback", params)
                            stop_lane_impl(reason="exit_chip_PORTAL_EXIT_fallback")
                except Exception as ex:
                    _log_event("exit_watcher_cb_err", str(ex)[:300])

            sess.on_event(on_console)
            # keep ws alive
            with _cdp_ws_lock:
                global _cdp_ws
                _cdp_ws = sess
            consecutive_failures = 0
            _log_event("exit_watcher_connected", ws_url[:120])
            # loop until stop or disconnect
            while not _exit_watcher_stop.is_set():
                lane2 = _lane_snapshot()
                if not lane2.get("active"):
                    break
                # keepalive: reader thread handles recv; check if ws still alive by ping?
                # We'll just sleep and let reader thread stay; detect dead by trying evaluate occasionally
                time.sleep(2)
                # If lane changed ws_url, break to reconnect
                if _lane_snapshot().get("ws_debugger_url") != ws_url:
                    _log_event("exit_watcher_ws_url_changed_reconnect", None)
                    break
                # Re-assert the kiosk layer. Idempotent, and the only thing that
                # survives chatgpt.com's SPA re-renders and full document swaps.
                try:
                    sess.evaluate(
                        "(window.__portalKiosk ? 'ok' : 'gone')", timeout=5)
                    if js_value(sess.evaluate("(!!window.__portalKiosk)", timeout=5)) is not True:
                        sess.evaluate(load_kiosk_js(), user_gesture=True, timeout=10)
                    # Any navigation on chatgpt.com drops fullscreen, which puts the
                    # browser tab strip and URL bar back on a wall display. Re-enter it.
                    # userGesture is what makes requestFullscreen legal here.
                    sess.evaluate(
                        "(function(){if(!document.fullscreenElement){"
                        "document.documentElement.requestFullscreen().catch(function(){});"
                        "return 'refullscreened';}return 'already';})()",
                        user_gesture=True, timeout=5)
                except Exception as ex:
                    _log_event("exit_watcher_chip_reassert_failed", str(ex)[:200])
                    break
                # Try a harmless eval to detect dead socket (optional)
                # Skip to avoid spam; rely on sess._ws.connected if available
                try:
                    # websocket-client .connected may exist
                    if hasattr(sess._ws, 'connected') and not sess._ws.connected:
                        raise RuntimeError("ws not connected")
                except Exception as ex:
                    _log_event("exit_watcher_dead_detected", str(ex)[:200])
                    break
            try:
                sess.close()
            except: pass
            with _cdp_ws_lock:
                if _cdp_ws is sess:
                    _cdp_ws = None
        except Exception as e:
            consecutive_failures += 1
            _log_event("exit_watcher_conn_fail", f"{e} fail#{consecutive_failures}")
            time.sleep(min(2 * consecutive_failures, 10))
    _log_event("exit_watcher_stopped", None)

def _kiosk_idle_seconds() -> Optional[float]:
    """Seconds since the last real speech on either side, or None if the kiosk
    layer can't be reached. Uses its own short-lived CDP session so it never
    contends with the exit watcher's socket."""
    lane = _lane_snapshot()
    ws_url = lane.get("ws_debugger_url")
    if not ws_url:
        return None
    sess = None
    try:
        sess = CDPSession(ws_url)
        sess.connect(timeout=6)
        res = sess.evaluate(
            "(window.__portalKiosk ? window.__portalKiosk.idleMs() : -1)", timeout=6)
        val = js_value(res)
        if not isinstance(val, (int, float)) or val < 0:
            return None
        return float(val) / 1000.0
    except Exception:
        return None
    finally:
        if sess:
            try:
                sess.close()
            except Exception:
                pass

def _resume_voice_if_dropped() -> Optional[str]:
    """ChatGPT navigates from / to /c/<id> after the first answer, and on this
    device that is a FULL document load: the WebRTC call dies, the kiosk is
    wiped and the user is staring at the normal chat UI mid-conversation.

    We do not control chatgpt.com, so the lane heals itself — re-attach to
    whatever tab is now current and drive it back into a live orb-only call.
    The conversation continues in the same chat, so the user just sees the orb
    return and can keep talking.
    """
    lane = _lane_snapshot()
    if not lane.get("active"):
        return None
    ok, tabs = cdp_list_tabs()
    if not ok:
        return None
    tab = cdp_find_chatgpt_tab(tabs)
    if not tab:
        return None
    ws_url = tab.get("webSocketDebuggerUrl")
    sess = None
    try:
        sess = CDPSession(ws_url)
        sess.connect(timeout=8)
        sess.send("Runtime.enable", {}, timeout=5)
        live = js_value(sess.evaluate(
            "(function(){var b=document.querySelectorAll('button');"
            "for(var i=0;i<b.length;i++){var a=b[i].getAttribute('aria-label')||'';"
            "if(/end voice/i.test(a)||/voice mode selector/i.test(a)) return true;}"
            "return false;})()", timeout=6))
        if live is True:
            # Call is fine; just make sure the tab we track is the current one.
            if ws_url != lane.get("ws_debugger_url"):
                _set_lane(ws_debugger_url=ws_url, chatgpt_tab_id=tab.get("id"),
                          chatgpt_tab_url=tab.get("url"))
            return None
        _log_event("voice_dropped_resuming", (tab.get("url") or "")[:80])
        do_inject_chip(sess)
        do_fullscreen(sess)
        steps: List[Dict[str, Any]] = []
        enter_voice_sequence(sess, lambda n, o, d: steps.append({"name": n, "ok": o}))
        _set_lane(ws_debugger_url=ws_url, chatgpt_tab_id=tab.get("id"),
                  chatgpt_tab_url=tab.get("url"))
        _log_event("voice_resumed", json.dumps(steps)[:300])
        return "resumed"
    except Exception as e:
        _log_event("voice_resume_failed", str(e)[:200])
        return None
    finally:
        if sess:
            try:
                sess.close()
            except Exception:
                pass

def _idle_watchdog_loop():
    _log_event("idle_watchdog_started", f"idle={IDLE_TIMEOUT_SEC}s")
    while not _idle_watcher_stop.is_set():
        time.sleep(10)
        lane = _lane_snapshot()
        if not lane.get("active"):
            continue
        # Check foreground. VACA re-fronts ITSELF while the lane is open (the
        # presence automation wakes the screen when someone stands near the
        # Portal — i.e. exactly when someone is using ChatGPT), so a foreground
        # flip to VACA is noise, not an exit: shove Chromium back to the front.
        # The lane has exactly three legitimate exits — the on-screen chip, the
        # HA boolean, and the idle timeout below. Give up re-fronting only if
        # it clearly isn't sticking.
        try:
            pkg = get_foreground_package()
            if pkg and pkg != "org.chromium.chrome":
                # Require two consecutive misses. A single miss is almost always
                # a transient — most often VACA's own relaunch from the previous
                # lane's teardown landing late — and re-fronting Chrome into
                # that race reloads the renderer and kills the live call.
                misses = (_lane_snapshot().get("fg_misses") or 0) + 1
                _set_lane(fg_misses=misses)
                if misses < 2:
                    _log_event("idle_watchdog_fg_miss_ignored", f"fg={pkg} miss {misses}")
                    continue
                refront_fails = _lane_snapshot().get("refront_fails") or 0
                if refront_fails >= 5:
                    _log_event("idle_watchdog_refront_gave_up", f"fg={pkg} after {refront_fails} attempts -> stopping")
                    stop_lane_impl(reason=f"idle_fg_{pkg}")
                    continue
                _log_event("idle_watchdog_refronting_chrome", f"fg={pkg} attempt {refront_fails + 1}")
                # REORDER_TO_FRONT (0x00020000) | NEW_TASK (0x10000000).
                # A plain `am start` RE-CREATES the activity, which reloads the
                # tab and kills a live voice call — that is exactly what killed
                # the 12:47 session. Reordering brings the existing instance
                # forward and leaves the page, and the WebRTC call, untouched.
                adb_cmd(["shell", "am", "start", "-f", "0x10020000", "-n",
                         "org.chromium.chrome/com.google.android.apps.chrome.Main"], timeout=10)
                time.sleep(2)
                if get_foreground_package() == "org.chromium.chrome":
                    _set_lane(refront_fails=0)
                else:
                    _set_lane(refront_fails=refront_fails + 1)
                continue
            elif pkg == "org.chromium.chrome":
                if _lane_snapshot().get("refront_fails") or _lane_snapshot().get("fg_misses"):
                    _set_lane(refront_fails=0, fg_misses=0)
        except Exception as e:
            _log_event("idle_watchdog_fg_err", str(e)[:200])
        # Idle is measured in SPEECH, not wall-clock: the kiosk layer taps the
        # microphone and ChatGPT's WebRTC reply track, so a long thinking pause
        # or a slow reply never counts as idle, and a room that has genuinely
        # gone quiet closes the lane and hands the screen back to Jarvis.
        try:
            # Grace period: never close a lane that only just came up. Belt and
            # braces alongside markLive() — if the kiosk failed to stamp, the
            # user still gets time to say the first thing.
            started = lane.get("start_epoch") or 0
            if started and time.time() - started < LANE_GRACE_SEC:
                continue
            # If the browser itself is gone (renderer crash, Chrome killed),
            # the lane must not linger "on": VACA stays muted and the household
            # gets a dashboard whose Jarvis is deaf, with no visible cause.
            # Two consecutive misses so a momentary CDP hiccup is not fatal.
            ok_tabs, tabs = cdp_list_tabs()
            if not ok_tabs or not cdp_find_chatgpt_tab(tabs):
                gone = (_lane_snapshot().get("tab_gone") or 0) + 1
                _set_lane(tab_gone=gone)
                if gone >= 2:
                    _log_event("idle_watchdog_tab_gone", "no chatgpt tab -> closing lane")
                    stop_lane_impl(reason="chatgpt_tab_gone")
                continue
            _set_lane(tab_gone=0)

            # Heal a call that the /c/<id> navigation tore down before judging
            # idleness — a dropped call reads as silence and would close the
            # lane in the middle of a conversation.
            if _resume_voice_if_dropped() == "resumed":
                continue
            voice_idle = _kiosk_idle_seconds()
            if voice_idle is not None:
                if voice_idle > VOICE_IDLE_SEC:
                    _log_event("idle_watchdog_voice_silence",
                               f"no speech for {voice_idle:.0f}s > {VOICE_IDLE_SEC}s -> closing")
                    stop_lane_impl(reason=f"voice_idle_{VOICE_IDLE_SEC}s")
                continue
            # Kiosk unreachable (page dead / never installed): fall back to the
            # coarse wall-clock ceiling so a broken lane still self-heals.
            last = lane.get("last_activity_epoch") or lane.get("start_epoch") or time.time()
            if time.time() - last > IDLE_TIMEOUT_SEC:
                _log_event("idle_watchdog_timeout", f"kiosk unreachable, wall-clock idle > {IDLE_TIMEOUT_SEC}s")
                stop_lane_impl(reason=f"idle_timeout_{IDLE_TIMEOUT_SEC}s")
                continue
        except Exception as e:
            _log_event("idle_watchdog_idle_err", str(e)[:200])
    _log_event("idle_watchdog_stopped", None)

def start_watchers():
    global _exit_watcher_thread, _idle_watcher_thread
    _exit_watcher_stop.clear()
    _idle_watcher_stop.clear()
    if _exit_watcher_thread is None or not _exit_watcher_thread.is_alive():
        _exit_watcher_thread = threading.Thread(target=_exit_watcher_loop, daemon=True, name="exit-watcher")
        _exit_watcher_thread.start()
    if _idle_watcher_thread is None or not _idle_watcher_thread.is_alive():
        _idle_watcher_thread = threading.Thread(target=_idle_watchdog_loop, daemon=True, name="idle-watchdog")
        _idle_watcher_thread.start()

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # suppress default; we use _log_event
        print(f"[http] {self.client_address[0]} {fmt % args}", flush=True)

    def _client_allowed(self) -> bool:
        client_ip = self.client_address[0]
        # Allow if any allowed prefix matches? spec says exact list contains 127.0.0.1 and 192.168.4.62
        # We'll do exact match, but also allow ::1 and 127.* if 127.0.0.1 in list
        if client_ip in ALLOWED_CLIENTS:
            return True
        # allow 127.0.0.1 equivalent checks
        if client_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            if "127.0.0.1" in ALLOWED_CLIENTS:
                return True
        # Also allow if client_ip startswith allowed? Keep strict exact only per spec.
        # But Mac may have 192.168.4.x as client if HA is .62 — only that ip allowed.
        return False

    def _json_response(self, code: int, obj: Any):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._client_allowed():
            self._json_response(403, {"ok": False, "error": f"client {self.client_address[0]} not in ALLOWED_CLIENTS"})
            return
        if self.path.startswith("/api/health"):
            self.handle_health()
        elif self.path.startswith("/api/status"):
            self.handle_status()
        else:
            self._json_response(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._client_allowed():
            self._json_response(403, {"ok": False, "error": f"client {self.client_address[0]} not in ALLOWED_CLIENTS"})
            return
        # drain body
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        if self.path.startswith("/api/start"):
            self.handle_start()
        elif self.path.startswith("/api/stop"):
            self.handle_stop()
        else:
            self._json_response(404, {"ok": False, "error": "not found"})

    def handle_health(self):
        adb_connected, adb_detail = adb_check_connected()
        fg_activity, _fg_raw = get_foreground_activity()
        cdp_ok, tabs = cdp_list_tabs()
        chrome_version = cdp_get_chrome_version()
        lane = _lane_snapshot()
        # determine chatgpt_tab_url from cdp if lane active or if any tabs contain chatgpt
        chatgpt_tab_url = lane.get("chatgpt_tab_url")
        if not chatgpt_tab_url and cdp_ok and isinstance(tabs, list):
            found = cdp_find_chatgpt_tab(tabs)
            if found:
                chatgpt_tab_url = found.get("url")
        try:
            portal_awake, dreaming, _ = is_portal_awake()
        except:
            portal_awake = False
        obj = {
            "ok": True,
            "adb_connected": adb_connected,
            "portal_awake": portal_awake,
            "foreground_activity": fg_activity,
            "cdp_ok": cdp_ok,
            "chrome_version": chrome_version,
            "chatgpt_tab_url": chatgpt_tab_url,
            "lane_active": lane.get("active", False),
            "uptime_sec": int(time.time() - START_TIME),
            "version": VERSION,
        }
        self._json_response(200, obj)

    def handle_status(self):
        lane = _lane_snapshot()
        with _events_lock:
            ev = list(_events[-20:])
        obj = {
            "active": lane.get("active"),
            "chatgpt_tab_url": lane.get("chatgpt_tab_url"),
            "chatgpt_tab_id": lane.get("chatgpt_tab_id"),
            "start_epoch": lane.get("start_epoch"),
            "last_activity_epoch": lane.get("last_activity_epoch"),
            "uptime_sec": int(time.time() - START_TIME),
            "version": VERSION,
            "events": ev,
        }
        self._json_response(200, obj)

    def handle_start(self):
        # Serialize against any other start/stop (see _op_lock). A start that
        # is already running (e.g. a double-tapped pill, or the voice-phrase
        # automation firing a beat after the button) must not drive a second
        # CDP session against the same tab, so this blocks rather than races.
        if not _op_lock.acquire(timeout=40):
            self._json_response(409, {"ok": False, "error": "another start/stop is still in flight"})
            return
        try:
            self._handle_start_locked()
        finally:
            _op_lock.release()

    def _handle_start_locked(self):
        # Idempotent: if a lane is already live, a second /api/start (double
        # tap, or the HA boolean already on) is a no-op success, not a second
        # open racing the first over the same tab.
        if _lane_snapshot().get("active"):
            _log_event("start_skip_already_active", None)
            self._json_response(200, {"ok": True, "steps": [], "lane": _lane_snapshot(),
                                       "note": "lane already active, no-op"})
            return
        steps = []
        def add_step(name: str, ok: bool, detail: str):
            steps.append({"name": name, "ok": ok, "detail": (detail or '')[:1000]})
            _log_event(f"start_step:{name}", f"ok={ok} detail={detail[:300]}")

        # Any early failure below (steps 1-9) must fail CLOSED: the dock pill
        # and/or the HA voice-phrase automation already flipped
        # input_boolean.portal_chatgpt_active to "on" before this handler
        # ever ran (that flip is what triggers open_lane_from_ha() in the
        # first place) or the caller otherwise expects the boolean to reflect
        # reality. Only the voice_open_failed branch (step 10, below) used to
        # clean up after itself; every earlier return just replied 500 and
        # left the boolean stuck "on" with no Chrome and no orb running —
        # reproduced live 2026-08-03 when a second adb device (a phone on
        # USB) made cdp_forward fail. _stop_lane_locked is idempotent and
        # safe to call even when nothing actually started: force-stopping
        # Chrome/relaunching VACA are no-ops if they were never touched, and
        # it always flips the boolean back off.
        def fail_closed(error: str) -> None:
            stop_res = _stop_lane_locked(f"start_failed:{error}")
            self._json_response(500, {"ok": False, "error": error, "steps": steps,
                                       "lane": _lane_snapshot(), "stop_result": stop_res})

        now = time.time()
        # 1. adb connect + confirm device
        ok1, d1 = do_adb_connect()
        add_step("adb_connect", ok1, d1)
        if not ok1:
            fail_closed("adb_connect failed")
            return

        # 2. wakeup + dismiss screensaver if dreaming
        ok2, d2 = do_wakeup_and_dismiss()
        add_step("wakeup_dismiss", ok2, d2)
        # non-fatal if wake check fails, but report

        # 3. adb forward
        ok3, d3 = do_forward()
        add_step("cdp_forward", ok3, d3)
        if not ok3:
            fail_closed("cdp_forward failed")
            return

        # 4. am start VIEW CHATGPT_URL in chrome
        ok4, d4 = do_launch_chrome()
        add_step("am_start_chrome_chatgpt", ok4, d4)
        if not ok4:
            fail_closed("am start chrome failed")
            return

        # 5. poll /json/list until chatgpt tab appears, then make sure exactly
        # one survives so we cannot attach to a stale duplicate.
        # No fixed sleep: the poller runs at 250ms and a warm tab is usually
        # there on the first look.
        ok5, tab_or_detail = do_poll_chatgpt_tab(timeout_sec=20)
        add_step("grant_mic_permission", True, grant_mic_permission())
        if ok5:
            add_step("prune_tabs", True, prune_chatgpt_tabs())
            ok5, tab_or_detail = do_poll_chatgpt_tab(timeout_sec=10)
        add_step("poll_chatgpt_tab", ok5, str(tab_or_detail)[:800] if not ok5 else json.dumps(tab_or_detail)[:800])
        if not ok5:
            fail_closed("chatgpt tab not found")
            return
        tab = tab_or_detail
        ws_url = tab.get("webSocketDebuggerUrl")
        tab_url = tab.get("url")
        tab_id = tab.get("id")

        # 6. attach CDP, enable Runtime and Page
        sess = None
        try:
            sess = CDPSession(ws_url)
            sess.connect(timeout=10)
            try:
                sess.send("Runtime.enable", {}, timeout=5)
                add_step("cdp_runtime_enable", True, "ok")
            except Exception as e:
                add_step("cdp_runtime_enable", False, str(e)[:500])
            try:
                sess.send("Page.enable", {}, timeout=5)
                add_step("cdp_page_enable", True, "ok")
            except Exception as e:
                add_step("cdp_page_enable", False, str(e)[:500])
        except Exception as e:
            add_step("cdp_attach", False, str(e)[:600])
            fail_closed(f"CDP attach failed: {e}")
            return
        add_step("cdp_attach", True, ws_url)

        # 7. inject exit chip live + newDocument
        ok7, d7 = do_inject_chip(sess)
        add_step("inject_exit_chip", ok7, d7)

        # 8. request fullscreen via Runtime.evaluate userGesture
        ok8, d8 = do_fullscreen(sess)
        add_step("request_fullscreen", ok8, d8)

        # 9. Yield the microphone BEFORE asking ChatGPT for it. Android 9 gives
        # the mic to ONE app: with VACA's wake-word listener capturing
        # (src:VOICE_RECOGNITION), ChatGPT's getUserMedia track opens and reads
        # pure silence — proven live 2026-08-02 (Chrome's capture lasted 142ms).
        # This has to happen before the voice sequence, not after it.
        ok9, d9 = ha_set_switch(VACA_MUTE_SWITCH, True)
        add_step("vaca_mic_muted", ok9, d9)
        time.sleep(1.5)

        # 10. Drive ChatGPT into a live, orb-only voice call.
        try:
            snap = enter_voice_verified(sess, add_step)
        except Exception as e:
            snap = {"error": str(e)[:300]}
            add_step("voice_sequence", False, str(e)[:400])

        voice_ok = bool(snap.get("voiceLive")) and bool(snap.get("micOn"))
        try:
            sess.close()
        except: pass

        if not voice_ok:
            # enter_voice_verified already retried once from a clean page.
            # Marking the lane "active" here anyway was the failure mode: a
            # dead orb sat on the wall with VACA muted and the HA boolean on,
            # looking identical to a healthy lane until the 60s+45s idle
            # watchdog eventually caught it. Fail closed instead — tear
            # everything down now, return VACA's mic and the dashboard
            # immediately, and report the real failure.
            add_step("voice_open_failed", False,
                     "call never came live after retry; closing lane instead of stranding it")
            _log_event("start_failed_closing_lane", json.dumps(snap)[:300])
            stop_res = _stop_lane_locked("voice_open_failed")
            self._json_response(200, {
                "ok": False,
                "error": "voice call never came up; lane closed",
                "steps": steps,
                "lane": _lane_snapshot(),
                "voice": snap,
                "stop_result": stop_res,
            })
            return

        # 11. Mark lane active BEFORE flipping the HA boolean — not after.
        # This order was the second self-inflicted race: flipping the boolean
        # on first let the HA websocket event reach _ha_ws_loop while local
        # `active` was still False, so its `new=="on" and not active` guard
        # spawned a duplicate open_lane_from_ha(). That duplicate then queued
        # on _op_lock and fired for real after the NEXT stop, which made a
        # closed lane silently reopen itself — reproduced live 2026-08-02:
        # start -> stop -> ghost restart -> stop -> another ghost restart,
        # indefinitely, each one re-arming the next via its own boolean flip.
        _set_lane(active=True, chatgpt_tab_url=tab_url, chatgpt_tab_id=tab_id,
                  ws_debugger_url=ws_url, start_epoch=now, last_activity_epoch=now)

        ok10, d10 = ha_set_input_boolean("input_boolean.portal_chatgpt_active", True)
        add_step("ha_input_boolean_on", ok10, d10)

        # Start watchers (idempotent)
        start_watchers()

        _log_event("lane_started", {"tab_url": tab_url, "tab_id": tab_id})

        self._json_response(200, {
            "ok": True,
            "steps": steps,
            "lane": _lane_snapshot(),
            "voice": snap,
        })

    def handle_stop(self):
        try:
            res = stop_lane_impl(reason="api_stop")
            # stop watchers? keep watchers alive but they will idle; do NOT kill threads — they check active flag
            # Ensure exit watcher stops its inner loop (it will because active=False)
            _exit_watcher_stop.set()  # will be cleared on next start via start_watchers; but we need watcher to remain alive for next lane
            # Actually we cleared stop flag in start_watchers, so set it now then immediately clear? No — we need watcher thread to exit inner loop but not die.
            # The watcher loop checks active; setting stop flag ensures current blocking reconnect exits. Then restart watchers.
            time.sleep(0.5)
            start_watchers()  # re-arm (clears stop flags)
            self._json_response(200, {"ok": True, "stop_result": res, "lane": _lane_snapshot()})
        except Exception as e:
            _log_event("stop_error", f"{e}\n{traceback.format_exc()[:500]}")
            # still try to mark inactive
            try:
                _set_lane(active=False)
            except: pass
            self._json_response(500, {"ok": False, "error": str(e)[:800]})

LANE_BOOLEAN = "input_boolean.portal_chatgpt_active"

def _ha_ws_loop():
    """Follow input_boolean.portal_chatgpt_active over the HA websocket.

    This is what lets the Portal dock pill (and any HA automation) drive the
    lane with a plain input_boolean toggle — no rest_command, no YAML edit and
    no Home Assistant restart. Feedback loops are impossible because we only
    act when HA and the lane actually disagree.
    """
    ws_url = HA_URL.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    backoff = 2
    while True:
        try:
            from websocket import create_connection
            ws = create_connection(ws_url, timeout=20, suppress_origin=True)
            ws.recv()  # auth_required
            ws.send(json.dumps({"type": "auth", "access_token": HASS_TOKEN}))
            if json.loads(ws.recv()).get("type") != "auth_ok":
                raise RuntimeError("HA websocket auth failed")
            ws.send(json.dumps({"id": 1, "type": "subscribe_events",
                                "event_type": "state_changed"}))
            _log_event("ha_ws_connected", ws_url)
            backoff = 2
            while True:
                msg = json.loads(ws.recv())
                if msg.get("type") != "event":
                    continue
                data = msg.get("event", {}).get("data", {})
                if data.get("entity_id") != LANE_BOOLEAN:
                    continue
                new = (data.get("new_state") or {}).get("state")
                active = bool(_lane_snapshot().get("active"))
                if new == "on" and not active:
                    _log_event("ha_ws_open_requested", None)
                    threading.Thread(target=open_lane_from_ha, daemon=True).start()
                elif new == "off" and active:
                    _log_event("ha_ws_close_requested", None)
                    threading.Thread(target=stop_lane_impl,
                                     kwargs={"reason": "ha_boolean_off"},
                                     daemon=True).start()
        except Exception as e:
            _log_event("ha_ws_error", str(e)[:200])
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

def open_lane_from_ha():
    """Same lane-open path as POST /api/start, driven by the HA boolean."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/start", method="POST", data=b"{}")
        with urllib.request.urlopen(req, timeout=120) as resp:
            _log_event("ha_ws_open_result", f"http={resp.status}")
    except Exception as e:
        _log_event("ha_ws_open_failed", str(e)[:300])

def reconcile_on_boot():
    """A restart of this daemon must never strand the Portal inside Chromium.

    Lane state lives in memory, so a fresh process has no idea a lane was open.
    If the Portal is sitting in the browser while HA says the lane is off, the
    household is looking at a wall display they cannot get out of — close it.
    """
    time.sleep(3)
    try:
        pkg = get_foreground_package()
        if pkg != "org.chromium.chrome":
            return
        req = urllib.request.Request(
            f"{HA_URL}/api/states/{LANE_BOOLEAN}",
            headers={"Authorization": f"Bearer {HASS_TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            state = json.loads(resp.read().decode()).get("state")
        if state == "off":
            _log_event("boot_reconcile_closing_stranded_lane", f"fg={pkg} ha={state}")
            stop_lane_impl(reason="boot_reconcile")
        elif state == "on":
            # A lane is LIVE and someone may be mid-conversation on it. Adopt
            # it instead of ignoring it: without adoption the exit chip is dead
            # (its console listener lived in the previous process) and nothing
            # would ever close the lane — the household would be stuck in a
            # fullscreen browser with no working exit.
            ok, _ = cdp_forward()
            ok2, tabs = cdp_list_tabs()
            tab = cdp_find_chatgpt_tab(tabs) if (ok and ok2) else None
            if tab:
                _set_lane(active=True, chatgpt_tab_url=tab.get("url"),
                          chatgpt_tab_id=tab.get("id"),
                          ws_debugger_url=tab.get("webSocketDebuggerUrl"),
                          start_epoch=time.time(), last_activity_epoch=time.time())
                _log_event("boot_reconcile_adopted_live_lane", tab.get("url", "")[:80])
            else:
                _log_event("boot_reconcile_adopt_failed_no_tab", "closing instead")
                stop_lane_impl(reason="boot_reconcile_no_tab")
    except Exception as e:
        _log_event("boot_reconcile_error", str(e)[:200])

def run_server():
    print(f"Starting portal_chatgpt_bridge on {HOST}:{PORT} adb={ADB} portal={PORTAL_SERIAL} cdp_port={CDP_PORT}", flush=True)
    threading.Thread(target=_ha_ws_loop, daemon=True, name="ha-ws").start()
    threading.Thread(target=reconcile_on_boot, daemon=True, name="boot-reconcile").start()
    _log_event("boot", f"host={HOST} port={PORT} adb={ADB} portal={PORTAL_SERIAL} cdp={CDP_PORT} allowed={ALLOWED_CLIENTS}")
    start_watchers()
    # socket reuse
    class ReuseTCPServer(http.server.ThreadingHTTPServer):
        allow_reuse_address = True
    server = ReuseTCPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _log_event("shutdown", None)
        _exit_watcher_stop.set()
        _idle_watcher_stop.set()

if __name__ == "__main__":
    run_server()
