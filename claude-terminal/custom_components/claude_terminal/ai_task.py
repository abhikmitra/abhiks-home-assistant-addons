"""AI Task entity for Claude Terminal."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.ai_task import AITaskEntity, AITaskEntityFeature
from homeassistant.components.ai_task.task import GenDataTask, GenDataTaskResult
from homeassistant.components.conversation import ChatLog
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ClaudeTerminalAPI, ClaudeTerminalAPIError
from .const import DOMAIN, LOGGER, get_addon_hostname


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AI Task entity."""
    LOGGER.info("Setting up Claude Terminal AI Task entity")
    # Reuse the API client if conversation entity already created it
    if "api" not in hass.data.get(DOMAIN, {}):
        hostname = await hass.async_add_executor_job(get_addon_hostname)
        session = async_get_clientsession(hass)
        api = ClaudeTerminalAPI(session, hostname=hostname)
        hass.data.setdefault(DOMAIN, {})["api"] = api
    api = hass.data[DOMAIN]["api"]
    async_add_entities([ClaudeTerminalAITaskEntity(config_entry, api)])
    LOGGER.info("Claude Terminal AI Task entity registered")


def _describe_structure(structure: Any) -> str:
    """Extract field descriptions from a vol.Schema for the prompt."""
    if structure is None:
        return ""

    # vol.Schema wraps a dict of vol.Required/Optional keys → selector validators.
    # We extract the key names and descriptions to guide Claude's JSON output.
    try:
        fields = []
        schema_dict = structure.schema if hasattr(structure, "schema") else structure
        for key, _validator in schema_dict.items():
            name = key.schema if hasattr(key, "schema") else str(key)
            desc = getattr(key, "description", None) or ""
            required = isinstance(key, type) and key.__name__ == "Required"  # rough check
            field_str = f'  - "{name}"'
            if desc:
                field_str += f": {desc}"
            if required:
                field_str += " (required)"
            fields.append(field_str)
        if fields:
            return "\n\nReturn a JSON object with these fields:\n" + "\n".join(fields)
    except Exception:
        LOGGER.debug("Could not extract structure fields", exc_info=True)
    return ""


class ClaudeTerminalAITaskEntity(AITaskEntity):
    """Claude Terminal AI Task entity."""

    _attr_has_entity_name = True
    _attr_name = "Claude Terminal"
    _attr_supported_features = AITaskEntityFeature.GENERATE_DATA

    def __init__(self, config_entry: ConfigEntry, api: ClaudeTerminalAPI) -> None:
        """Initialize the entity."""
        self._api = api
        self._attr_unique_id = f"{config_entry.entry_id}_ai_task"

    async def _async_generate_data(
        self,
        task: GenDataTask,
        chat_log: ChatLog,
    ) -> GenDataTaskResult:
        """Handle a generate data task.

        Note: We intentionally bypass HA's ChatLog. All context is managed
        by Claude Code's session system via the Agent SDK.
        """
        LOGGER.info(
            "AI Task request: task_name=%s, instructions_length=%d, has_structure=%s",
            task.name,
            len(task.instructions),
            task.structure is not None,
        )

        # Build instructions with structure guidance if provided
        instructions = task.instructions
        structure_desc = _describe_structure(task.structure)
        if structure_desc:
            instructions += structure_desc
            LOGGER.debug("Appended structure description to instructions")

        context = {
            "source": "ai_task",
            "task_name": task.name,
            "language": "en",
        }

        try:
            data = await self._api.async_query(
                query=instructions,
                context=context,
            )

            result_text = data.get("result", "")
            session_id = data.get("session_id", "")

            LOGGER.info(
                "AI Task response: session_id=%s, result_length=%d, cost_usd=%s",
                session_id,
                len(str(result_text)),
                data.get("cost_usd"),
            )

            # Try to parse as JSON for structured output
            result_data: Any = result_text
            if task.structure is not None and isinstance(result_text, str):
                try:
                    result_data = json.loads(result_text)
                    LOGGER.debug("Parsed AI Task result as JSON")
                except (json.JSONDecodeError, ValueError):
                    LOGGER.debug("AI Task result is not JSON, returning as text")

            return GenDataTaskResult(
                conversation_id=session_id or "",
                data=result_data,
            )

        except ClaudeTerminalAPIError as err:
            LOGGER.error("Claude Terminal API error during AI Task: %s (code=%s)", err, err.code)
            return GenDataTaskResult(
                conversation_id="",
                data=f"Error: {err}",
            )
        except Exception:
            LOGGER.error("Unexpected error during AI Task", exc_info=True)
            return GenDataTaskResult(
                conversation_id="",
                data="Error: unexpected failure. Check add-on logs.",
            )
