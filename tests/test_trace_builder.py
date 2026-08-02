"""Tests for agent_eval.mlflow.trace_builder — trace construction and span hierarchy."""

import json

from conftest import (
    make_assistant, make_result, make_system_init, make_user,
)

from agent_eval.mlflow.trace_builder import build_trace, make_span


def _write_stream(tmp_path, events):
    """Write events to a stdout.log file and return the path."""
    stdout = tmp_path / "stdout.log"
    stdout.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return stdout


def _basic_run_result():
    return {
        "exit_code": 0,
        "duration_s": 10.0,
        "cost_usd": 0.10,
        "model": "claude-sonnet-4-5",
        "token_usage": {"input": 100, "output": 50,
                        "cache_read": 0, "cache_create": 0},
    }


def _get_span_type(span):
    """Extract mlflow.spanType from a span's attributes."""
    return json.loads(span["attributes"].get("mlflow.spanType", '"?"'))


class TestMakeSpan:
    def test_required_fields(self):
        """make_span returns a dict with all required trace span fields."""
        span = make_span(
            trace_id="tr-001", parent_id="sp-parent",
            name="test", span_type="TOOL",
            start_ns=1000, end_ns=2000,
        )
        assert span["trace_id"] == "tr-001"
        assert span["parent_span_id"] == "sp-parent"
        assert span["name"] == "test"
        assert span["start_time_unix_nano"] == 1000
        assert span["end_time_unix_nano"] == 2000
        assert len(span["span_id"]) == 16  # hex of 8 bytes
        assert _get_span_type(span) == "TOOL"


class TestBuildTrace:
    def test_returns_dict_with_spans(self, tmp_path):
        """build_trace returns a trace dict with info and non-empty spans."""
        events = [
            make_system_init(),
            make_assistant("msg_001",
                           tools=[("Bash", "tu_001", {"command": "ls"})]),
            make_user(tool_results=[("tu_001", "file1.txt")]),
            make_result(cost_usd=0.10, num_turns=1),
        ]
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _basic_run_result(),
                            run_id="test-run", experiment_id="exp-001")
        assert trace is not None
        assert "info" in trace
        assert trace["info"]["trace_id"]
        spans = trace["data"]["spans"]
        assert len(spans) > 0
        # Root span (no parent) should be AGENT type
        root = next(s for s in spans if s["parent_span_id"] is None)
        assert _get_span_type(root) == "AGENT"

    def test_skips_subagent_in_top_segments(self, tmp_path):
        """Foreground subagent messages do not create top-level tool spans."""
        events = [
            make_system_init(),
            make_assistant("msg_001",
                           tools=[("Bash", "tu_001", {"command": "ls"})]),
            make_user(tool_results=[("tu_001", "output")]),
            # Foreground subagent message — should NOT appear as a top-level segment
            make_assistant("msg_sub_001",
                           parent_tool_use_id="tu_agent_x",
                           tools=[("Read", "tu_sub", {"file_path": "/sub"})]),
            make_result(cost_usd=0.10, num_turns=2),
        ]
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _basic_run_result(),
                            run_id="test-run", experiment_id="exp-001")
        assert trace is not None
        spans = trace["data"]["spans"]
        root = next(s for s in spans if s["parent_span_id"] is None)
        # Find all TOOL-type spans
        tool_spans = [s for s in spans if _get_span_type(s) == "TOOL"]
        tool_names = [s["name"] for s in tool_spans]
        # Bash should be present as a top-level tool span
        assert any("Bash" in n for n in tool_names)
        # The subagent's Read should NOT be a direct child of root
        # (it should be nested under an Agent span, if at all)
        top_level_reads = [
            s for s in tool_spans
            if "Read" in s["name"]
            and s["parent_span_id"] == root["span_id"]
        ]
        assert len(top_level_reads) == 0

    def test_returns_none_for_missing_file(self, tmp_path):
        """build_trace returns None if stdout file doesn't exist."""
        result = build_trace(tmp_path / "nonexistent.log",
                             _basic_run_result(),
                             run_id="x", experiment_id="e")
        assert result is None

    def test_thinking_blocks_become_llm_spans(self, tmp_path):
        """Extended thinking content blocks are emitted as thinking LLM spans."""
        events = [
            make_system_init(),
            make_assistant(
                "msg_001",
                thinking="I should write a hello world file.",
                tools=[("Write", "tu_001",
                        {"file_path": "/workspace/hello.py",
                         "content": 'print("Hello")\n'})],
            ),
            make_user(tool_results=[("tu_001", "File created")]),
            make_assistant("msg_002", text="Created hello.py"),
            make_result(cost_usd=0.10, num_turns=2),
        ]
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _basic_run_result(),
                            run_id="test-run", experiment_id="exp-001")
        assert trace is not None
        spans = trace["data"]["spans"]
        thinking = [s for s in spans if s["name"] == "thinking"]
        assert len(thinking) == 1
        outputs = json.loads(thinking[0]["attributes"]["mlflow.spanOutputs"])
        assert "hello world" in outputs["thinking"]

    def test_trajectory_user_steps_seed_prompt_and_chain_spans(self, tmp_path):
        """Harbor trajectory.json user steps become root prompt + CHAIN spans."""
        events = [
            make_system_init(),
            # Harbor-like: no user text in stream-json, only tool_result user event
            make_assistant(
                "msg_001",
                thinking="Plan the file write.",
                tools=[("Write", "tu_001", {"file_path": "/x", "content": "x"})],
            ),
            make_user(tool_results=[("tu_001", "ok")]),
            make_assistant("msg_002", text="Created /x"),
            make_result(cost_usd=0.10, num_turns=2),
        ]
        stdout = _write_stream(tmp_path, events)
        traj = tmp_path / "trajectory.json"
        traj.write_text(json.dumps({
            "schema_version": "ATIF-v1.7",
            "steps": [
                {
                    "step_id": 1,
                    "timestamp": "2026-04-14T19:59:59.000Z",
                    "source": "user",
                    "message": "<command-name>/aeh-hello-world</command-name>",
                },
                {
                    "step_id": 2,
                    "timestamp": "2026-04-14T19:59:59.100Z",
                    "source": "user",
                    "message": "Create a simple Hello, World! Python program.",
                },
                {
                    "step_id": 3,
                    "source": "agent",
                    "message": "Created /x",
                    "reasoning_content": "Plan the file write.",
                },
            ],
        }))
        trace = build_trace(
            stdout, _basic_run_result(),
            run_id="test-run", experiment_id="exp-001",
            trajectory_path=traj,
        )
        assert trace is not None
        root = next(s for s in trace["data"]["spans"] if s["parent_span_id"] is None)
        root_inputs = json.loads(root["attributes"]["mlflow.spanInputs"])
        assert "/aeh-hello-world" in root_inputs["prompt"]
        assert "Hello, World!" in root_inputs["prompt"]
        # Must not fall back to the assistant's final text as the prompt
        assert root_inputs["prompt"] != "Created /x"

        user_spans = [
            s for s in trace["data"]["spans"]
            if _get_span_type(s) == "CHAIN" and s["name"].startswith("user:")
        ]
        assert len(user_spans) == 2
        messages = [
            json.loads(s["attributes"]["mlflow.spanOutputs"])["message"]
            for s in user_spans
        ]
        assert any("/aeh-hello-world" in m for m in messages)
        assert any("Hello, World!" in m for m in messages)

    def test_prompt_matches_chain_spans_when_stream_also_has_user_text(self, tmp_path):
        """Root prompt must agree with the CHAIN spans, not mix stream + trajectory."""
        events = [
            make_system_init(),
            make_user(text="stale prompt from a retried stream"),
            make_assistant("msg_001", text="Created /x"),
            make_result(cost_usd=0.10, num_turns=1),
        ]
        stdout = _write_stream(tmp_path, events)
        traj = tmp_path / "trajectory.json"
        traj.write_text(json.dumps({
            "schema_version": "ATIF-v1.7",
            "steps": [
                {
                    "step_id": 1,
                    "timestamp": "2026-04-14T19:59:59.000Z",
                    "source": "user",
                    "message": "Create a simple Hello, World! Python program.",
                },
            ],
        }))
        trace = build_trace(
            stdout, _basic_run_result(),
            run_id="test-run", experiment_id="exp-001",
            trajectory_path=traj,
        )
        root = next(s for s in trace["data"]["spans"] if s["parent_span_id"] is None)
        root_inputs = json.loads(root["attributes"]["mlflow.spanInputs"])
        user_spans = [
            s for s in trace["data"]["spans"]
            if _get_span_type(s) == "CHAIN" and s["name"].startswith("user:")
        ]
        chain_message = json.loads(
            user_spans[0]["attributes"]["mlflow.spanOutputs"])["message"]

        # Root prompt and the CHAIN span must come from the same source
        # (trajectory), not disagree with each other.
        assert root_inputs["prompt"] == chain_message
        assert "stale prompt" not in root_inputs["prompt"]

    def test_write_tool_span_includes_file_content(self, tmp_path):
        """Write TOOL span inputs keep the file body, not just file_path."""
        events = [
            make_system_init(),
            make_assistant(
                "msg_001",
                thinking="Write hello.py",
                tools=[("Write", "tu_001",
                        {"file_path": "/workspace/hello.py",
                         "content": 'print("Hello, World!")\n'})],
            ),
            make_user(tool_results=[("tu_001", "File created")]),
            make_assistant("msg_002", text="Created hello.py"),
            make_result(cost_usd=0.10, num_turns=2),
        ]
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _basic_run_result(),
                            run_id="test-run", experiment_id="exp-001")
        write = next(s for s in trace["data"]["spans"] if s["name"] == "Write")
        inputs = json.loads(write["attributes"]["mlflow.spanInputs"])
        assert inputs["file_path"] == "/workspace/hello.py"
        assert 'print("Hello, World!")' in inputs["content"]

    def test_trajectory_observation_enriches_tool_output(self, tmp_path):
        """ATIF observation content replaces a short stream tool_result."""
        events = [
            make_system_init(),
            make_assistant(
                "msg_001",
                thinking="Write the file.",
                # Stream omits content; trajectory supplies full args + observation.
                tools=[("Write", "tu_001", {"file_path": "/workspace/hello.py"})],
            ),
            make_user(tool_results=[("tu_001", "File created successfully")]),
            make_assistant("msg_002", text="Created hello.py"),
            make_result(cost_usd=0.10, num_turns=2),
        ]
        stdout = _write_stream(tmp_path, events)
        rich = (
            "File created successfully at: /workspace/hello.py\n\n"
            '[metadata] {"type": "create", "filePath": "/workspace/hello.py", '
            '"content": "print(\\"Hello, World!\\")\\n"}'
        )
        traj = tmp_path / "trajectory.json"
        traj.write_text(json.dumps({
            "schema_version": "ATIF-v1.7",
            "steps": [
                {
                    "step_id": 1,
                    "source": "user",
                    "message": "Create hello.py",
                },
                {
                    "step_id": 2,
                    "source": "agent",
                    "tool_calls": [{
                        "tool_call_id": "tu_001",
                        "function_name": "Write",
                        "arguments": {
                            "file_path": "/workspace/hello.py",
                            "content": 'print("Hello, World!")\n',
                        },
                    }],
                    "observation": {
                        "results": [{
                            "source_call_id": "tu_001",
                            "content": rich,
                        }],
                    },
                },
            ],
        }))
        trace = build_trace(
            stdout, _basic_run_result(),
            run_id="test-run", experiment_id="exp-001",
            trajectory_path=traj,
        )
        write = next(s for s in trace["data"]["spans"] if s["name"] == "Write")
        inputs = json.loads(write["attributes"]["mlflow.spanInputs"])
        outputs = json.loads(write["attributes"]["mlflow.spanOutputs"])
        assert 'print("Hello, World!")' in inputs["content"]
        assert "Hello, World!" in outputs["result"]
        assert "[metadata]" in outputs["result"]

    def test_agent_step_spans_include_outputs(self, tmp_path):
        """AGENT step spans expose text/thinking and tool names."""
        events = [
            make_system_init(),
            make_assistant(
                "msg_001",
                thinking="I should write a hello world file.",
                tools=[("Write", "tu_001",
                        {"file_path": "/workspace/hello.py",
                         "content": 'print("Hi")\n'})],
            ),
            make_user(tool_results=[("tu_001", "ok")]),
            make_assistant("msg_002", text="Created hello.py"),
            make_result(cost_usd=0.10, num_turns=2),
        ]
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _basic_run_result(),
                            run_id="test-run", experiment_id="exp-001")
        agent_steps = [
            s for s in trace["data"]["spans"]
            if _get_span_type(s) == "AGENT" and s["parent_span_id"] is not None
        ]
        assert agent_steps
        # At least one step should carry thinking or final text in outputs.
        outputs_list = [
            json.loads(s["attributes"].get("mlflow.spanOutputs", "{}"))
            for s in agent_steps
            if "mlflow.spanOutputs" in s["attributes"]
        ]
        assert any(o.get("thinking") or o.get("text") for o in outputs_list)
        # The write step should list Write among tools.
        inputs_list = [
            json.loads(s["attributes"]["mlflow.spanInputs"])
            for s in agent_steps
        ]
        assert any("Write" in (i.get("tools") or []) for i in inputs_list)

    def test_thinking_attributed_to_its_own_turns_step(self, tmp_path):
        """Each step's thinking output is its OWN turn's, not the next turn's.

        Regression test: thinking is emitted before its turn's text/tool_use
        in stream order, so a step-grouping bug can misattribute it to the
        *previous* step instead of the one it actually belongs to.
        """
        events = [
            make_system_init(),
            make_assistant(
                "msg_001",
                thinking="Plan: write file A first.",
                text="Writing file A now.",
                tools=[("Write", "tu_001",
                        {"file_path": "/workspace/a.py", "content": "a = 1\n"})],
            ),
            make_user(tool_results=[("tu_001", "File A created")]),
            make_assistant(
                "msg_002",
                thinking="Plan: write file B next.",
                text="Writing file B now.",
                tools=[("Write", "tu_002",
                        {"file_path": "/workspace/b.py", "content": "b = 2\n"})],
            ),
            make_user(tool_results=[("tu_002", "File B created")]),
            make_assistant("msg_003", text="Done."),
            make_result(cost_usd=0.10, num_turns=3),
        ]
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _basic_run_result(),
                            run_id="test-run", experiment_id="exp-001")
        agent_steps = [
            s for s in trace["data"]["spans"]
            if _get_span_type(s) == "AGENT" and s["parent_span_id"] is not None
        ]

        def _outputs(step_text_substr):
            step = next(
                s for s in agent_steps
                if step_text_substr in json.loads(
                    s["attributes"].get("mlflow.spanOutputs", "{}")
                ).get("text", "")
            )
            return json.loads(step["attributes"]["mlflow.spanOutputs"])

        step_a = _outputs("Writing file A")
        step_b = _outputs("Writing file B")
        assert "file A first" in step_a.get("thinking", "")
        assert "file B next" not in step_a.get("thinking", "")
        assert "file B next" in step_b.get("thinking", "")
        assert "file A first" not in step_b.get("thinking", "")

    def test_chain_span_clamped_to_trace_window(self, tmp_path):
        """A trajectory timestamp far outside the trace window gets clamped."""
        events = [
            make_system_init(),
            make_assistant("msg_001", text="Created /x"),
            make_result(cost_usd=0.10, num_turns=1),
        ]
        stdout = _write_stream(tmp_path, events)
        traj = tmp_path / "trajectory.json"
        traj.write_text(json.dumps({
            "schema_version": "ATIF-v1.7",
            "steps": [
                {
                    # Clock way before the stream-json events (skewed source).
                    "timestamp": "1970-01-01T00:00:00.000Z",
                    "source": "user",
                    "message": "Create a simple Hello, World! Python program.",
                },
            ],
        }))
        trace = build_trace(
            stdout, _basic_run_result(),
            run_id="test-run", experiment_id="exp-001",
            trajectory_path=traj,
        )
        root = next(s for s in trace["data"]["spans"] if s["parent_span_id"] is None)
        user_span = next(
            s for s in trace["data"]["spans"]
            if _get_span_type(s) == "CHAIN" and s["name"].startswith("user:")
        )
        assert user_span["start_time_unix_nano"] >= root["start_time_unix_nano"]
        assert user_span["end_time_unix_nano"] <= root["end_time_unix_nano"]

    def test_reasoning_content_backfills_thinking_when_stream_has_none(self, tmp_path):
        """ATIF reasoning_content fills in thinking when the stream has none."""
        events = [
            make_system_init(),
            make_assistant("msg_001", text="Writing file A now.",
                           tools=[("Write", "tu_001",
                                   {"file_path": "/x", "content": "a = 1\n"})]),
            make_user(tool_results=[("tu_001", "ok")]),
            make_assistant("msg_002", text="Done."),
            make_result(cost_usd=0.10, num_turns=2),
        ]
        stdout = _write_stream(tmp_path, events)
        traj = tmp_path / "trajectory.json"
        traj.write_text(json.dumps({
            "schema_version": "ATIF-v1.7",
            "steps": [
                {
                    "source": "agent",
                    "reasoning_content": "Backfilled plan for file A.",
                },
            ],
        }))
        trace = build_trace(
            stdout, _basic_run_result(),
            run_id="test-run", experiment_id="exp-001",
            trajectory_path=traj,
        )
        thinking_spans = [s for s in trace["data"]["spans"] if s["name"] == "thinking"]
        assert len(thinking_spans) == 1
        outputs = json.loads(thinking_spans[0]["attributes"]["mlflow.spanOutputs"])
        assert "Backfilled plan for file A" in outputs["thinking"]

    def test_reasoning_content_does_not_override_existing_stream_thinking(self, tmp_path):
        """Trajectory reasoning_content is ignored when stream already has thinking."""
        events = [
            make_system_init(),
            make_assistant("msg_001", thinking="Real stream thinking.",
                           text="Writing file A now.",
                           tools=[("Write", "tu_001",
                                   {"file_path": "/x", "content": "a = 1\n"})]),
            make_user(tool_results=[("tu_001", "ok")]),
            make_assistant("msg_002", text="Done."),
            make_result(cost_usd=0.10, num_turns=2),
        ]
        stdout = _write_stream(tmp_path, events)
        traj = tmp_path / "trajectory.json"
        traj.write_text(json.dumps({
            "schema_version": "ATIF-v1.7",
            "steps": [
                {"source": "agent", "reasoning_content": "Should not appear."},
            ],
        }))
        trace = build_trace(
            stdout, _basic_run_result(),
            run_id="test-run", experiment_id="exp-001",
            trajectory_path=traj,
        )
        thinking_spans = [s for s in trace["data"]["spans"] if s["name"] == "thinking"]
        assert len(thinking_spans) == 1
        outputs = json.loads(thinking_spans[0]["attributes"]["mlflow.spanOutputs"])
        assert outputs["thinking"] == "Real stream thinking."

    def test_thinking_flushed_before_tool_only_turn(self, tmp_path):
        """Pending thinking is attached to a tool-only turn (no llm text)."""
        events = [
            make_system_init(),
            make_assistant("msg_001", thinking="Thinking before tool.",
                           tools=[("Write", "tu_001",
                                   {"file_path": "/x", "content": "a = 1\n"})]),
            make_user(tool_results=[("tu_001", "ok")]),
            make_result(cost_usd=0.10, num_turns=1),
        ]
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(
            stdout, _basic_run_result(),
            run_id="test-run", experiment_id="exp-001",
        )
        agent_steps = [
            s for s in trace["data"]["spans"]
            if _get_span_type(s) == "AGENT" and s["parent_span_id"] is not None
        ]
        assert len(agent_steps) == 1
        outputs = json.loads(agent_steps[0]["attributes"]["mlflow.spanOutputs"])
        assert "Thinking before tool" in outputs.get("thinking", "")

    def test_thinking_tool_only_turn_does_not_reuse_prior_llm_text(self, tmp_path):
        """A thinking→tool turn after a texted step must not reuse that text.

        Regression: _flush_step used to leave current_llm set, so a later
        no-text tool turn inherited the previous step's assistant text.
        """
        events = [
            make_system_init(),
            make_assistant(
                "msg_001",
                text="Writing file A now.",
                tools=[("Write", "tu_001",
                        {"file_path": "/workspace/a.py", "content": "a = 1\n"})],
            ),
            make_user(tool_results=[("tu_001", "File A created")]),
            make_assistant(
                "msg_002",
                thinking="Plan: write file B with no spoken text.",
                tools=[("Write", "tu_002",
                        {"file_path": "/workspace/b.py", "content": "b = 2\n"})],
            ),
            make_user(tool_results=[("tu_002", "File B created")]),
            make_assistant("msg_003", text="Done."),
            make_result(cost_usd=0.10, num_turns=3),
        ]
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _basic_run_result(),
                            run_id="test-run", experiment_id="exp-001")
        agent_steps = [
            s for s in trace["data"]["spans"]
            if _get_span_type(s) == "AGENT" and s["parent_span_id"] is not None
        ]

        def _io(step):
            return (
                json.loads(step["attributes"].get("mlflow.spanInputs", "{}")),
                json.loads(step["attributes"].get("mlflow.spanOutputs", "{}")),
            )

        step_a = next(
            s for s in agent_steps
            if "Writing file A" in _io(s)[1].get("text", "")
        )
        step_b = next(
            s for s in agent_steps
            if "write file B" in _io(s)[1].get("thinking", "")
        )
        step_c = next(
            s for s in agent_steps
            if _io(s)[1].get("text", "") == "Done."
        )

        _, out_a = _io(step_a)
        in_b, out_b = _io(step_b)
        _, out_c = _io(step_c)

        assert out_a.get("text") == "Writing file A now."
        assert "write file B" not in out_a.get("thinking", "")
        assert "Write" in (in_b.get("tools") or [])
        assert out_b.get("text") in (None, "")
        assert "Writing file A" not in (out_b.get("text") or "")
        assert out_c.get("text") == "Done."
        assert "Writing file A" not in (out_c.get("text") or "")
