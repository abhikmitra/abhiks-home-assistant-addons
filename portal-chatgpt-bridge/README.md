# Portal ChatGPT Bridge

Runs the Meta Portal's second-assistant ChatGPT lane from the HA box itself,
removing the Mac from the flow entirely. The daemon follows
`input_boolean.portal_chatgpt_active` over the HA websocket; when the dock
pill (or the "chat gpt" voice phrase) flips it on, the bridge drives the
Portal's Chrome over ADB + Chrome DevTools Protocol into a fullscreen,
orb-only chatgpt.com voice call, and tears it down cleanly on exit.

**Canonical source:** `Day2DayAgentHelp/Agent-Tooling/meta-portal/` — the
`portal_chatgpt_bridge.py` and `portal_chatgpt_kiosk.js` here are deploy
copies. Edit there, copy here, bump the version in `config.yaml`, push, then
Add-on Store → Check for updates → Update.

## One-time setup

1. The Portal only trusts already-authorized adb keys. Copy the authorized
   pair (originally the Mac's `~/.android/adbkey{,.pub}`) into HA config:

   ```bash
   ssh root@192.168.4.62 "mkdir -p /homeassistant/portal-chatgpt-bridge"
   scp ~/.android/adbkey ~/.android/adbkey.pub \
       root@192.168.4.62:/homeassistant/portal-chatgpt-bridge/
   ```

   (The SSH add-on sees HA config at `/homeassistant`; this add-on sees the
   same directory at `/config`.)

2. Set `hass_token` in the add-on configuration to a long-lived HA token.

## Verify

```bash
curl -s http://192.168.4.62:8767/api/health   # lane_active, foreground, cdp_ok
curl -s http://192.168.4.62:8767/api/status   # event log; look for ha_ws_connected
```

Regression test (from any allowed client, e.g. the Mac):

```bash
BRIDGE=http://192.168.4.62:8767 python3 \
  Day2DayAgentHelp/Agent-Tooling/meta-portal/test_chatgpt_lane_stuck_selected.py
```

If the Mac's old LaunchAgent `com.day2day.portal-chatgpt-bridge` is ever
re-enabled alongside this add-on, both will fight over the lane — keep
exactly one running.
