"""Tests for ClaudeTerminalAPI HTTP client."""

from __future__ import annotations

import pytest
import pytest_asyncio
import aiohttp
from aioresponses import aioresponses

from claude_terminal.api import ClaudeTerminalAPI, ClaudeTerminalAPIError


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def api_client():
    session = aiohttp.ClientSession()
    client = ClaudeTerminalAPI(session, hostname="localhost", port=8099)
    yield client
    await session.close()


@pytest.mark.asyncio
async def test_health_check_success(api_client):
    """Test that a 200 response from the health endpoint returns True."""
    with aioresponses() as m:
        m.get(
            "http://localhost:8099/api/health",
            status=200,
            payload={"status": "ok", "busy": False},
        )
        result = await api_client.async_check_health()
    assert result is True


@pytest.mark.asyncio
async def test_health_check_failure(api_client):
    """Test that a 500 response from the health endpoint returns False."""
    with aioresponses() as m:
        m.get(
            "http://localhost:8099/api/health",
            status=500,
            payload={"status": "error"},
        )
        result = await api_client.async_check_health()
    assert result is False


@pytest.mark.asyncio
async def test_health_check_connection_error(api_client):
    """Test that a ClientError during health check returns False."""
    with aioresponses() as m:
        m.get(
            "http://localhost:8099/api/health",
            exception=aiohttp.ClientConnectionError("Connection refused"),
        )
        result = await api_client.async_check_health()
    assert result is False


@pytest.mark.asyncio
async def test_query_success(api_client):
    """Test that a successful query returns all expected fields."""
    with aioresponses() as m:
        m.post(
            "http://localhost:8099/api/query",
            status=200,
            payload={
                "result": "Hello! How can I help?",
                "session_id": "sess-abc123",
                "cost_usd": 0.0025,
            },
        )
        data = await api_client.async_query(
            query="Hello",
            context={"source": "conversation"},
        )
    assert data["result"] == "Hello! How can I help?"
    assert data["session_id"] == "sess-abc123"
    assert data["cost_usd"] == 0.0025


@pytest.mark.asyncio
async def test_query_with_conversation_id(api_client):
    """Test that conversation_id is forwarded in the request payload."""
    with aioresponses() as m:
        m.post(
            "http://localhost:8099/api/query",
            status=200,
            payload={
                "result": "Continuing conversation...",
                "session_id": "sess-xyz789",
                "cost_usd": 0.001,
            },
        )
        data = await api_client.async_query(
            query="Follow-up question",
            context={"source": "conversation"},
            conversation_id="sess-xyz789",
        )
    assert data["session_id"] == "sess-xyz789"
    assert data["result"] == "Continuing conversation..."


@pytest.mark.asyncio
async def test_query_rate_limited(api_client):
    """Test that a 429 response raises ClaudeTerminalAPIError with code 429."""
    with aioresponses() as m:
        m.post(
            "http://localhost:8099/api/query",
            status=429,
            payload={"message": "Rate limit exceeded", "code": 429},
        )
        with pytest.raises(ClaudeTerminalAPIError) as exc_info:
            await api_client.async_query(query="Hello")
    assert exc_info.value.code == 429


@pytest.mark.asyncio
async def test_query_server_busy(api_client):
    """Test that a 503 response raises ClaudeTerminalAPIError with code 503."""
    with aioresponses() as m:
        m.post(
            "http://localhost:8099/api/query",
            status=503,
            payload={"message": "Server busy", "code": 503},
        )
        with pytest.raises(ClaudeTerminalAPIError) as exc_info:
            await api_client.async_query(query="Hello")
    assert exc_info.value.code == 503


@pytest.mark.asyncio
async def test_query_connection_error(api_client):
    """Test that a ClientError during query raises ClaudeTerminalAPIError with 'Cannot connect'."""
    with aioresponses() as m:
        m.post(
            "http://localhost:8099/api/query",
            exception=aiohttp.ClientConnectionError("Connection refused"),
        )
        with pytest.raises(ClaudeTerminalAPIError) as exc_info:
            await api_client.async_query(query="Hello")
    assert "Cannot connect" in str(exc_info.value)
