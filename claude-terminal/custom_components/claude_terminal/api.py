"""HTTP client for Claude Terminal add-on API."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .const import (
    API_HEALTH_PATH,
    API_QUERY_PATH,
    API_TIMEOUT_SECONDS,
    DEFAULT_ADDON_HOSTNAME,
    DEFAULT_ADDON_PORT,
    LOGGER,
)


class ClaudeTerminalAPIError(Exception):
    """Error communicating with Claude Terminal API."""

    def __init__(self, message: str, code: int = 500) -> None:
        """Initialize."""
        super().__init__(message)
        self.code = code


class ClaudeTerminalAPI:
    """Client for the Claude Terminal add-on API server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        hostname: str = DEFAULT_ADDON_HOSTNAME,
        port: int = DEFAULT_ADDON_PORT,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._base_url = f"http://{hostname}:{port}"
        LOGGER.debug(
            "Claude Terminal API client initialized, base_url=%s", self._base_url
        )

    async def async_check_health(self) -> bool:
        """Check if the API server is healthy."""
        url = f"{self._base_url}{API_HEALTH_PATH}"
        LOGGER.debug("Checking API health at %s", url)
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    LOGGER.debug("Health check OK, busy=%s", data.get("busy"))
                    return True
                LOGGER.warning("Health check failed, status=%s", resp.status)
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            LOGGER.warning("Health check error: %s", err)
            return False

    async def async_query(
        self,
        query: str,
        context: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        json_schema: dict | None = None,
    ) -> dict[str, Any]:
        """Send a query to the Claude Terminal API server."""
        url = f"{self._base_url}{API_QUERY_PATH}"
        payload: dict[str, Any] = {"query": query}
        if context:
            payload["context"] = context
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if json_schema:
            payload["json_schema"] = json_schema

        LOGGER.info(
            "Sending query to Claude Terminal API: source=%s, query_length=%d, has_conversation_id=%s",
            context.get("source", "unknown") if context else "unknown",
            len(query),
            bool(conversation_id),
        )

        try:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
            async with self._session.post(url, json=payload, timeout=timeout) as resp:
                try:
                    data = await resp.json()
                except (aiohttp.ContentTypeError, ValueError) as parse_err:
                    LOGGER.error(
                        "Claude Terminal API returned non-JSON response: status=%s, error=%s",
                        resp.status, parse_err,
                    )
                    raise ClaudeTerminalAPIError(
                        f"API returned non-JSON response (HTTP {resp.status})", resp.status
                    ) from parse_err

                if resp.status != 200:
                    error_msg = data.get("message", f"HTTP {resp.status}")
                    error_code = data.get("code", resp.status)
                    LOGGER.error(
                        "Claude Terminal API error: status=%s, message=%s, code=%s",
                        resp.status, error_msg, error_code,
                    )
                    raise ClaudeTerminalAPIError(error_msg, error_code)

                LOGGER.info(
                    "Claude Terminal API response: session_id=%s, result_length=%d, cost_usd=%s",
                    data.get("session_id"),
                    len(str(data.get("result", ""))),
                    data.get("cost_usd"),
                )
                return data

        except aiohttp.ClientError as err:
            LOGGER.error("Failed to connect to Claude Terminal API: %s", err)
            raise ClaudeTerminalAPIError(
                f"Cannot connect to Claude Terminal add-on: {err}"
            ) from err
        except asyncio.TimeoutError:
            LOGGER.error(
                "Claude Terminal API request timed out after %ds", API_TIMEOUT_SECONDS
            )
            raise ClaudeTerminalAPIError(
                f"Request timed out after {API_TIMEOUT_SECONDS} seconds"
            ) from None
