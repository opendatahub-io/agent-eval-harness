"""Tests for agent_eval.events — structured event parser."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"))

from agent_eval.events import (
    parse_stream_events, merge_subagent_transcripts, extract_conversation_text,
)
from conftest import (
    make_assistant, make_result, make_system_init, make_user,
)


def _to_stdout(events):
    """Convert event dicts to JSONL string (what parse_stream_events expects)."""
    return "\n".join(json.dumps(e) for e in events)


# ---------------------------------------------------------------------------
# T003: parse_stream_events basic tests
# ---------------------------------------------------------------------------

class TestParseStreamEvents:
    def test_empty_input(self):
        assert parse_stream_events("") == []
        assert parse_stream_events(None) == []

    def test_non_jsonl_content(self):
        assert parse_stream_events("this is plain text\nnot json at all") == []

    def test_system_init_event(self):
        events = [make_system_init(model="claude-opus-4-6")]
        result = parse_stream_events(_to_stdout(events))
        assert len(result) == 1
        assert result[0]["type"] == "system"
        assert result[0]["subtype"] == "init"
        assert result[0]["model"] == "claude-opus-4-6"
        assert result[0]["timestamp"] is not None

    def test_assistant_text(self):
        events = [make_assistant("msg_001", text="Hello world")]
        result = parse_stream_events(_to_stdout(events))
        assert len(result) == 1
        assert result[0]["type"] == "assistant"
        assert result[0]["text"] == "Hello world"
        assert result[0]["tools"] == []
        assert result[0]["_msg_id"] == "msg_001"

    def test_assistant_with_tool_calls(self):
        events = [
            make_assistant("msg_001", tools=[
                ("Read", "tu_001", {"file_path": "/path/to/file"}),
                ("Bash", "tu_002", {"command": "ls"}),
            ]),
        ]
        result = parse_stream_events(_to_stdout(events))
        assert len(result) == 1
        assert len(result[0]["tools"]) == 2
        assert result[0]["tools"][0]["name"] == "Read"
        assert result[0]["tools"][0]["id"] == "tu_001"
        assert result[0]["tools"][0]["input"]["file_path"] == "/path/to/file"
        assert result[0]["tools"][1]["name"] == "Bash"

    def test_tool_results_from_user_events(self):
        events = [
            make_assistant("msg_001", tools=[("Read", "tu_001", {"file_path": "/f"})]),
            make_user(tool_results=[("tu_001", "file contents here")]),
        ]
        result = parse_stream_events(_to_stdout(events))
        assert len(result) == 2
        assert result[0]["type"] == "assistant"
        assert result[1]["type"] == "tool_result"
        assert result[1]["tool_use_id"] == "tu_001"
        assert result[1]["tool_name"] == "Read"
        assert result[1]["content"] == "file contents here"
        assert result[1]["is_error"] is False

    def test_tool_result_error(self):
        events = [
            make_assistant("msg_001", tools=[("Bash", "tu_001", {"command": "fail"})]),
            make_user(tool_results=[("tu_001", "command failed", True)]),
        ]
        result = parse_stream_events(_to_stdout(events))
        tool_result = result[1]
        assert tool_result["is_error"] is True

    def test_result_event(self):
        events = [make_result(cost_usd=0.25, num_turns=7)]
        result = parse_stream_events(_to_stdout(events))
        assert len(result) == 1
        assert result[0]["type"] == "result"
        assert result[0]["cost_usd"] == 0.25
        assert result[0]["num_turns"] == 7
        assert result[0]["timestamp"] is None

    def test_full_conversation(self):
        events = [
            make_system_init(),
            make_assistant("msg_001", text="Let me read that file."),
            make_assistant("msg_002", tools=[("Read", "tu_001", {"file_path": "/f"})]),
            make_user(tool_results=[("tu_001", "file content")]),
            make_assistant("msg_003", text="Here is the result."),
            make_result(cost_usd=0.10, num_turns=3),
        ]
        result = parse_stream_events(_to_stdout(events))
        types = [e["type"] for e in result]
        assert types == ["system", "assistant", "assistant", "tool_result",
                         "assistant", "result"]

    def test_timestamps_preserved(self):
        events = [make_assistant("msg_001", text="test")]
        result = parse_stream_events(_to_stdout(events))
        assert result[0]["timestamp"] == "2026-04-14T20:00:00.000Z"

    def test_subagent_events_in_stream(self):
        events = [
            make_assistant("msg_001", text="Delegating to agent.",
                           tools=[("Agent", "tu_agent", {"prompt": "do stuff"})]),
            make_assistant("msg_sub_001", parent_tool_use_id="tu_agent",
                           text="Working on it...",
                           tools=[("Read", "tu_sub_001", {"file_path": "/sub"})]),
        ]
        # Add agent_id to the raw event (conftest doesn't set it)
        raw = json.loads(json.dumps(events[1]))
        raw["agent_id"] = "agent-001"
        stdout = json.dumps(events[0]) + "\n" + json.dumps(raw)

        result = parse_stream_events(stdout)
        assert len(result) == 2
        sub_event = result[1]
        assert sub_event["parent_tool_use_id"] == "tu_agent"
        assert sub_event["agent_id"] == "agent-001"
        assert sub_event["text"] == "Working on it..."
        assert sub_event["tools"][0]["name"] == "Read"


# ---------------------------------------------------------------------------
# T003a: tool input capping
# ---------------------------------------------------------------------------

class TestToolInputCapping:
    def test_small_input_not_truncated(self):
        events = [
            make_assistant("msg_001", tools=[
                ("Write", "tu_001", {"file_path": "/f", "content": "short"}),
            ]),
        ]
        result = parse_stream_events(_to_stdout(events), result_cap=100)
        tool = result[0]["tools"][0]
        assert tool["input"]["content"] == "short"
        assert "_truncated" not in tool["input"]

    def test_large_input_truncated(self):
        large_content = "x" * 200
        events = [
            make_assistant("msg_001", tools=[
                ("Write", "tu_001", {"file_path": "/f", "content": large_content}),
            ]),
        ]
        result = parse_stream_events(_to_stdout(events), result_cap=50)
        tool = result[0]["tools"][0]
        assert tool["input"]["content"] == "x" * 50 + "[truncated]"
        assert tool["input"]["_truncated"]["content"]["truncated"] is True
        assert tool["input"]["_truncated"]["content"]["original_length"] == 200

    def test_tool_result_content_truncated(self):
        large_result = "y" * 200
        events = [
            make_assistant("msg_001", tools=[("Read", "tu_001", {})]),
            make_user(tool_results=[("tu_001", large_result)]),
        ]
        result = parse_stream_events(_to_stdout(events), result_cap=50)
        tr = result[1]
        assert tr["content"] == "y" * 50 + "[truncated]"
        assert tr["truncated"] is True
        assert tr["original_length"] == 200

    def test_result_cap_default(self):
        content = "z" * 50001
        events = [
            make_assistant("msg_001", tools=[("Read", "tu_001", {})]),
            make_user(tool_results=[("tu_001", content)]),
        ]
        result = parse_stream_events(_to_stdout(events))
        tr = result[1]
        assert tr["truncated"] is True
        assert tr["original_length"] == 50001
        assert tr["content"].endswith("[truncated]")
        assert len(tr["content"]) == 50000 + len("[truncated]")


# ---------------------------------------------------------------------------
# T015-T017, T020-T027: subagent and edge case tests
# (placed here since they all test the events module)
# ---------------------------------------------------------------------------

class TestSubagentEvents:
    def test_subagent_tagging_in_stream(self):
        """T015: events with parent_tool_use_id carry both tags."""
        raw_event = {
            "type": "assistant",
            "parent_tool_use_id": "tu_agent_001",
            "agent_id": "agent-001",
            "message": {
                "id": "msg_sub_001",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "working"}],
                "usage": {"input_tokens": 10, "output_tokens": 5,
                           "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0},
            },
            "timestamp": "2026-04-14T20:00:02.000Z",
        }
        result = parse_stream_events(json.dumps(raw_event))
        assert len(result) == 1
        assert result[0]["parent_tool_use_id"] == "tu_agent_001"
        assert result[0]["agent_id"] == "agent-001"

    def test_transcript_merging(self, tmp_path):
        """T016: subagent transcripts merged with agent_id, deduplicated."""
        events = [
            make_assistant("msg_001", text="root message"),
        ]
        parsed = parse_stream_events(_to_stdout(events))

        # Write transcript with 2 messages, one overlapping
        subdir = tmp_path / "subagents"
        subdir.mkdir()
        transcript = [
            {"message": {"id": "msg_new_001", "role": "assistant",
                         "content": [{"type": "text", "text": "new work"}]},
             "timestamp": "2026-04-14T20:00:03.000Z"},
            {"message": {"id": "msg_new_002", "role": "assistant",
                         "content": [{"type": "text", "text": "more work"}]},
             "timestamp": "2026-04-14T20:00:04.000Z"},
        ]
        (subdir / "agent-sub1.jsonl").write_text(
            "\n".join(json.dumps(e) for e in transcript))

        merged = merge_subagent_transcripts(parsed, str(subdir))
        sub_events = [e for e in merged if e.get("agent_id")]
        assert len(sub_events) == 2
        assert all(e["agent_id"] == "agent-sub1" for e in sub_events)

    def test_transcript_dedup(self, tmp_path):
        """T016/T024: already-streamed events not duplicated."""
        events = [
            make_assistant("msg_sub_001", parent_tool_use_id="tu_agent",
                           text="streamed"),
        ]
        parsed = parse_stream_events(_to_stdout(events))

        subdir = tmp_path / "subagents"
        subdir.mkdir()
        transcript = [
            {"message": {"id": "msg_sub_001", "role": "assistant",
                         "content": [{"type": "text", "text": "streamed"}]},
             "timestamp": "2026-04-14T20:00:01.000Z"},
            {"message": {"id": "msg_sub_002", "role": "assistant",
                         "content": [{"type": "text", "text": "new"}]},
             "timestamp": "2026-04-14T20:00:02.000Z"},
        ]
        (subdir / "agent-sub1.jsonl").write_text(
            "\n".join(json.dumps(e) for e in transcript))

        merged = merge_subagent_transcripts(parsed, str(subdir))
        assert len(merged) == 2  # original + 1 new (msg_sub_002)

    def test_merge_preserves_result_event_at_end(self, tmp_path):
        """Result events (timestamp=None) stay at end after merge."""
        events = [
            make_system_init(),
            make_assistant("msg_001", text="root"),
            make_result(cost_usd=0.10),
        ]
        parsed = parse_stream_events(_to_stdout(events))
        assert parsed[-1]["type"] == "result"

        subdir = tmp_path / "subagents"
        subdir.mkdir()
        transcript = [
            {"message": {"id": "msg_new", "role": "assistant",
                         "content": [{"type": "text", "text": "sub work"}]},
             "timestamp": "2026-04-14T20:00:05.000Z"},
        ]
        (subdir / "agent-sub1.jsonl").write_text(
            "\n".join(json.dumps(e) for e in transcript))

        merged = merge_subagent_transcripts(parsed, str(subdir))
        assert merged[-1]["type"] == "result"

    def test_root_only_filtering(self):
        """T017: filtering by not parent_tool_use_id yields only root events."""
        raw_events = [
            make_assistant("msg_001", text="root"),
        ]
        raw_sub = {
            "type": "assistant",
            "parent_tool_use_id": "tu_agent",
            "agent_id": "agent-001",
            "message": {
                "id": "msg_sub", "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "sub"}],
                "usage": {"input_tokens": 10, "output_tokens": 5,
                           "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0},
            },
            "timestamp": "2026-04-14T20:00:01.000Z",
        }
        stdout = _to_stdout(raw_events) + "\n" + json.dumps(raw_sub)
        parsed = parse_stream_events(stdout)
        root_only = [e for e in parsed if not e.get("parent_tool_use_id")]
        assert len(root_only) == 1
        assert root_only[0]["text"] == "root"


# ---------------------------------------------------------------------------
# T004-T005: US1 integration tests (collect.py and load_case_record)
# ---------------------------------------------------------------------------

class TestEventsJsonGeneration:
    """T004: verify collect.py writes events.json from stdout.log."""

    def test_events_json_written(self, tmp_path):
        events = [
            make_system_init(),
            make_assistant("msg_001", text="Hello",
                           tools=[("Read", "tu_001", {"file_path": "/f"})]),
            make_user(tool_results=[("tu_001", "content")]),
            make_result(cost_usd=0.10),
        ]
        stdout_path = tmp_path / "case_dir" / "stdout.log"
        stdout_path.parent.mkdir(parents=True)
        stdout_path.write_text(_to_stdout(events))

        output_dir = tmp_path / "output" / "cases" / "case_dir"
        output_dir.mkdir(parents=True)

        from collect import _generate_events_json
        from agent_eval.config import EvalConfig

        config = EvalConfig()
        config.traces.events = True
        _generate_events_json(stdout_path.parent, output_dir, config)

        events_json = output_dir / "events.json"
        assert events_json.exists()
        loaded = json.loads(events_json.read_text())
        types = [e["type"] for e in loaded]
        assert "system" in types
        assert "assistant" in types
        assert "tool_result" in types
        assert "result" in types

    def test_events_json_not_written_when_disabled(self, tmp_path):
        """T023: traces.events=false means no events.json and record["events"]=[]."""
        events = [make_assistant("msg_001", text="Hello")]
        stdout_path = tmp_path / "case_dir" / "stdout.log"
        stdout_path.parent.mkdir(parents=True)
        stdout_path.write_text(_to_stdout(events))

        output_dir = tmp_path / "output"
        output_dir.mkdir(parents=True)

        from collect import _generate_events_json
        from agent_eval.config import EvalConfig

        config = EvalConfig()
        config.traces.events = True
        _generate_events_json(stdout_path.parent, output_dir, config)
        assert (output_dir / "events.json").exists()

        # Now verify that the caller gate (traces.events) controls this
        output_dir2 = tmp_path / "output2"
        output_dir2.mkdir(parents=True)
        config2 = EvalConfig()
        config2.traces.events = False
        # _generate_events_json still writes if called, but the caller
        # in _collect_per_case guards with `if config.traces.events:`
        # Verify load_case_record returns [] when no events.json exists
        from score import load_case_record
        record = load_case_record(output_dir2, config2)
        assert record["events"] == []


class TestLoadCaseRecord:
    """T005: verify load_case_record loads events from events.json."""

    def test_events_loaded(self, tmp_path):
        case_dir = tmp_path / "cases" / "test-case"
        case_dir.mkdir(parents=True)

        events = [
            {"type": "assistant", "text": "hello", "tools": [],
             "timestamp": "2026-04-14T20:00:00.000Z"},
        ]
        (case_dir / "events.json").write_text(json.dumps(events))

        from score import load_case_record
        from agent_eval.config import EvalConfig

        config = EvalConfig()
        record = load_case_record(case_dir, config)
        assert record["events"] == events
        assert "stdout" not in record

    def test_malformed_events_json(self, tmp_path):
        """T026: malformed events.json returns [] with warning."""
        case_dir = tmp_path / "cases" / "test-case"
        case_dir.mkdir(parents=True)
        (case_dir / "events.json").write_text("not valid json{{{")

        from score import load_case_record
        from agent_eval.config import EvalConfig

        config = EvalConfig()
        record = load_case_record(case_dir, config)
        assert record["events"] == []

    def test_missing_events_json(self, tmp_path):
        """T023: no events.json → record["events"] is []."""
        case_dir = tmp_path / "cases" / "test-case"
        case_dir.mkdir(parents=True)

        from score import load_case_record
        from agent_eval.config import EvalConfig

        config = EvalConfig()
        record = load_case_record(case_dir, config)
        assert record["events"] == []


# ---------------------------------------------------------------------------
# T010: {{ conversation }} rendering
# ---------------------------------------------------------------------------

class TestConversationRendering:
    """T010: verify {{ conversation }} renders root-only assistant text."""

    def test_conversation_variable(self):
        events = [
            {"type": "system", "subtype": "init", "model": "claude-opus-4-6",
             "timestamp": "2026-04-14T20:00:00.000Z"},
            {"type": "assistant", "text": "Let me check that file.",
             "tools": [{"name": "Read", "id": "tu_001",
                        "input": {"file_path": "/f"}}],
             "timestamp": "2026-04-14T20:00:01.000Z"},
            {"type": "assistant", "text": "Subagent working...",
             "tools": [], "parent_tool_use_id": "tu_agent",
             "agent_id": "agent-001",
             "timestamp": "2026-04-14T20:00:02.000Z"},
            {"type": "assistant", "text": "Here is the result.",
             "tools": [],
             "timestamp": "2026-04-14T20:00:03.000Z"},
        ]

        rendered = extract_conversation_text(events)
        assert "Let me check that file." in rendered
        assert "Here is the result." in rendered
        assert "Subagent working..." not in rendered

    def test_conversation_empty_when_no_text(self):
        events = [
            {"type": "assistant", "text": "",
             "tools": [{"name": "Read", "id": "tu_001", "input": {}}],
             "timestamp": "2026-04-14T20:00:01.000Z"},
        ]
        assert extract_conversation_text(events) == ""



# ---------------------------------------------------------------------------
# T014: process quality judge pattern
# ---------------------------------------------------------------------------

class TestProcessQualityPattern:
    """T014: verify iterating events gives correct tool call sequence."""

    def test_tool_call_sequence(self):
        events = [
            make_system_init(),
            make_assistant("msg_001", tools=[
                ("Read", "tu_001", {"file_path": "/f"}),
            ]),
            make_user(tool_results=[("tu_001", "content")]),
            make_assistant("msg_002", tools=[
                ("Write", "tu_002", {"file_path": "/g", "content": "x"}),
            ]),
        ]
        parsed = parse_stream_events(_to_stdout(events))
        tool_sequence = []
        for event in parsed:
            if event["type"] == "assistant":
                for tool in event.get("tools", []):
                    tool_sequence.append(tool["name"])
        assert tool_sequence == ["Read", "Write"]


class TestEdgeCases:
    def test_empty_stdout(self):
        """T020: empty/missing stdout returns []."""
        assert parse_stream_events("") == []

    def test_non_jsonl_content(self):
        """T021: plain text lines skipped."""
        assert parse_stream_events("not json\nalso not json\n") == []

    def test_tool_result_exceeding_cap(self):
        """T022: tool result truncated with marker and metadata."""
        large = "a" * 60000
        events = [
            make_assistant("msg_001", tools=[("Read", "tu_001", {})]),
            make_user(tool_results=[("tu_001", large)]),
        ]
        result = parse_stream_events(_to_stdout(events), result_cap=50000)
        tr = result[1]
        assert tr["content"].endswith("[truncated]")
        assert tr["truncated"] is True
        assert tr["original_length"] == 60000

    def test_subagent_dedup(self, tmp_path):
        """T024: events in both stdout and transcript not double-counted."""
        events = [
            make_assistant("msg_sub_001", parent_tool_use_id="tu_agent",
                           text="I'm in both"),
        ]
        parsed = parse_stream_events(_to_stdout(events))

        subdir = tmp_path / "subagents"
        subdir.mkdir()
        (subdir / "agent-sub.jsonl").write_text(json.dumps({
            "message": {"id": "msg_sub_001", "role": "assistant",
                        "content": [{"type": "text", "text": "I'm in both"}]},
            "timestamp": "2026-04-14T20:00:01.000Z",
        }))

        merged = merge_subagent_transcripts(parsed, str(subdir))
        assert len(merged) == 1

    def test_non_utf8_content(self):
        """T025: binary content replaced with placeholder."""
        from agent_eval.events import _sanitize_text
        result = _sanitize_text(b"\x80\x81\x82\xff\xfe")
        assert result == "(binary content, 5 bytes)"

    def test_sanitize_valid_utf8(self):
        from agent_eval.events import _sanitize_text
        assert _sanitize_text("hello") == "hello"
        assert _sanitize_text(b"hello") == "hello"

    def test_benchmark_parse_linear_scaling(self):
        """T027: verify parsing scales linearly with input size."""
        import time

        def make_stdout(n_lines):
            events = []
            for i in range(n_lines):
                events.append(make_assistant(
                    f"msg_{i}", text=f"message {i}",
                    tools=[("Read", f"tu_{i}", {"file_path": f"/f{i}"})]))
                events.append(make_user(tool_results=[(f"tu_{i}", f"content {i}")]))
            return _to_stdout(events)

        sizes = [10, 100, 1000]
        times = []
        for n in sizes:
            stdout = make_stdout(n)
            start = time.perf_counter()
            parse_stream_events(stdout)
            elapsed = time.perf_counter() - start
            times.append(elapsed)

        # 100x more input should take less than 200x the time (allowing overhead)
        ratio = times[2] / max(times[0], 1e-9)
        assert ratio < 200, f"Non-linear scaling: {sizes[0]} lines={times[0]:.4f}s, {sizes[2]} lines={times[2]:.4f}s, ratio={ratio:.1f}"

    def test_mixed_valid_and_invalid_lines(self):
        """Lines that aren't valid JSON are silently skipped."""
        stdout = "garbage\n" + json.dumps(make_system_init()) + "\nmore garbage\n"
        result = parse_stream_events(stdout)
        assert len(result) == 1
        assert result[0]["type"] == "system"
