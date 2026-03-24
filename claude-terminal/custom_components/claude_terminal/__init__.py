"""The Claude Terminal integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER

PLATFORMS: list[Platform | str] = [Platform.CONVERSATION]

# AI Task platform — only available on HA versions that support it
try:
    from homeassistant.components.ai_task import AITaskEntityFeature  # noqa: F401
    PLATFORMS.append("ai_task")
    LOGGER.debug("AI Task platform available, registering")
except ImportError:
    LOGGER.debug("AI Task platform not available on this HA version, skipping")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Claude Terminal from a config entry."""
    LOGGER.info("Setting up Claude Terminal integration")
    hass.data.setdefault(DOMAIN, {})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    LOGGER.info("Claude Terminal integration setup complete, platforms: %s", PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    LOGGER.info("Unloading Claude Terminal integration")
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN, None)
    return unload_ok
