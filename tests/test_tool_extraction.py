"""Tests for tool call extraction from events and trace_from_stdout.py."""

import json

import pytest

from conftest import make_assistant, make_result, make_user

from agent_eval.config import OutputConfig
from agent_eval.events import parse_stream_events

try:
    from trace_from_stdout import extract_summary
    _has_mlflow = True
except (ImportError, SystemExit):
    _has_mlflow = False


def _to_stdout_text(events):
    """Convert events to newline-separated JSON."""
    return "\n".join(json.dumps(e) for e in events)


def _extract_tool_calls_from_events(stdout_text, tool_outputs):
    """Extract tool calls from parsed events matching configured patterns."""
    events = parse_stream_events(stdout_text)
    tool_patterns = [o.tool for o in tool_outputs]
    calls = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        if event.get("parent_tool_use_id"):
            continue
        for tool in event.get("tools", []):
            name = tool.get("name", "")
            for pattern in tool_patterns:
                if pattern in name or name == pattern:
                    calls.append({"name": name, "input": tool.get("input", {})})
                    break
    return calls


class TestExtractToolCalls:
    def test_skips_subagent_messages(self):
        """Post-2.1.108: subagent tool calls are excluded."""
        events = [
            make_assistant("msg_001",
                           tools=[("Bash", "tu_001", {"command": "ls"})]),
            make_assistant("msg_sub_001",
                           parent_tool_use_id="tu_agent_x",
                           tools=[("Read", "tu_sub", {"file_path": "/sub"})]),
        ]
        tool_outputs = [OutputConfig(tool="Bash"), OutputConfig(tool="Read")]
        calls = _extract_tool_calls_from_events(
            _to_stdout_text(events), tool_outputs)
        assert len(calls) == 1
        assert calls[0]["name"] == "Bash"

    def test_pattern_matching(self):
        """Only tool calls matching configured patterns are returned."""
        events = [
            make_assistant("msg_001", tools=[
                ("Bash", "tu_001", {"command": "ls"}),
                ("Read", "tu_002", {"file_path": "/f"}),
                ("Write", "tu_003", {"file_path": "/g", "content": "x"}),
            ]),
        ]
        tool_outputs = [OutputConfig(tool="Bash")]
        calls = _extract_tool_calls_from_events(
            _to_stdout_text(events), tool_outputs)
        assert len(calls) == 1
        assert calls[0]["name"] == "Bash"


@pytest.mark.skipif(not _has_mlflow, reason="mlflow not installed")
class TestExtractSummary:
    def test_skips_subagent_tools(self):
        """Post-2.1.108: subagent tool calls excluded from summary."""
        events = [
            make_user(text="run the pipeline"),
            make_assistant("msg_001",
                           tools=[("Bash", "tu_001", {"command": "ls"})]),
            make_assistant("msg_sub_001",
                           parent_tool_use_id="tu_agent_x",
                           tools=[("Read", "tu_sub", {"file_path": "/sub"})]),
            make_result(cost_usd=0.10, num_turns=5),
        ]
        run_result = {"exit_code": 0, "duration_s": 10, "cost_usd": 0.10,
                      "num_turns": 5, "model": "claude-sonnet-4-5",
                      "token_usage": {"input": 100, "output": 50}}
        _prompt, response, intermediate = extract_summary(events, run_result)
        assert response["total_tool_calls"] == 1
        tool_names = [tc["tool"] for tc in intermediate["tool_calls"]]
        assert "Bash" in tool_names
        assert "Read" not in tool_names

    def test_pre_2_1_108_counts_all(self):
        """Pre-2.1.108: all tool calls counted (none have parent_tool_use_id)."""
        events = [
            make_user(text="run the pipeline"),
            make_assistant("msg_001",
                           tools=[("Bash", "tu_001", {"command": "ls"})]),
            make_assistant("msg_002",
                           tools=[("Read", "tu_002", {"file_path": "/f"})]),
            make_result(cost_usd=0.10, num_turns=5),
        ]
        run_result = {"exit_code": 0, "duration_s": 10, "cost_usd": 0.10,
                      "num_turns": 5, "model": "claude-sonnet-4-5",
                      "token_usage": {"input": 100, "output": 50}}
        _, response, _intermediate = extract_summary(events, run_result)
        assert response["total_tool_calls"] == 2
