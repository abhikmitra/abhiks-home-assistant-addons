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

main() {
    bashio::log.info "Initializing Codex Terminal add-on..."
    init_environment
    start_web_terminal
}

main "$@"
