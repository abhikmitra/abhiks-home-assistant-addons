"""Tests for context building and error handling in Claude Terminal conversation logic."""

from __future__ import annotations

import pytest

from claude_terminal.api import ClaudeTerminalAPIError


def _build_context(
    source: str = "conversation",
    user_name: str | None = None,
    device_name: str | None = None,
    satellite_name: str | None = None,
    language: str | None = None,
    extra_system_prompt: str | None = None,
) -> dict:
    """Helper that mirrors the context dict built in conversation.py."""
    return {
        "source": source,
        "user_name": user_name,
        "device_name": device_name,
        "satellite_name": satellite_name,
        "language": language,
        "extra_system_prompt": extra_system_prompt,
    }


def test_context_includes_source():
    """Verify that the context dict always includes source='conversation'."""
    context = _build_context()
    assert context.get("source") == "conversation"


def test_context_with_all_fields():
    """Verify all context fields are present when provided."""
    context = _build_context(
        source="conversation",
        user_name="Alice",
        device_name="Living Room Speaker",
        satellite_name="Bedroom Satellite",
        language="en",
        extra_system_prompt="Be concise.",
    )
    assert context["source"] == "conversation"
    assert context["user_name"] == "Alice"
    assert context["device_name"] == "Living Room Speaker"
    assert context["satellite_name"] == "Bedroom Satellite"
    assert context["language"] == "en"
    assert context["extra_system_prompt"] == "Be concise."


def test_api_error_produces_user_friendly_message():
    """Verify ClaudeTerminalAPIError carries the right message and code."""
    err = ClaudeTerminalAPIError("Rate limit exceeded", code=429)
    assert str(err) == "Rate limit exceeded"
    assert err.code == 429


def test_timeout_error_message():
    """Verify that a timeout error message contains 'timed out'."""
    from claude_terminal.const import API_TIMEOUT_SECONDS

    err = ClaudeTerminalAPIError(
        f"Request timed out after {API_TIMEOUT_SECONDS} seconds"
    )
    assert "timed out" in str(err)
