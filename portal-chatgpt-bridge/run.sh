#!/usr/bin/with-contenv bashio
set -e

# --- adb key -----------------------------------------------------------------
# The Portal only trusts adb clients whose RSA key it has already authorized.
# The authorized key pair (originally the Mac's) is delivered out-of-band into
# HA config at /config/portal-chatgpt-bridge/{adbkey,adbkey.pub} — never into
# this git repo. Copy it to where adb looks ($HOME/.android).
export HOME=/data
mkdir -p /data/.android
KEY_SRC=/config/portal-chatgpt-bridge
if [ -f "${KEY_SRC}/adbkey" ]; then
    cp "${KEY_SRC}/adbkey" "${KEY_SRC}/adbkey.pub" /data/.android/ 2>/dev/null || true
    chmod 600 /data/.android/adbkey
    bashio::log.info "adb key installed from ${KEY_SRC}"
else
    bashio::log.warning "No adb key at ${KEY_SRC}/adbkey — Portal will show 'unauthorized'. \
Copy the authorized key pair there and restart the add-on."
fi

# --- bridge config from add-on options --------------------------------------
export HASS_TOKEN="$(bashio::config 'hass_token')"
export HA_URL="$(bashio::config 'ha_url')"
export PORTAL_SERIAL="$(bashio::config 'portal_serial')"
export ALLOWED_CLIENTS="$(bashio::config 'allowed_clients')"
export IDLE_TIMEOUT_SEC="$(bashio::config 'idle_timeout_sec')"
export VOICE_IDLE_SEC="$(bashio::config 'voice_idle_sec')"
export LANE_GRACE_SEC="$(bashio::config 'lane_grace_sec')"
export ADB=/usr/bin/adb
export VERSION="addon-$(bashio::addon.version 2>/dev/null || echo unknown)"

if [ -z "${HASS_TOKEN}" ]; then
    bashio::log.error "hass_token option is empty — the bridge cannot follow the HA boolean. \
Set a long-lived access token in the add-on configuration."
fi

bashio::log.info "Starting portal_chatgpt_bridge (portal=${PORTAL_SERIAL}, ha=${HA_URL})"
exec python3 /opt/bridge/portal_chatgpt_bridge.py
