# AI Task Entity for Claude Terminal

## Overview

Add an AI Task entity to the Claude Terminal custom integration so automations can call `ai_task.generate_data` targeting Claude.

## Architecture

Routes through the existing `/api/query` endpoint with `source: "ai_task"` context. No changes to the API server.

```
ai_task.generate_data service call
  → ClaudeTerminalAITaskEntity._async_generate_data(task, chat_log)
  → ClaudeTerminalAPI.async_query(instructions, context={source: "ai_task", task_name: ...})
  → POST http://{hostname}:8099/api/query
  → api-server.py run_agent_query() (Agent SDK)
  → GenDataTaskResult(conversation_id=session_id, data=response)
```

## Changes

### New file: `custom_components/claude_terminal/ai_task.py`

- Subclasses `AITaskEntity` from `homeassistant.components.ai_task`
- Sets `_attr_supported_features = AITaskEntityFeature.GENERATE_DATA`
- Implements `_async_generate_data(task, chat_log) -> GenDataTaskResult`
- Sends `task.instructions` to the API server via `ClaudeTerminalAPI.async_query()`
- Context: `{"source": "ai_task", "task_name": task.name, "language": "en"}`
- If `task.structure` is provided, includes the structure field descriptions in the instructions text and asks Claude to return JSON matching it
- Returns `GenDataTaskResult(conversation_id=session_id, data=parsed_or_text)`
- ChatLog is intentionally not used (same design choice as conversation entity)

### Modified file: `custom_components/claude_terminal/__init__.py`

- Add AI Task platform registration. Use try/except to handle older HA versions that don't have `Platform.AI_TASK`:
  ```python
  PLATFORMS = [Platform.CONVERSATION]
  try:
      from homeassistant.components.ai_task import AITaskEntityFeature  # noqa: F401
      PLATFORMS.append("ai_task")
  except ImportError:
      pass
  ```

### No changes to:

- `api-server.py` — already handles `source: "ai_task"` in system prompt builder
- `api.py` — already supports `json_schema` parameter
- `conversation.py` — independent platform
- `const.py` — no new constants needed

## Structure handling

`GenDataTask.structure` is a `vol.Schema` built from HA's selector system. Converting voluptuous to JSON Schema is complex. Instead:

1. If `task.structure` is provided, extract field names and descriptions from it
2. Append them to the instructions: "Return a JSON object with these fields: ..."
3. Parse Claude's response as JSON
4. Validate against the schema; if validation fails, return raw text

## Usage

```yaml
action: ai_task.generate_data
target:
  entity_id: ai_task.claude_terminal
data:
  task_name: "morning_briefing"
  instructions: "What lights are on and what's the temperature?"
```

With structure:
```yaml
action: ai_task.generate_data
target:
  entity_id: ai_task.claude_terminal
data:
  task_name: "light_check"
  instructions: "Which lights are currently on?"
  structure:
    lights:
      selector:
        text:
      description: "Comma-separated list of light names"
      required: true
    count:
      selector:
        number:
          min: 0
      description: "Number of lights on"
      required: true
```

## Verified against HA source

- `AITaskEntity` source: `homeassistant/components/ai_task/entity.py`
- `GenDataTask` fields: `name`, `instructions`, `structure`, `attachments`, `llm_api`
- `GenDataTaskResult` fields: `conversation_id`, `data`
- `AITaskEntityFeature.GENERATE_DATA = 1`
- `DEFAULT_SYSTEM_PROMPT = "You are a Home Assistant expert and help users with their tasks."`
- Platform setup pattern: same as conversation — `async_setup_entry` with `AddEntitiesCallback`
