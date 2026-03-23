"""Test fixtures for Claude Terminal integration tests."""

import sys
from unittest.mock import MagicMock
import pytest

# Mock Home Assistant modules that won't be available in test environment
ha_modules = {
    "homeassistant": MagicMock(),
    "homeassistant.components": MagicMock(),
    "homeassistant.components.conversation": MagicMock(),
    "homeassistant.config_entries": MagicMock(),
    "homeassistant.const": MagicMock(),
    "homeassistant.core": MagicMock(),
    "homeassistant.helpers": MagicMock(),
    "homeassistant.helpers.device_registry": MagicMock(),
    "homeassistant.helpers.entity_platform": MagicMock(),
    "homeassistant.helpers.intent": MagicMock(),
}

# Set Platform constants
ha_modules["homeassistant.const"].Platform = MagicMock()
ha_modules["homeassistant.const"].Platform.CONVERSATION = "conversation"

for mod_name, mod_mock in ha_modules.items():
    sys.modules[mod_name] = mod_mock
