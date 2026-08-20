"""Tests for _extract_progress and _extract_denial_list in claude_code.py."""

import os

import pytest

from conftest import make_assistant, make_result, make_user

from agent_eval.agent.claude_code import (
    ClaudeCodeRunner, _detect_unknown_command, _extract_denial_list,
    _extract_progress,
)


def test_root_assistant_shows_tool():
    """Root assistant event with Bash tool shows the command."""
    event = make_assistant("msg_001",
                           tools=[("Bash", "tu_001", {"command": "ls -la"})])
    assert _extract_progress(event) == "Running: ls -la"


def test_subagent_msg_returns_empty():
    """Foreground subagent message (with parent_tool_use_id) returns empty."""
    event = make_assistant("msg_sub_001",
                           parent_tool_use_id="tu_agent_x",
                           tools=[("Bash", "tu_sub", {"command": "echo hi"})])
    assert _extract_progress(event) == ""


def test_result_event_shows_summary():
    """Result event shows turn count and cost."""
    event = make_result(cost_usd=0.15, num_turns=10)
    assert _extract_progress(event) == "Done (10 turns, $0.15)"


def test_permission_denial_detected():
    """User event with is_error and denial text surfaces a warning."""
    event = make_user(tool_results=[
        ("tu_001", "The user denied this tool call. Reason: not in allow list", True),
    ])
    result = _extract_progress(event)
    assert result.startswith("PERMISSION DENIED:")
    assert "denied" in result


def test_non_permission_error_ignored():
    """User event with is_error but non-permission text returns empty."""
    event = make_user(tool_results=[
        ("tu_001", "Error: file not found /tmp/missing.txt", True),
    ])
    assert _extract_progress(event) == ""


def test_normal_tool_result_ignored():
    """User event with a normal tool_result (no error) returns empty."""
    event = make_user(tool_results=[("tu_001", "file contents here")])
    assert _extract_progress(event) == ""


# ── _extract_denial_list tests ──────────────────────────────────────


def test_denial_list_from_result_event():
    """Structured permission_denials from result event are preferred."""
    denials = [
        {"tool_name": "Write", "tool_use_id": "tu_001", "tool_input": {}},
        {"tool_name": "Bash", "tool_use_id": "tu_002", "tool_input": {}},
    ]
    result_obj = make_result(permission_denials=denials)
    assert _extract_denial_list(result_obj, 0) == denials


def test_denial_list_result_takes_precedence_over_streaming():
    """Result event denials win even when streaming counter disagrees."""
    denials = [{"tool_name": "Write", "tool_use_id": "tu_001", "tool_input": {}}]
    result_obj = make_result(permission_denials=denials)
    assert _extract_denial_list(result_obj, 5) == denials


def test_denial_list_fallback_to_streaming_count():
    """When result has no denials, fall back to streaming counter."""
    result_obj = make_result()
    result = _extract_denial_list(result_obj, 3)
    assert len(result) == 3
    assert all(d["tool_name"] == "unknown" for d in result)


def test_denial_list_no_result_obj_with_streaming():
    """When result_obj is None (timeout), use streaming counter."""
    result = _extract_denial_list(None, 2)
    assert len(result) == 2


def test_denial_list_empty_when_no_denials():
    """No denials anywhere returns None."""
    result_obj = make_result(permission_denials=[])
    assert _extract_denial_list(result_obj, 0) is None


def test_denial_list_none_when_both_absent():
    """No result_obj and no streaming count returns None."""
    assert _extract_denial_list(None, 0) is None


class TestDetectUnknownCommand:
    """An unrecognised slash command is reported by the CLI as a *successful*
    run — exit 0, subtype "success", is_error false, 0 turns, $0.00 — so without
    this detection a misconfigured skill reads as "ran, produced nothing" and a
    whole eval completes green in seconds.

    Shape pinned against a real `claude --print --output-format stream-json` run.
    """

    def _result(self, **over):
        obj = {"type": "result", "subtype": "success", "is_error": False,
               "num_turns": 0, "total_cost_usd": 0,
               "result": "Unknown command: /epic-decompose"}
        obj.update(over)
        return obj

    def test_detects_unknown_slash_command(self):
        assert _detect_unknown_command(self._result()) == "/epic-decompose"

    def test_tolerates_surrounding_whitespace(self):
        assert _detect_unknown_command(
            self._result(result="  Unknown command: /foo\n")) == "/foo"

    def test_ignores_a_normal_successful_run(self):
        assert _detect_unknown_command(
            self._result(num_turns=12, result="Created 4 epics.")) is None

    def test_turns_guard_prevents_false_positive(self):
        """A real run that merely quotes the phrase must not be failed."""
        assert _detect_unknown_command(self._result(
            num_turns=7,
            result="The user typed Unknown command: /foo, so I explained it.",
        )) is None

    def test_phrase_must_lead_the_message(self):
        assert _detect_unknown_command(self._result(
            result="Everything worked. Unknown command: /foo was not needed.",
        )) is None

    def test_requires_a_slash_command(self):
        assert _detect_unknown_command(
            self._result(result="Unknown command: epic-decompose")) is None

    @pytest.mark.parametrize("turns", [0, None, False, "0", 0.0, [], {}])
    def test_no_evidence_of_work_still_detects(self, turns):
        """Absent/null/non-numeric counts must not be read as "had turns" — the
        leading phrase is the signal, and a stricter reading would silently stop
        detecting the bug if the payload shape changed."""
        assert _detect_unknown_command(
            self._result(num_turns=turns)) == "/epic-decompose"

    def test_missing_turn_count_key_still_detects(self):
        obj = self._result()
        del obj["num_turns"]
        assert _detect_unknown_command(obj) == "/epic-decompose"

    @pytest.mark.parametrize("turns", [1, 12, 2.5, True])
    def test_positive_turn_count_suppresses(self, turns):
        """Any positive count is evidence real work happened."""
        assert _detect_unknown_command(self._result(num_turns=turns)) is None

    def test_handles_missing_or_odd_payloads(self):
        assert _detect_unknown_command(None) is None
        assert _detect_unknown_command({}) is None
        assert _detect_unknown_command({"result": None, "num_turns": 0}) is None
        assert _detect_unknown_command({"result": 42, "num_turns": 0}) is None


class TestUnknownCommandFailsTheRun:
    """End-to-end through ClaudeCodeRunner.run_skill with a stub `claude` on PATH,
    reproducing the exact stream a real CLI emits for an unrecognised command.

    execute.py derives its per-case OK/FAIL purely from RunResult.exit_code, so
    flipping it here is what turns a silently-green run into a visible failure.
    """

    STREAM = (
        '{"type":"system","subtype":"init","session_id":"s1"}\n'
        '{"type":"assistant","message":{"content":[{"type":"text",'
        '"text":"Unknown command: /epic-decompose"}]},"session_id":"s1"}\n'
        '{"type":"result","subtype":"success","is_error":false,"num_turns":0,'
        '"total_cost_usd":0,"result":"Unknown command: /epic-decompose",'
        '"session_id":"s1"}\n'
    )

    OK_STREAM = (
        '{"type":"system","subtype":"init","session_id":"s1"}\n'
        '{"type":"result","subtype":"success","is_error":false,"num_turns":3,'
        '"total_cost_usd":0.5,"result":"Wrote 4 epics.","session_id":"s1"}\n'
    )

    def _stub_claude(self, tmp_path, monkeypatch, stream):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        stub = bindir / "claude"
        stub.write_text(
            "#!/bin/sh\ncat > /dev/null\n"
            f"cat <<'STREAM_EOF'\n{stream}STREAM_EOF\n"
        )
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    # Single-object payload: without a log prefix the runner asks for
    # --output-format json and parses one object instead of a stream.
    JSON_UNKNOWN = ('{"type":"result","subtype":"success","is_error":false,'
                    '"num_turns":0,"total_cost_usd":0,'
                    '"result":"Unknown command: /epic-decompose"}\n')

    def _run(self, tmp_path, log_prefix=None):
        ws = tmp_path / "ws"
        ws.mkdir()
        return ClaudeCodeRunner(log_prefix=log_prefix).execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60,
        )

    def test_unknown_command_fails_the_streaming_path(self, tmp_path, monkeypatch):
        """eval-run always passes a log prefix, so this is the production path."""
        self._stub_claude(tmp_path, monkeypatch, self.STREAM)
        result = self._run(tmp_path, log_prefix="test")
        assert result.exit_code != 0, "an unrecognised skill must fail the case"
        assert "/epic-decompose" in result.stderr
        assert "runner.plugin_dirs" in result.stderr

    def test_unknown_command_fails_the_json_path(self, tmp_path, monkeypatch):
        self._stub_claude(tmp_path, monkeypatch, self.JSON_UNKNOWN)
        result = self._run(tmp_path)
        assert result.exit_code != 0, "an unrecognised skill must fail the case"
        assert "/epic-decompose" in result.stderr

    def test_normal_run_still_succeeds(self, tmp_path, monkeypatch):
        """Guard against failing healthy runs."""
        self._stub_claude(tmp_path, monkeypatch, self.OK_STREAM)
        result = self._run(tmp_path, log_prefix="test")
        assert result.exit_code == 0, result.stderr
        assert "Unknown command" not in (result.stderr or "")


class TestDenialAggregationAcrossSegments:
    """A CLI-resumed session emits one result event PER segment, each carrying
    only that segment's permission_denials. Reading only the final event lost
    7 denials on a real run (the final segment's list was empty), which hid a
    workspace escape from run_result.json."""

    def test_collected_segments_are_unioned(self):
        seg1 = [{"tool_name": "Read", "tool_use_id": "tu_a", "tool_input": {}}]
        seg2 = [{"tool_name": "Read", "tool_use_id": "tu_b", "tool_input": {}}]
        final = make_result(permission_denials=[])  # last segment: empty
        got = _extract_denial_list(final, 0, collected=seg1 + seg2)
        assert got == seg1 + seg2

    def test_collected_wins_over_empty_final_event(self):
        collected = [{"tool_name": "Write", "tool_use_id": "tu_x", "tool_input": {}}]
        assert _extract_denial_list(make_result(), 0, collected=collected) == collected

    def test_cumulative_reporting_is_deduplicated(self):
        """If a CLI version reports cumulatively, the same tool_use_id must
        not be double-counted."""
        d = {"tool_name": "Read", "tool_use_id": "tu_a", "tool_input": {}}
        got = _extract_denial_list(None, 0, collected=[d, dict(d)])
        assert got == [d]

    def test_entries_without_ids_are_kept(self):
        anon = [{"tool_name": "unknown"}, {"tool_name": "unknown"}]
        assert _extract_denial_list(None, 0, collected=list(anon)) == anon

    def test_no_collected_falls_back_to_final_then_streaming(self):
        denials = [{"tool_name": "Bash", "tool_use_id": "tu_1", "tool_input": {}}]
        assert _extract_denial_list(make_result(permission_denials=denials), 0) == denials
        assert len(_extract_denial_list(make_result(), 4)) == 4

    def test_runner_unions_denials_from_stream(self, tmp_path, monkeypatch):
        """End-to-end: two result events in the stream, denials only in the
        first — RunResult must still carry them."""
        import os
        seg1_denial = ('[{"tool_name":"Read","tool_use_id":"tu_esc",'
                       '"tool_input":{"file_path":"/repo/secret"}}]')
        stream = (
            '{"type":"system","subtype":"init","session_id":"s1"}\n'
            '{"type":"result","subtype":"success","is_error":false,"num_turns":5,'
            f'"total_cost_usd":0.5,"result":"partial","permission_denials":{seg1_denial},'
            '"session_id":"s1"}\n'
            '{"type":"system","subtype":"init","session_id":"s1"}\n'
            '{"type":"result","subtype":"success","is_error":false,"num_turns":2,'
            '"total_cost_usd":0.6,"result":"done","permission_denials":[],'
            '"session_id":"s1"}\n'
        )
        bindir = tmp_path / "bin"
        bindir.mkdir()
        stub = bindir / "claude"
        stub.write_text("#!/bin/sh\ncat > /dev/null\n"
                        f"cat <<'STREAM_EOF'\n{stream}STREAM_EOF\n")
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
        ws = tmp_path / "ws"
        ws.mkdir()
        result = ClaudeCodeRunner(log_prefix="test").execute(
            target="x", args="", workspace=ws, model="m", timeout_s=60)
        assert result.permission_denials is not None
        assert [d["tool_use_id"] for d in result.permission_denials] == ["tu_esc"]
