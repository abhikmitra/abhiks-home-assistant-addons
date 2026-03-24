"""Tests for the Python API server."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

# Add scripts dir to path so we can import api-server
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


class TestBuildSystemPrompt:
    """Test system prompt builder."""

    def setup_method(self):
        # Import from api-server.py (has hyphen, use importlib)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "api_server",
            Path(__file__).parent.parent / "scripts" / "api-server.py",
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_conversation_prompt_includes_fields(self):
        prompt = self.mod.build_system_prompt({
            "source": "conversation",
            "user_name": "Abhik",
            "device_name": "Kitchen Speaker",
            "language": "en",
        })
        assert "conversation interface" in prompt
        assert "User: Abhik" in prompt
        assert "Kitchen Speaker" in prompt
        assert "concise and action-oriented" in prompt

    def test_ai_task_prompt(self):
        prompt = self.mod.build_system_prompt({
            "source": "ai_task",
            "task_name": "morning_briefing",
            "language": "en",
        })
        assert "AI Task interface" in prompt
        assert "Task: morning_briefing" in prompt
        assert "consumed by automations" in prompt

    def test_extra_system_prompt_appended(self):
        prompt = self.mod.build_system_prompt({
            "source": "conversation",
            "extra_system_prompt": "Only respond in Spanish",
        })
        assert "Only respond in Spanish" in prompt

    def test_empty_context(self):
        prompt = self.mod.build_system_prompt({})
        assert isinstance(prompt, str)
        assert "conversation interface" in prompt

    def test_none_context(self):
        prompt = self.mod.build_system_prompt(None)
        assert isinstance(prompt, str)

    def test_includes_current_time(self):
        prompt = self.mod.build_system_prompt({"source": "conversation"})
        # Should contain a date-like string
        assert "202" in prompt  # year


class TestRateLimiting:
    """Test rate limiting logic."""

    def setup_method(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "api_server",
            Path(__file__).parent.parent / "scripts" / "api-server.py",
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.mod.request_timestamps.clear()

    def test_not_limited_when_empty(self):
        assert self.mod.is_rate_limited() is False

    def test_limited_after_max(self):
        now = time.time()
        for _ in range(self.mod.RATE_LIMIT_MAX):
            self.mod.request_timestamps.append(now)
        assert self.mod.is_rate_limited() is True


class TestGetOAuthToken:
    """Test OAuth token resolution."""

    def setup_method(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "api_server",
            Path(__file__).parent.parent / "scripts" / "api-server.py",
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_env_var_takes_priority(self):
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}):
            assert self.mod.get_oauth_token() == "test-token"

    def test_returns_none_when_no_source(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                assert self.mod.get_oauth_token() is None


class TestScriptValidation:
    """Test script path validation."""

    def setup_method(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "api_server",
            Path(__file__).parent.parent / "scripts" / "api-server.py",
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    @pytest.mark.asyncio
    async def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid script path"):
            await self.mod.run_script("../../../etc/passwd", None, [])

    @pytest.mark.asyncio
    async def test_rejects_absolute_path(self):
        with pytest.raises(ValueError, match="Invalid script path"):
            await self.mod.run_script("/etc/passwd", None, [])

    @pytest.mark.asyncio
    async def test_rejects_non_python(self):
        with pytest.raises(ValueError, match="Invalid script path"):
            await self.mod.run_script("script.sh", None, [])

    @pytest.mark.asyncio
    async def test_rejects_missing_script(self):
        with pytest.raises(FileNotFoundError):
            await self.mod.run_script("nonexistent.py", None, [])

    @pytest.mark.asyncio
    async def test_requires_script_or_code(self):
        with pytest.raises(ValueError, match="Provide"):
            await self.mod.run_script(None, None, [])
