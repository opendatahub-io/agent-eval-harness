"""Tests for documentation tracking (Read tool call extraction)."""

import json
import tempfile
from pathlib import Path

import pytest
import yaml

# Import events module
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_eval.events import extract_read_calls, parse_stream_events
from agent_eval.config import EvalConfig


class TestExtractReadCalls:
    """Test extraction of Read tool calls from events."""

    def test_extract_single_read_call(self):
        """Test extracting a single Read tool call."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:00:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_1",
                        "input": {
                            "file_path": "/path/to/file.md"
                        }
                    }
                ]
            }
        ]

        read_calls = extract_read_calls(events)

        assert len(read_calls) == 1
        assert read_calls[0]["file_path"] == "/path/to/file.md"
        assert read_calls[0]["timestamp"] == "2026-05-21T10:00:00Z"
        assert read_calls[0]["offset"] is None
        assert read_calls[0]["limit"] is None
        assert read_calls[0]["pages"] is None

    def test_extract_read_call_with_offset_and_limit(self):
        """Test extracting Read call with offset and limit parameters."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:01:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_2",
                        "input": {
                            "file_path": "/path/to/large-file.txt",
                            "offset": 100,
                            "limit": 50
                        }
                    }
                ]
            }
        ]

        read_calls = extract_read_calls(events)

        assert len(read_calls) == 1
        assert read_calls[0]["file_path"] == "/path/to/large-file.txt"
        assert read_calls[0]["offset"] == 100
        assert read_calls[0]["limit"] == 50

    def test_extract_read_call_with_pages(self):
        """Test extracting Read call with pages parameter (PDF)."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:02:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_3",
                        "input": {
                            "file_path": "/path/to/document.pdf",
                            "pages": "1-5"
                        }
                    }
                ]
            }
        ]

        read_calls = extract_read_calls(events)

        assert len(read_calls) == 1
        assert read_calls[0]["file_path"] == "/path/to/document.pdf"
        assert read_calls[0]["pages"] == "1-5"

    def test_extract_multiple_read_calls(self):
        """Test extracting multiple Read calls from multiple events."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:00:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_1",
                        "input": {"file_path": "/path/to/CLAUDE.md"}
                    }
                ]
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:01:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_2",
                        "input": {"file_path": "/path/to/ai-docs/workflow.md"}
                    }
                ]
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:02:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_3",
                        "input": {"file_path": "/path/to/README.md"}
                    }
                ]
            }
        ]

        read_calls = extract_read_calls(events)

        assert len(read_calls) == 3
        assert read_calls[0]["file_path"] == "/path/to/CLAUDE.md"
        assert read_calls[1]["file_path"] == "/path/to/ai-docs/workflow.md"
        assert read_calls[2]["file_path"] == "/path/to/README.md"

    def test_filter_non_read_tool_calls(self):
        """Test that only Read tool calls are extracted."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:00:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_1",
                        "input": {"file_path": "/path/to/file.md"}
                    },
                    {
                        "name": "Write",
                        "id": "tool_2",
                        "input": {"file_path": "/path/to/output.txt", "content": "test"}
                    },
                    {
                        "name": "Bash",
                        "id": "tool_3",
                        "input": {"command": "ls -la"}
                    }
                ]
            }
        ]

        read_calls = extract_read_calls(events)

        assert len(read_calls) == 1
        assert read_calls[0]["file_path"] == "/path/to/file.md"

    def test_skip_subagent_read_calls(self):
        """Test that Read calls from subagents are excluded."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:00:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_1",
                        "input": {"file_path": "/path/to/main-file.md"}
                    }
                ]
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:01:00Z",
                "parent_tool_use_id": "agent_1",  # Subagent call
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_2",
                        "input": {"file_path": "/path/to/subagent-file.md"}
                    }
                ]
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:02:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_3",
                        "input": {"file_path": "/path/to/another-main-file.md"}
                    }
                ]
            }
        ]

        read_calls = extract_read_calls(events)

        # Should only have the 2 main-level Read calls, not the subagent one
        assert len(read_calls) == 2
        assert read_calls[0]["file_path"] == "/path/to/main-file.md"
        assert read_calls[1]["file_path"] == "/path/to/another-main-file.md"

    def test_ignore_read_calls_without_file_path(self):
        """Test that Read calls without file_path are ignored."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:00:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_1",
                        "input": {}  # Missing file_path
                    }
                ]
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:01:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_2",
                        "input": {"file_path": ""}  # Empty file_path
                    }
                ]
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:02:00Z",
                "tools": [
                    {
                        "name": "Read",
                        "id": "tool_3",
                        "input": {"file_path": "/valid/path.md"}
                    }
                ]
            }
        ]

        read_calls = extract_read_calls(events)

        assert len(read_calls) == 1
        assert read_calls[0]["file_path"] == "/valid/path.md"

    def test_empty_events_list(self):
        """Test that empty events list returns empty read_calls."""
        read_calls = extract_read_calls([])
        assert read_calls == []

    def test_events_with_no_read_calls(self):
        """Test events with no Read calls."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:00:00Z",
                "tools": [
                    {
                        "name": "Write",
                        "id": "tool_1",
                        "input": {"file_path": "/path/to/output.txt", "content": "test"}
                    }
                ]
            },
            {
                "type": "user",
                "timestamp": "2026-05-21T10:01:00Z",
                "text": "Continue"
            }
        ]

        read_calls = extract_read_calls(events)
        assert read_calls == []

    def test_assistant_event_without_tools(self):
        """Test assistant event with no tools."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:00:00Z",
                "text": "I understand the task.",
                "tools": []
            }
        ]

        read_calls = extract_read_calls(events)
        assert read_calls == []


class TestDocumentationTrackingConfig:
    """Test config validation for documentation tracking."""

    def test_documentation_tracking_requires_events(self):
        """Test that documentation_tracking requires events to be enabled."""
        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {"path": "eval/dataset", "schema": "test"},
            "outputs": [{"path": "output", "schema": "test"}],
            "traces": {
                "events": False,
                "documentation_tracking": True
            }
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            with pytest.raises(ValueError, match="documentation_tracking requires traces.events"):
                EvalConfig.from_yaml(config_path)
        finally:
            Path(config_path).unlink()

    def test_documentation_tracking_with_events_enabled(self):
        """Test that documentation_tracking works when events are enabled."""
        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {"path": "eval/dataset", "schema": "test"},
            "outputs": [{"path": "output", "schema": "test"}],
            "traces": {
                "events": True,
                "documentation_tracking": True
            }
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)
            assert config.traces.events is True
            assert config.traces.documentation_tracking is True
        finally:
            Path(config_path).unlink()

    def test_documentation_tracking_default_disabled(self):
        """Test that documentation_tracking defaults to False."""
        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case"},
            "skill": "test-skill",
            "dataset": {"path": "eval/dataset", "schema": "test"},
            "outputs": [{"path": "output", "schema": "test"}],
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)
            assert config.traces.documentation_tracking is False
        finally:
            Path(config_path).unlink()


class TestEndToEndDocumentationTracking:
    """Test end-to-end documentation tracking from stream-json to score.py."""

    def test_parse_and_extract_from_stream_json(self):
        """Test parsing stream-json and extracting Read calls."""
        # Simulate stream-json output with Read tool calls
        stream_json = '''{"type":"assistant","timestamp":"2026-05-21T10:00:00Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"tool_1","name":"Read","input":{"file_path":"/path/to/CLAUDE.md"}}]}}
{"type":"user","timestamp":"2026-05-21T10:00:01Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tool_1","content":"# Project Documentation"}]}}
{"type":"assistant","timestamp":"2026-05-21T10:00:02Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"tool_2","name":"Read","input":{"file_path":"/path/to/ai-docs/workflow.md","offset":0,"limit":100}}]}}
{"type":"user","timestamp":"2026-05-21T10:00:03Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tool_2","content":"# Workflow"}]}}
{"type":"assistant","timestamp":"2026-05-21T10:00:04Z","message":{"role":"assistant","content":[{"type":"text","text":"I found the documentation."}]}}
'''

        events = parse_stream_events(stream_json)
        read_calls = extract_read_calls(events)

        assert len(read_calls) == 2
        assert read_calls[0]["file_path"] == "/path/to/CLAUDE.md"
        assert read_calls[0]["offset"] is None
        assert read_calls[1]["file_path"] == "/path/to/ai-docs/workflow.md"
        assert read_calls[1]["offset"] == 0
        assert read_calls[1]["limit"] == 100

    def test_read_calls_chronological_order(self):
        """Test that Read calls are returned in chronological order."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:00:00Z",
                "tools": [
                    {"name": "Read", "id": "1", "input": {"file_path": "/first.md"}}
                ]
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:01:00Z",
                "tools": [
                    {"name": "Read", "id": "2", "input": {"file_path": "/second.md"}}
                ]
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-21T10:02:00Z",
                "tools": [
                    {"name": "Read", "id": "3", "input": {"file_path": "/third.md"}}
                ]
            }
        ]

        read_calls = extract_read_calls(events)

        assert len(read_calls) == 3
        assert read_calls[0]["file_path"] == "/first.md"
        assert read_calls[0]["timestamp"] == "2026-05-21T10:00:00Z"
        assert read_calls[1]["file_path"] == "/second.md"
        assert read_calls[1]["timestamp"] == "2026-05-21T10:01:00Z"
        assert read_calls[2]["file_path"] == "/third.md"
        assert read_calls[2]["timestamp"] == "2026-05-21T10:02:00Z"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
