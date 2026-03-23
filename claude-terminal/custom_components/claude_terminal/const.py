"""Constants for the Claude Terminal integration."""

import logging

DOMAIN = "claude_terminal"
LOGGER = logging.getLogger(__name__)

# Add-on API configuration
DEFAULT_ADDON_HOSTNAME = "claude-terminal"
DEFAULT_ADDON_PORT = 8099
API_QUERY_PATH = "/api/query"
API_HEALTH_PATH = "/api/health"
API_TIMEOUT_SECONDS = 130  # Slightly longer than the server's 120s timeout
