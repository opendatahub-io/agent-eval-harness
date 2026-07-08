"""Multi-step workflow config parsing and execution helpers."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig, WorkflowConfig, WorkflowStepConfig


def _write(tmp_path, body, name="eval.yaml"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


# ── Config parsing ──────────────────────────────────────────


class TestWorkflowConfigParsing:
    def test_basic_workflow_parses(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: wf-test
workflow:
  steps:
    - id: setup
      type: script
      command: echo hello
    - id: analyze
      type: skill
      skill: test-skill
      arguments: "--input foo"
    - id: check
      type: validate
      command: python validate.py
      retry_step: analyze
      max_retries: 2
      retry_prompt: "fix it: {check.stderr}"
"""))
        assert cfg.is_workflow
        assert not cfg.skill
        assert len(cfg.workflow.steps) == 3

        s0 = cfg.workflow.steps[0]
        assert s0.id == "setup"
        assert s0.type == "script"
        assert s0.command == "echo hello"

        s1 = cfg.workflow.steps[1]
        assert s1.id == "analyze"
        assert s1.type == "skill"
        assert s1.skill == "test-skill"
        assert s1.arguments == "--input foo"

        s2 = cfg.workflow.steps[2]
        assert s2.id == "check"
        assert s2.type == "validate"
        assert s2.validate is not None
        assert s2.validate.retry_step == "analyze"
        assert s2.validate.max_retries == 2

    def test_is_workflow_false_for_single_skill(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: single
skill: test-skill
"""))
        assert not cfg.is_workflow
        assert cfg.skill == "test-skill"

    def test_mutual_exclusivity(self, tmp_path):
        with pytest.raises(ValueError, match="mutually exclusive"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: bad
skill: some-skill
workflow:
  steps:
    - id: s
      type: script
      command: echo
"""))

    def test_duplicate_step_ids_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="duplicate"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: dup
workflow:
  steps:
    - id: a
      type: script
      command: echo 1
    - id: a
      type: script
      command: echo 2
"""))

    def test_retry_step_must_reference_skill(self, tmp_path):
        with pytest.raises(ValueError, match="skill step"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: bad-retry
workflow:
  steps:
    - id: setup
      type: script
      command: echo
    - id: check
      type: validate
      command: python v.py
      retry_step: setup
"""))

    def test_retry_step_must_exist(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: missing
workflow:
  steps:
    - id: check
      type: validate
      command: python v.py
      retry_step: nonexistent
"""))

    def test_skill_step_requires_skill_field(self, tmp_path):
        with pytest.raises(ValueError, match="require 'skill'"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: no-skill
workflow:
  steps:
    - id: bad
      type: skill
      arguments: foo
"""))

    def test_script_step_requires_command(self, tmp_path):
        with pytest.raises(ValueError, match="require 'command'"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: no-cmd
workflow:
  steps:
    - id: bad
      type: script
"""))

    def test_invalid_step_type_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="type must be"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: bad-type
workflow:
  steps:
    - id: x
      type: unknown
      command: echo
"""))

    def test_on_failure_defaults_to_abort(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: defaults
workflow:
  steps:
    - id: s
      type: script
      command: echo
"""))
        assert cfg.workflow.steps[0].on_failure == "abort"

    def test_on_failure_continue(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: cont
workflow:
  steps:
    - id: s
      type: script
      command: echo
      on_failure: continue
"""))
        assert cfg.workflow.steps[0].on_failure == "continue"

    def test_invalid_on_failure_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="on_failure"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: bad
workflow:
  steps:
    - id: s
      type: script
      command: echo
      on_failure: explode
"""))

    def test_continue_session_parsed(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: cont-sess
workflow:
  steps:
    - id: main
      type: skill
      skill: test
    - id: followup
      type: skill
      skill: test
      continue_session: true
"""))
        assert not cfg.workflow.steps[0].continue_session
        assert cfg.workflow.steps[1].continue_session

    def test_step_condition_parsed(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: cond
workflow:
  steps:
    - id: main
      type: skill
      skill: test
    - id: nudge
      type: skill
      skill: test
      condition: "steps.main.timed_out"
"""))
        assert cfg.workflow.steps[1].condition == "steps.main.timed_out"

    def test_empty_steps_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="non-empty"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: empty
workflow:
  steps: []
"""))


# ── Execution helpers ───────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"))


class TestStepConditionEval:
    def test_true_condition(self):
        from execute import StepResult, _eval_step_condition
        results = {
            "main": StepResult(
                step_id="main", step_type="skill",
                exit_code=-1, duration_s=100, timed_out=True),
        }
        assert _eval_step_condition("steps.main.timed_out", results) is True

    def test_false_condition(self):
        from execute import StepResult, _eval_step_condition
        results = {
            "main": StepResult(
                step_id="main", step_type="skill",
                exit_code=0, duration_s=50, timed_out=False),
        }
        assert _eval_step_condition("steps.main.timed_out", results) is False

    def test_exit_code_condition(self):
        from execute import StepResult, _eval_step_condition
        results = {
            "setup": StepResult(
                step_id="setup", step_type="script",
                exit_code=1, duration_s=2),
        }
        assert _eval_step_condition("steps.setup.exit_code != 0", results) is True
        assert _eval_step_condition("steps.setup.exit_code == 0", results) is False

    def test_missing_step_returns_false(self):
        from execute import _eval_step_condition
        assert _eval_step_condition("steps.nonexistent.timed_out", {}) is False

    def test_invalid_expression_returns_false(self):
        from execute import _eval_step_condition
        assert _eval_step_condition("invalid python !!!", {}) is False


class TestStepEnv:
    def test_builds_env_vars(self):
        from execute import StepResult, _step_env
        results = {
            "my-step": StepResult(
                step_id="my-step", step_type="skill",
                exit_code=0, duration_s=42.5, cost_usd=1.23,
                timed_out=False),
        }
        env = _step_env(results)
        assert env["STEP_MY_STEP_EXIT_CODE"] == "0"
        assert env["STEP_MY_STEP_DURATION_S"] == "42.5"
        assert env["STEP_MY_STEP_TIMED_OUT"] == "0"
        assert env["STEP_MY_STEP_COST_USD"] == "1.23"

    def test_timed_out_flag(self):
        from execute import StepResult, _step_env
        results = {
            "slow": StepResult(
                step_id="slow", step_type="skill",
                exit_code=-1, duration_s=3600, timed_out=True),
        }
        env = _step_env(results)
        assert env["STEP_SLOW_TIMED_OUT"] == "1"

    def test_stderr_truncated(self):
        from execute import StepResult, _step_env
        long_err = "x" * 5000
        results = {
            "err": StepResult(
                step_id="err", step_type="script",
                exit_code=1, duration_s=1, stderr=long_err),
        }
        env = _step_env(results)
        assert len(env["STEP_ERR_STDERR"]) == 4096


class TestRunScriptStep:
    def test_successful_script(self, tmp_path):
        from execute import _run_script_step
        from agent_eval.config import WorkflowStepConfig

        step = WorkflowStepConfig(
            id="echo", type="script", command="echo hello")
        result = _run_script_step(step, tmp_path, {})
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_failing_script(self, tmp_path):
        from execute import _run_script_step
        from agent_eval.config import WorkflowStepConfig

        step = WorkflowStepConfig(
            id="fail", type="script", command="exit 42")
        result = _run_script_step(step, tmp_path, {})
        assert result.exit_code == 42

    def test_script_timeout(self, tmp_path):
        from execute import _run_script_step
        from agent_eval.config import WorkflowStepConfig

        step = WorkflowStepConfig(
            id="slow", type="script", command="sleep 60",
            timeout=1)
        result = _run_script_step(step, tmp_path, {})
        assert result.exit_code == -1
        assert result.timed_out

    def test_script_with_case_data_placeholders(self, tmp_path):
        from execute import _run_script_step
        from agent_eval.config import WorkflowStepConfig

        step = WorkflowStepConfig(
            id="echo", type="script",
            command="echo {payload_tag}")
        result = _run_script_step(
            step, tmp_path, {},
            case_data={"payload_tag": "4.18.0-0.nightly-test"})
        assert result.exit_code == 0
        assert "4.18.0-0.nightly-test" in result.stdout


class TestResolveStepArgs:
    def test_step_result_reference(self):
        from execute import StepResult, _resolve_step_args
        results = {
            "validate": StepResult(
                step_id="validate", step_type="script",
                exit_code=1, duration_s=2,
                stderr="missing field: analysis"),
        }
        resolved = _resolve_step_args(
            "Fix: {validate.stderr}", results)
        assert "missing field: analysis" in resolved

    def test_case_data_fallback(self):
        from execute import _resolve_step_args
        resolved = _resolve_step_args(
            "{payload_tag} --snapshot", {},
            case_data={"payload_tag": "4.18.0"})
        assert resolved == "4.18.0 --snapshot"


class TestWorkflowResultJson:
    def test_workflow_result_loaded_in_case_record(self, tmp_path):
        """Verify score.py loads workflow_result.json into the record."""
        case_dir = tmp_path / "cases" / "case-001"
        case_dir.mkdir(parents=True)

        wf = {
            "steps": {
                "setup": {"exit_code": 0, "duration_s": 2, "type": "script",
                          "skipped": False},
                "analyze": {"exit_code": 0, "duration_s": 100, "type": "skill",
                            "cost_usd": 1.5, "skipped": False},
            },
            "total_retries": 1,
            "total_duration_s": 102,
            "total_cost_usd": 1.5,
        }
        (case_dir / "workflow_result.json").write_text(json.dumps(wf))

        from agent_eval.config import EvalConfig
        config = EvalConfig(name="test", dataset=__import__(
            "agent_eval.config", fromlist=["DatasetConfig"]).DatasetConfig())

        from score import load_case_record
        record = load_case_record(case_dir, config)
        assert "workflow" in record
        assert record["workflow"]["total_retries"] == 1
        assert "analyze" in record["workflow"]["steps"]
