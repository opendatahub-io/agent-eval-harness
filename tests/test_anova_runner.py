"""Tests for agent_eval.anova_runner.make_run_fn — the eval-anova run_fn bridge.

Uses a mock runner (no Claude/Vertex) and a mocked LLM judge call (no network),
exercising the real load_judges / load_case_record / collect / flatten path.
"""

import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from agent_eval.agent.base import RunResult
from agent_eval.anova_runner import _score_module, make_run_fn
from agent_eval.config import EvalConfig
from agent_eval.matrix import Condition

# eval-anova orchestrate is a skill script
_scripts = str(Path(__file__).parent.parent / "skills" / "eval-anova" / "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)
from orchestrate import run_cell  # noqa: E402


def _make_eval(tmp_path: Path) -> Path:
    """Write a minimal eval.yaml + one dataset case with an oracle."""
    ds = tmp_path / "dataset" / "task-x"
    ds.mkdir(parents=True)
    (ds / "input.yaml").write_text(yaml.safe_dump({"prompt": "Implement the thing."}))
    (ds / "instruction.txt").write_text("Implement the thing.")
    (ds / "oracle.diff").write_text("--- a/x\n+++ b/x\n+the answer\n")
    (ds / "annotations.yaml").write_text(
        yaml.safe_dump({"oracle": "oracle.diff", "instruction": "instruction.txt"}))

    eval_yaml = tmp_path / "eval.yaml"
    eval_yaml.write_text(textwrap.dedent("""
        name: t
        skill: null
        execution: {mode: case, arguments: "{prompt}", timeout: 60}
        runner: {type: claude-code}
        models: {skill: m, judge: j}
        permissions: {allow: [Write], deny: []}
        dataset: {path: dataset, schema: x}
        outputs:
          - {path: artifacts, schema: x}
        traces: {stdout: false, stderr: true, events: false, metrics: false}
        judges:
          - name: has_code_changes
            check: |
              mods = outputs.get("modified_files") or {}
              total = sum(len(v) for v in mods.values() if isinstance(v, str))
              return (total >= 5), f"{total} chars"
          - name: solution_quality
            feedback_type: int
            prompt: |
              instruction: {{ outputs.annotation_instruction_content }}
              oracle: {{ outputs.annotation_oracle_content }}
              diff: {{ outputs.modified_files }}
    """))
    return eval_yaml


def _mock_runner(monkeypatch, written: dict):
    """Patch ClaudeCodeRunner.run_skill to write a file and return success."""
    from agent_eval.agent.claude_code import ClaudeCodeRunner

    def fake_run_skill(self, skill_name, args, workspace, model, **kw):
        (Path(workspace) / "solution.py").write_text(
            "def f():\n    return 42  # plenty of chars here\n")
        written["model"] = model
        written["args"] = args
        return RunResult(exit_code=0, stdout="", stderr="", duration_s=1.0,
                         cost_usd=0.01)

    monkeypatch.setattr(ClaudeCodeRunner, "run_skill", fake_run_skill)


def _mock_llm(monkeypatch, captured: dict, score_val=4):
    """Patch the score module's LLM call to capture the rendered prompt."""
    score = _score_module()

    def fake_call(prompt, model, system_prompt, images=None, max_tokens=1024):
        captured["prompt"] = prompt
        return f'{{"score": {score_val}, "rationale": "ok"}}'

    monkeypatch.setattr(score, "_call_judge_llm", fake_call)


def test_run_fn_returns_flat_judge_dict(tmp_path, monkeypatch):
    eval_yaml = _make_eval(tmp_path)
    config = EvalConfig.from_yaml(eval_yaml)
    written, captured = {}, {}
    _mock_runner(monkeypatch, written)
    _mock_llm(monkeypatch, captured, score_val=4)

    run_fn = make_run_fn(
        config, runs_dir=tmp_path / "runs", project_root=tmp_path,
        normalize=lambda n, v, t: (v - 1) / 4.0 if n == "solution_quality"
        and isinstance(v, (int, float)) else v,
    )
    flat = run_fn("task-x", model="claude-haiku-4-5")

    assert flat["has_code_changes"] is True          # in-place edit detected
    assert flat["solution_quality"] == pytest.approx(0.75)  # 4 -> (4-1)/4
    assert written["model"] == "claude-haiku-4-5"
    assert written["args"] == "Implement the thing."  # {prompt} resolved


def test_llm_judge_sees_oracle_and_diff(tmp_path, monkeypatch):
    eval_yaml = _make_eval(tmp_path)
    config = EvalConfig.from_yaml(eval_yaml)
    written, captured = {}, {}
    _mock_runner(monkeypatch, written)
    _mock_llm(monkeypatch, captured)

    run_fn = make_run_fn(config, runs_dir=tmp_path / "runs", project_root=tmp_path)
    run_fn("task-x", model="m")

    # The judge prompt must contain the injected oracle + the agent's diff.
    assert "the answer" in captured["prompt"]          # oracle.diff content
    assert "solution.py" in captured["prompt"]         # modified file path
    assert "Implement the thing." in captured["prompt"]  # instruction


def test_run_cell_composite_with_bridge(tmp_path, monkeypatch):
    eval_yaml = _make_eval(tmp_path)
    config = EvalConfig.from_yaml(eval_yaml)
    written, captured = {}, {}
    _mock_runner(monkeypatch, written)
    _mock_llm(monkeypatch, captured, score_val=5)

    run_fn = make_run_fn(
        config, runs_dir=tmp_path / "runs", project_root=tmp_path,
        normalize=lambda n, v, t: (v - 1) / 4.0 if n == "solution_quality"
        and isinstance(v, (int, float)) else v,
    )
    jc = {"has_code_changes": {"type": "boolean", "gate": True},
          "solution_quality": {"type": "numeric"}}
    res = run_cell(Condition(condition_id="c", levels={"model": "m"}),
                   "task-x", 0, {}, jc, run_fn=run_fn)
    assert res.composite == pytest.approx(1.0)  # gate True, score 5 -> 1.0
