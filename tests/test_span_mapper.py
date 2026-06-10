"""Tests for OTel span-to-event mappers."""

import json
from pathlib import Path

import pytest

from agent_eval.otel.span_mapper import (
    ClaudeCodeSpanMapper,
    OpenCodeSpanMapper,
    get_span_mapper,
    _nano_to_iso,
    _get_attr,
)


def _attr(key, value):
    """Build an OTLP attribute entry."""
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _span(name, span_id, parent_id=None, start_ns=1000000000,
          end_ns=2000000000, attributes=None, events=None):
    """Build an OTLP span dict."""
    s = {
        "traceId": "a" * 32,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attributes or [],
        "events": events or [],
        "status": {},
    }
    if parent_id:
        s["parentSpanId"] = parent_id
    return s


def _wrap_resource_spans(*spans):
    """Wrap spans in a ResourceSpans structure."""
    return [{
        "resource": {"attributes": [_attr("service.name", "test")]},
        "scopeSpans": [{"scope": {"name": "test"}, "spans": list(spans)}],
    }]


class TestHelpers:

    def test_nano_to_iso(self):
        assert _nano_to_iso("1000000000") == "1970-01-01T00:00:01.000Z"
        assert _nano_to_iso(1000000000) == "1970-01-01T00:00:01.000Z"
        assert _nano_to_iso(None) == ""
        assert _nano_to_iso("invalid") == ""

    def test_get_attr(self):
        attrs = [
            _attr("model", "opus"),
            _attr("tokens", 100),
            _attr("enabled", True),
        ]
        assert _get_attr(attrs, "model") == "opus"
        assert _get_attr(attrs, "tokens") == 100
        assert _get_attr(attrs, "enabled") is True
        assert _get_attr(attrs, "missing") is None
        assert _get_attr(attrs, "missing", "default") == "default"
        assert _get_attr(None, "model") is None


class TestClaudeCodeSpanMapper:

    def test_empty_spans(self):
        mapper = ClaudeCodeSpanMapper()
        assert mapper.map_spans([]) == []

    def test_system_init_event(self):
        """First interaction span produces a system/init event."""
        mapper = ClaudeCodeSpanMapper()
        spans = _wrap_resource_spans(
            _span("claude_code.interaction", "s1", start_ns=1000000000),
            _span("claude_code.llm_request", "s2", parent_id="s1",
                   start_ns=1100000000,
                   attributes=[_attr("model", "claude-opus-4-6")]),
        )
        events = mapper.map_spans(spans)
        init = [e for e in events if e["type"] == "system"]
        assert len(init) == 1
        assert init[0]["subtype"] == "init"
        assert init[0]["model"] == "claude-opus-4-6"

    def test_tool_span_produces_two_events(self):
        """A tool span produces an assistant event (tool_use) and a tool_result."""
        mapper = ClaudeCodeSpanMapper()
        tool_output_event = {
            "name": "tool.output",
            "attributes": [
                _attr("tool.input", json.dumps({"file_path": "/test.py"})),
                _attr("tool.output", "file content here"),
            ],
        }
        spans = _wrap_resource_spans(
            _span("claude_code.interaction", "s1"),
            _span("claude_code.tool", "s2", parent_id="s1",
                   start_ns=1100000000, end_ns=1200000000,
                   attributes=[_attr("tool_name", "Read")],
                   events=[tool_output_event]),
        )
        events = mapper.map_spans(spans)
        assistants = [e for e in events if e["type"] == "assistant" and e.get("tools")]
        results = [e for e in events if e["type"] == "tool_result"]

        assert len(assistants) == 1
        assert assistants[0]["tools"][0]["name"] == "Read"
        assert assistants[0]["tools"][0]["id"] == "s2"

        assert len(results) == 1
        assert results[0]["tool_use_id"] == "s2"
        assert results[0]["tool_name"] == "Read"
        assert results[0]["content"] == "file content here"
        assert results[0]["is_error"] is False

    def test_tool_error_detection(self):
        """A failed tool execution sets is_error=True on the result."""
        mapper = ClaudeCodeSpanMapper()
        spans = _wrap_resource_spans(
            _span("claude_code.interaction", "s1"),
            _span("claude_code.tool", "s2", parent_id="s1",
                   attributes=[_attr("tool_name", "Bash")]),
            _span("claude_code.tool.execution", "s3", parent_id="s2",
                   attributes=[_attr("success", False)]),
        )
        events = mapper.map_spans(spans)
        results = [e for e in events if e["type"] == "tool_result"]
        assert results[0]["is_error"] is True

    def test_subagent_spans_tagged(self):
        """Spans with agent_id get parent_tool_use_id tags."""
        mapper = ClaudeCodeSpanMapper()
        spans = _wrap_resource_spans(
            _span("claude_code.interaction", "s1"),
            _span("claude_code.tool", "s2", parent_id="s1",
                   attributes=[
                       _attr("tool_name", "Agent"),
                       _attr("agent_id", "agent-1"),
                       _attr("parent_agent_id", "main"),
                   ]),
        )
        events = mapper.map_spans(spans)
        tool_events = [e for e in events if e.get("agent_id")]
        assert len(tool_events) >= 1
        assert tool_events[0]["agent_id"] == "agent-1"

    def test_result_event_appended(self):
        """A synthetic result event is always appended."""
        mapper = ClaudeCodeSpanMapper()
        spans = _wrap_resource_spans(
            _span("claude_code.interaction", "s1"),
        )
        events = mapper.map_spans(spans)
        results = [e for e in events if e["type"] == "result"]
        assert len(results) == 1

    def test_extract_usage(self):
        """Token usage is aggregated from llm_request spans."""
        mapper = ClaudeCodeSpanMapper()
        spans = _wrap_resource_spans(
            _span("claude_code.llm_request", "s1",
                   attributes=[
                       _attr("model", "claude-opus-4-6"),
                       _attr("input_tokens", 500),
                       _attr("output_tokens", 200),
                       _attr("cache_read_tokens", 100),
                       _attr("cache_creation_tokens", 50),
                       _attr("gen_ai.response.id", "msg-1"),
                   ]),
            _span("claude_code.llm_request", "s2",
                   start_ns=2000000000,
                   attributes=[
                       _attr("model", "claude-opus-4-6"),
                       _attr("input_tokens", 300),
                       _attr("output_tokens", 100),
                       _attr("gen_ai.response.id", "msg-2"),
                   ]),
        )
        usage = mapper.extract_usage(spans)
        assert usage["token_usage"]["input"] == 800
        assert usage["token_usage"]["output"] == 300
        assert usage["token_usage"]["cache_read"] == 100
        assert usage["num_turns"] == 2
        assert usage["resolved_model"] == "claude-opus-4-6"
        assert usage["models_used"] == ["claude-opus-4-6"]
        assert usage["per_model_turns"]["claude-opus-4-6"] == 2

    def test_api_bodies_integration(self, tmp_path):
        """Assistant text is extracted from API body response files."""
        mapper = ClaudeCodeSpanMapper()
        bodies_dir = tmp_path / "bodies"
        bodies_dir.mkdir()
        (bodies_dir / "001_response.json").write_text(json.dumps({
            "id": "req-123",
            "content": [
                {"type": "text", "text": "Hello from Claude"},
                {"type": "tool_use", "name": "Read", "id": "tu1"},
            ],
        }))

        spans = _wrap_resource_spans(
            _span("claude_code.interaction", "s1"),
            _span("claude_code.llm_request", "s2", parent_id="s1",
                   attributes=[
                       _attr("request_id", "req-123"),
                       _attr("model", "opus"),
                       _attr("output_tokens", 50),
                   ]),
        )
        events = mapper.map_spans(spans, api_bodies_dir=bodies_dir)
        assistant_events = [e for e in events if e["type"] == "assistant"]
        assert any("Hello from Claude" in e.get("text", "") for e in assistant_events)


class TestOpenCodeSpanMapper:

    def test_empty_spans(self):
        mapper = OpenCodeSpanMapper()
        assert mapper.map_spans([]) == []

    def test_stream_text_span(self):
        mapper = OpenCodeSpanMapper()
        spans = _wrap_resource_spans(
            _span("ai.streamText", "s1",
                   attributes=[
                       _attr("gen_ai.request.model", "claude-sonnet-4-6"),
                       _attr("gen_ai.completion", "Here is the answer."),
                       _attr("gen_ai.usage.input_tokens", 100),
                       _attr("gen_ai.usage.output_tokens", 50),
                   ]),
        )
        events = mapper.map_spans(spans)
        init = [e for e in events if e["type"] == "system"]
        assert init[0]["model"] == "claude-sonnet-4-6"

        assistants = [e for e in events if e["type"] == "assistant"]
        assert assistants[0]["text"] == "Here is the answer."

    def test_tool_call_span(self):
        mapper = OpenCodeSpanMapper()
        spans = _wrap_resource_spans(
            _span("ai.toolCall", "s1",
                   start_ns=1000000000, end_ns=2000000000,
                   attributes=[
                       _attr("ai.toolCall.name", "bash"),
                       _attr("ai.toolCall.id", "tc-1"),
                       _attr("ai.toolCall.args", json.dumps({"command": "ls"})),
                       _attr("ai.toolCall.result", "file1.txt\nfile2.txt"),
                   ]),
        )
        events = mapper.map_spans(spans)
        tool_events = [e for e in events if e["type"] == "assistant" and e.get("tools")]
        assert tool_events[0]["tools"][0]["name"] == "bash"
        assert tool_events[0]["tools"][0]["input"] == {"command": "ls"}

        results = [e for e in events if e["type"] == "tool_result"]
        assert results[0]["tool_name"] == "bash"
        assert results[0]["content"] == "file1.txt\nfile2.txt"

    def test_extract_usage(self):
        mapper = OpenCodeSpanMapper()
        spans = _wrap_resource_spans(
            _span("ai.streamText", "s1",
                   attributes=[
                       _attr("gen_ai.request.model", "claude-sonnet-4-6"),
                       _attr("gen_ai.usage.input_tokens", 200),
                       _attr("gen_ai.usage.output_tokens", 100),
                   ]),
        )
        usage = mapper.extract_usage(spans)
        assert usage["token_usage"]["input"] == 200
        assert usage["token_usage"]["output"] == 100
        assert usage["num_turns"] == 1
        assert usage["resolved_model"] == "claude-sonnet-4-6"


class TestSpanMapperRegistry:

    def test_get_claude_code_mapper(self):
        mapper = get_span_mapper("claude-code")
        assert isinstance(mapper, ClaudeCodeSpanMapper)

    def test_get_opencode_mapper(self):
        mapper = get_span_mapper("opencode")
        assert isinstance(mapper, OpenCodeSpanMapper)

    def test_unknown_type_defaults_to_claude(self):
        mapper = get_span_mapper("unknown")
        assert isinstance(mapper, ClaudeCodeSpanMapper)
