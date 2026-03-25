"""Conversation agent for Claude Terminal."""

from __future__ import annotations

from typing import Literal

from homeassistant.components.conversation import (
    ChatLog,
    ConversationEntity,
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
)
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.components import conversation as ha_conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ClaudeTerminalAPI, ClaudeTerminalAPIError
from .const import DOMAIN, LOGGER, get_addon_hostname


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the conversation entity."""
    LOGGER.info("Setting up Claude Terminal conversation entity")
    hostname = await hass.async_add_executor_job(get_addon_hostname)
    LOGGER.info("Using add-on hostname: %s", hostname)
    session = async_get_clientsession(hass)
    api = ClaudeTerminalAPI(session, hostname=hostname)
    hass.data[DOMAIN]["api"] = api
    async_add_entities([ClaudeTerminalConversationEntity(config_entry, api)])
    LOGGER.info("Claude Terminal conversation entity registered")


class ClaudeTerminalConversationEntity(ConversationEntity):
    """Claude Terminal conversation agent entity."""

    _attr_has_entity_name = True
    _attr_name = "Claude Terminal"
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, config_entry: ConfigEntry, api: ClaudeTerminalAPI) -> None:
        """Initialize the entity."""
        self._api = api
        self._attr_unique_id = f"{config_entry.entry_id}_conversation"

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return supported languages."""
        return "*"

    async def _resolve_user_name(self, user_id: str | None) -> str | None:
        """Resolve a user ID to a display name."""
        if not user_id:
            return None
        try:
            user = await self.hass.auth.async_get_user(user_id)
            if user:
                LOGGER.debug("Resolved user_id=%s to name=%s", user_id, user.name)
                return user.name
        except Exception:
            LOGGER.warning("Failed to resolve user_id=%s", user_id, exc_info=True)
        return None

    def _resolve_device_name(self, device_id: str | None) -> str | None:
        """Resolve a device ID to a display name."""
        if not device_id:
            return None
        try:
            registry = dr.async_get(self.hass)
            device = registry.async_get(device_id)
            if device:
                LOGGER.debug("Resolved device_id=%s to name=%s", device_id, device.name)
                return device.name
        except Exception:
            LOGGER.warning("Failed to resolve device_id=%s", device_id, exc_info=True)
        return None

    def _error_result(
        self, language: str, conversation_id: str | None, message: str
    ) -> ConversationResult:
        """Build an error ConversationResult."""
        response = intent.IntentResponse(language=language)
        response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, message)
        return ConversationResult(response=response, conversation_id=conversation_id)

    def _get_exposed_entities(self) -> list[dict]:
        """Return entities exposed to the conversation assistant."""
        interesting_attrs = (
            "friendly_name", "brightness", "color_temp", "hvac_mode",
            "current_temperature", "temperature", "media_title",
        )
        entities = []
        for state in self.hass.states.async_all():
            if not async_should_expose(self.hass, ha_conversation.DOMAIN, state.entity_id):
                continue
            attrs = state.attributes
            entity = {
                "entity_id": state.entity_id,
                "name": attrs.get("friendly_name", state.entity_id),
                "state": state.state,
                "attributes": {
                    k: attrs[k] for k in interesting_attrs if k in attrs
                },
            }
            entities.append(entity)
        return entities

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Handle a conversation message.

        Note: We intentionally bypass HA's ChatLog management. All context is
        managed by Claude Code's own session system via -p and --resume.
        """
        LOGGER.info(
            "Conversation request: text_length=%d, language=%s, conversation_id=%s, "
            "device_id=%s, satellite_id=%s, user_id=%s",
            len(user_input.text),
            user_input.language,
            user_input.conversation_id,
            user_input.device_id,
            getattr(user_input, 'satellite_id', None),
            user_input.context.user_id if user_input.context else None,
        )

        user_name = await self._resolve_user_name(
            user_input.context.user_id if user_input.context else None
        )
        device_name = self._resolve_device_name(user_input.device_id)
        satellite_name = self._resolve_device_name(
            getattr(user_input, 'satellite_id', None)
        )

        context = {
            "source": "conversation",
            "user_name": user_name,
            "device_name": device_name,
            "satellite_name": satellite_name,
            "language": user_input.language,
            "extra_system_prompt": getattr(user_input, 'extra_system_prompt', None),
            "exposed_entities": self._get_exposed_entities(),
        }

        LOGGER.debug("Built context for API call: %s", context)

        try:
            data = await self._api.async_query(
                query=user_input.text,
                context=context,
                conversation_id=user_input.conversation_id,
            )

            response_text = data.get("result", "I received your message but got an empty response.")
            session_id = data.get("session_id")

            LOGGER.info(
                "Conversation response: session_id=%s, response_length=%d, cost_usd=%s",
                session_id,
                len(response_text),
                data.get("cost_usd"),
            )

            response = intent.IntentResponse(language=user_input.language)
            response.async_set_speech(response_text)
            return ConversationResult(
                response=response,
                conversation_id=session_id,
            )

        except ClaudeTerminalAPIError as err:
            LOGGER.error("Claude Terminal API error: %s (code=%s)", err, err.code)
            return self._error_result(
                user_input.language,
                user_input.conversation_id,
                f"Sorry, I couldn't process your request: {err}",
            )
        except Exception:
            LOGGER.error("Unexpected error during conversation", exc_info=True)
            return self._error_result(
                user_input.language,
                user_input.conversation_id,
                "Sorry, an unexpected error occurred. Check the add-on logs for details.",
            )
