#!/usr/bin/with-contenv bashio

set -e
set -o pipefail

# Initialize environment for Codex CLI using /data (HA best practice, persists across updates)
init_environment() {
    local data_home="/data/home"
    local codex_home="/data/.codex"

    bashio::log.info "Initializing Codex CLI environment in /data..."

    mkdir -p "$data_home" "$codex_home"
    chmod 755 "$data_home"
    chmod 700 "$codex_home"

    export HOME="$data_home"
    export CODEX_HOME="$codex_home"

    bashio::log.info "Environment initialized:"
    bashio::log.info "  - Home: $HOME"
    bashio::log.info "  - Codex home (auth persisted here): $CODEX_HOME"
}

start_web_terminal() {
    local port=7682
    bashio::log.info "Starting Codex web terminal on port ${port}..."

    local auto_launch_codex
    auto_launch_codex=$(bashio::config 'auto_launch_codex' 'true')

    local launch_command
    if [ "$auto_launch_codex" = "true" ]; then
        launch_command="tmux new-session -A -s codex 'codex'"
    else
        launch_command="tmux new-session -A -s codex bash"
    fi

    local ttyd_theme='{"background":"#1a1b26","foreground":"#c0caf5","cursor":"#10a37f","cursorAccent":"#1a1b26","selectionBackground":"#33467c","selectionForeground":"#c0caf5"}'

    exec ttyd \
        --port "${port}" \
        --interface 0.0.0.0 \
        --writable \
        --ping-interval 30 \
        --client-option enableReconnect=true \
        --client-option reconnect=10 \
        --client-option reconnectInterval=5 \
        --client-option "theme=${ttyd_theme}" \
        --client-option fontSize=14 \
        bash -c "$launch_command"
}

# Codex flap digest: every N minutes ask Codex for one board-worthy line and
# fire a portal_toast event (source_key codex_flap, ambient). 0 disables the
# scheduled tick but the refresh button still works.
# Manual triggers: `flap-now` in the web terminal, or the Portal's
# "Refresh board" button (input_boolean.portal_flap_refresh — polled every
# 5s; held on while the run is in flight so the button can show live state).
start_flap_loop() {
    local interval
    interval=$(bashio::config 'flap_interval_minutes' '60')
    cp /opt/flap.py /usr/local/bin/flap-now
    chmod +x /usr/local/bin/flap-now
    bashio::log.info "Flap digest: interval=${interval}m, refresh-button watcher every 5s"
    (
        local refresh_entity="input_boolean.portal_flap_refresh"
        local elapsed=$(( interval > 0 ? interval * 60 - 120 : 0 ))  # first tick 2m after boot
        while true; do
            sleep 5
            elapsed=$(( elapsed + 5 ))
            local state
            # `|| true`: run.sh's set -e is inherited here — without the guard one
            # failed poll (supervisor API not ready after restart) kills this
            # watcher permanently and the refresh button wedges.
            state=$(curl -s -m 8 -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
                "http://supervisor/core/api/states/${refresh_entity}" 2>/dev/null | jq -r '.state' 2>/dev/null || true)
            if [ "$state" = "on" ]; then
                bashio::log.info "Flap refresh requested from Portal button"
                python3 /opt/flap.py "manual refresh from the Portal board button" \
                    || bashio::log.warning "flap refresh run failed"
                curl -s -m 8 -X POST -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
                    -H "Content-Type: application/json" \
                    -d "{\"entity_id\":\"${refresh_entity}\"}" \
                    "http://supervisor/core/api/services/input_boolean/turn_off" > /dev/null
                elapsed=0
            elif [ "$interval" -gt 0 ] 2>/dev/null && [ "$elapsed" -ge $(( interval * 60 )) ]; then
                if python3 /opt/flap.py "interval tick (every ${interval}m)"; then
                    elapsed=0
                else
                    # A failed run must NOT wait the full hour — that is how the
                    # board went dry for 65 min on 2026-08-04 (revoked token).
                    # Retry in 10 minutes instead.
                    bashio::log.warning "flap digest run failed — retrying in 10m"
                    elapsed=$(( interval * 60 - 600 ))
                fi
            fi
        done
    ) &
}

# ChatGPT-app remote control: run the app-server with remote control enabled
# so the phone app can drive this Codex. Uses the standalone build installed
# under /data (the npm build ships no daemon, and codex's own daemonizer
# can't read /proc start times in this container — so we background it
# ourselves and keep it alive with a lightweight respawn loop).
# Pair from a terminal window: `codex-remote pair`.
start_remote_control() {
    local codex_bin="/data/codex-bin/codex"
    if [ ! -x "$codex_bin" ]; then
        bashio::log.info "Remote control: standalone codex not installed (/data/codex-bin) — skipping"
        return 0
    fi
    cat > /usr/local/bin/codex-remote <<'EOSH'
#!/bin/sh
exec env CODEX_HOME=/data/.codex /data/codex-bin/codex remote-control "$@"
EOSH
    chmod +x /usr/local/bin/codex-remote
    bashio::log.info "Remote control: starting app-server (respawn on exit)"
    (
        while true; do
            CODEX_HOME=/data/.codex "$codex_bin" app-server --remote-control \
                --listen unix:///data/.codex/app-server-control/app-server-control.sock \
                >> /data/.codex/app-server-manual.log 2>&1
            bashio::log.warning "Remote-control app-server exited; respawning in 30s"
            sleep 30
        done
    ) &
}

# Portal voice-codex bridge: the same codex_ha_bridge.py that used to run on
# the Mac, now Green-only. HA's rest_command.run_codex_task posts here and the
# reply lands in input_text.portal_codex_* for the Portal card + TTS.
start_codex_bridge() {
    if [ ! -x /data/codex-bin/codex ]; then
        bashio::log.info "Codex bridge: standalone codex missing — skipping"
        return 0
    fi
    bashio::log.info "Codex bridge: starting on :8766 (respawn on exit)"
    (
        while true; do
            env CODEX_BIN=/data/codex-bin/codex \
                CODEX_HOME=/data/.codex \
                HASS_SERVER=http://supervisor/core \
                HASS_TOKEN="${SUPERVISOR_TOKEN}" \
                HA_CONFIG_REPO=/config \
                CODEX_HA_ALLOWED_CLIENTS="127.0.0.1,::1,172.30.32.1,172.30.32.2,192.168.4.62" \
                CODEX_HA_REASONING_EFFORT=low \
                python3 /opt/codex_ha_bridge.py >> /data/codex-bridge.log 2>&1
            bashio::log.warning "Codex bridge exited; respawning in 30s"
            sleep 30
        done
    ) &
}

main() {
    bashio::log.info "Initializing Codex Terminal add-on..."
    init_environment
    mkdir -p /data/.codex/app-server-control
    start_remote_control
    start_codex_bridge
    start_flap_loop
    start_web_terminal
}

main "$@"
