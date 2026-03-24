"""Constants for the Claude Terminal integration."""

import logging
from pathlib import Path

DOMAIN = "claude_terminal"
LOGGER = logging.getLogger(__name__)

# Add-on API configuration
DEFAULT_ADDON_HOSTNAME = "claude-terminal"
DEFAULT_ADDON_PORT = 8099
API_QUERY_PATH = "/api/query"
API_HEALTH_PATH = "/api/health"
API_TIMEOUT_SECONDS = 130  # Slightly longer than the server's 120s timeout

# The add-on writes its actual hostname here during startup
ADDON_HOSTNAME_FILE = Path("/config/custom_components/claude_terminal/.addon_hostname")


def get_addon_hostname() -> str:
    """Read the add-on hostname written by run.sh, fall back to default."""
    try:
        if ADDON_HOSTNAME_FILE.exists():
            hostname = ADDON_HOSTNAME_FILE.read_text().strip()
            if hostname:
                LOGGER.info("Discovered add-on hostname from file: %s", hostname)
                return hostname
    except Exception:
        LOGGER.warning("Failed to read addon hostname file", exc_info=True)
    LOGGER.info("Using default add-on hostname: %s", DEFAULT_ADDON_HOSTNAME)
    return DEFAULT_ADDON_HOSTNAME
