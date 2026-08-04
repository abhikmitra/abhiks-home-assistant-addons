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
            state=$(curl -s -m 8 -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
                "http://supervisor/core/api/states/${refresh_entity}" | jq -r '.state' 2>/dev/null)
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
                python3 /opt/flap.py "interval tick (every ${interval}m)" \
                    || bashio::log.warning "flap digest run failed"
                elapsed=0
            fi
        done
    ) &
}

main() {
    bashio::log.info "Initializing Codex Terminal add-on..."
    init_environment
    start_flap_loop
    start_web_terminal
}

main "$@"
