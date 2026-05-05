"""Tests for {{ stdout }} template variable and _extract_assistant_text() helper."""

import json
from unittest.mock import MagicMock, patch

from conftest import make_assistant, make_result, make_system_init, make_user

from score import _extract_assistant_text, _make_anthropic_llm_judge


def _jsonl(*events):
    return "\n".join(json.dumps(e) for e in events)


ASSISTANT_EVENT = make_assistant("msg_001", text="Hello from the skill")
MULTI_BLOCK_EVENT = {
    "type": "assistant",
    "message": {
        "content": [
            {"type": "text", "text": "First block"},
            {"type": "tool_use", "id": "tu_001", "name": "Write", "input": {}},
            {"type": "text", "text": "Second block"},
        ]
    },
}
SUBAGENT_EVENT = make_assistant("msg_sub", text="Subagent output",
                                parent_tool_use_id="toolu_abc123")
USER_EVENT = make_user(text="user input")
SYSTEM_EVENT = make_system_init()
RESULT_EVENT = make_result(cost_usd=0.05, num_turns=1)


# --- T002: _extract_assistant_text with valid JSONL ---

class TestExtractAssistantTextJSONL:
    def test_single_assistant_event(self):
        stdout = _jsonl(ASSISTANT_EVENT)
        result = _extract_assistant_text(stdout)
        assert result == "Hello from the skill"

    def test_multiple_assistant_events(self):
        event2 = make_assistant("msg_002", text="Second message")
        stdout = _jsonl(ASSISTANT_EVENT, event2)
        result = _extract_assistant_text(stdout)
        assert result == "Hello from the skill\nSecond message"

    def test_multi_block_event(self):
        stdout = _jsonl(MULTI_BLOCK_EVENT)
        result = _extract_assistant_text(stdout)
        assert result == "First block\nSecond block"

    def test_filters_non_assistant_events(self):
        stdout = _jsonl(USER_EVENT, ASSISTANT_EVENT, SYSTEM_EVENT, RESULT_EVENT)
        result = _extract_assistant_text(stdout)
        assert result == "Hello from the skill"

    def test_filters_subagent_events(self):
        stdout = _jsonl(ASSISTANT_EVENT, SUBAGENT_EVENT)
        result = _extract_assistant_text(stdout)
        assert result == "Hello from the skill"


# --- T003: {{ stdout }} rendering in _make_anthropic_llm_judge ---

class TestStdoutTemplateRendering:
    def _make_mock_client(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 4, "rationale": "good"}')]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        return mock_client

    @patch("score._get_anthropic_client")
    def test_stdout_variable_renders_extracted_text(self, mock_get_client):
        mock_client = self._make_mock_client()
        mock_get_client.return_value = mock_client

        prompt = "Evaluate this output:\n{{ stdout }}\nScore 1-5."
        scorer = _make_anthropic_llm_judge("test", prompt, "test-model")

        stdout = _jsonl(ASSISTANT_EVENT)
        scorer(outputs={"stdout": stdout, "files": {}})

        call_args = mock_client.messages.create.call_args
        rendered = call_args.kwargs["messages"][0]["content"]
        assert "Hello from the skill" in rendered
        assert "{{ stdout }}" not in rendered

    @patch("score._get_anthropic_client")
    def test_stdout_variable_not_present_leaves_prompt_unchanged(self, mock_get_client):
        mock_client = self._make_mock_client()
        mock_get_client.return_value = mock_client

        prompt = "Evaluate:\n{{ outputs }}\nScore 1-5."
        scorer = _make_anthropic_llm_judge("test", prompt, "test-model")

        scorer(outputs={"stdout": "some data", "files": {"f.md": "content"}})

        call_args = mock_client.messages.create.call_args
        rendered = call_args.kwargs["messages"][0]["content"]
        assert "{{ stdout }}" not in rendered
        assert "{{ outputs }}" not in rendered
        assert "content" in rendered
        assert "some data" not in rendered

    @patch("score._get_anthropic_client")
    def test_stdout_with_none_outputs(self, mock_get_client):
        mock_client = self._make_mock_client()
        mock_get_client.return_value = mock_client

        prompt = "Evaluate:\n{{ stdout }}\nScore 1-5."
        scorer = _make_anthropic_llm_judge("test", prompt, "test-model")

        scorer(outputs=None)

        call_args = mock_client.messages.create.call_args
        rendered = call_args.kwargs["messages"][0]["content"]
        assert "{{ stdout }}" in rendered


# --- T007: Mixed {{ outputs }} and {{ stdout }} ---

class TestMixedTemplateVariables:
    @patch("score._get_anthropic_client")
    def test_both_variables_resolve_independently(self, mock_get_client):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 5, "rationale": "excellent"}')]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        prompt = "Files:\n{{ outputs }}\n\nConversation:\n{{ stdout }}\n\nScore 1-5."
        scorer = _make_anthropic_llm_judge("test", prompt, "test-model")

        stdout = _jsonl(ASSISTANT_EVENT)
        outputs = {
            "stdout": stdout,
            "files": {"output/result.md": "# Result\nSome content"},
        }
        scorer(outputs=outputs)

        call_args = mock_client.messages.create.call_args
        rendered = call_args.kwargs["messages"][0]["content"]
        assert "Hello from the skill" in rendered
        assert "# Result" in rendered
        assert "{{ stdout }}" not in rendered
        assert "{{ outputs }}" not in rendered


# --- T008: Empty/missing stdout ---

class TestExtractAssistantTextEmpty:
    def test_empty_string(self):
        assert _extract_assistant_text("") == "(no stdout captured)"

    def test_none(self):
        assert _extract_assistant_text(None) == "(no stdout captured)"

    def test_whitespace_only(self):
        assert _extract_assistant_text("   \n  \n  ") == "(no stdout captured)"


# --- T009: Plain text (non-JSONL) stdout ---

class TestExtractAssistantTextPlainText:
    def test_plain_text_falls_back_to_raw(self):
        plain = "This is just regular output\nfrom a non-Claude runner"
        result = _extract_assistant_text(plain)
        assert result == plain

    def test_mixed_json_and_text(self):
        mixed = "Some log line\n" + json.dumps(ASSISTANT_EVENT) + "\nAnother log"
        result = _extract_assistant_text(mixed)
        assert result == "Hello from the skill"


# --- T010: Subagent-only JSONL ---

class TestExtractAssistantTextSubagentOnly:
    def test_subagent_only_returns_placeholder(self):
        stdout = _jsonl(SUBAGENT_EVENT, USER_EVENT, RESULT_EVENT)
        result = _extract_assistant_text(stdout)
        assert result == "(no top-level assistant text)"

    def test_jsonl_with_no_assistant_events_returns_placeholder(self):
        stdout = _jsonl(USER_EVENT, SYSTEM_EVENT, RESULT_EVENT)
        result = _extract_assistant_text(stdout)
        assert result == "(no top-level assistant text)"
