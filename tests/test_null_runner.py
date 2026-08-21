"""Tests for the null (do-nothing) runner — the dataset solvability probe.

Covers the RunResult contract, the RUNNERS registry entry, from_config
override tolerance (the exact kwarg set execute.py passes), instant
zero-cost execution with no workspace writes, and the config-load rejection
of ``runner.type: "null"`` (the probe is CLI-only: ``--agent null``).
"""

import time

import pytest
import yaml

from agent_eval.agent import RUNNERS, NullRunner
from agent_eval.agent.base import EvalRunner, RunResult
from agent_eval.config import EvalConfig


def _write(tmp_path, text):
    path = tmp_path / "eval.yaml"
    path.write_text(text)
    return path


def _minimal_config(tmp_path):
    return EvalConfig.from_yaml(_write(tmp_path, yaml.safe_dump({
        "name": "null-test",
        "execution": {"skill": "s"},
    })))


# ---------------------------------------------------------------------------
# Registry + exports
# ---------------------------------------------------------------------------

def test_null_is_registered():
    assert RUNNERS["null"] is NullRunner


def test_null_runner_is_exported():
    import agent_eval.agent as agent_pkg
    assert "NullRunner" in agent_pkg.__all__
    assert issubclass(NullRunner, EvalRunner)


# ---------------------------------------------------------------------------
# from_config — ignores every config field and override
# ---------------------------------------------------------------------------

def test_from_config_ignores_full_override_set(tmp_path):
    """The exact kwarg set execute.py passes per case must be accepted
    (and dropped) — the probe has no knobs."""
    config = _minimal_config(tmp_path)
    runner = NullRunner.from_config(
        config,
        log_prefix="eval:case-001",
        subagent_model="sonnet",
        mlflow_experiment="exp",
        mlflow_tracking_uri="http://localhost:5000",
        effort="high",
        permissions={"allow": ["Read"], "deny": []},
    )
    assert isinstance(runner, NullRunner)


def test_from_config_ignores_runner_specific_settings(tmp_path):
    config = EvalConfig.from_yaml(_write(tmp_path, yaml.safe_dump({
        "name": "null-test",
        "execution": {"skill": "s"},
        "runner": {"type": "claude-code", "effort": "high",
                   "settings": {"anything": True},
                   "env": {"FOO": "bar"}},
    })))
    runner = NullRunner.from_config(config)
    assert isinstance(runner, NullRunner)


# ---------------------------------------------------------------------------
# execute — instant, zero-cost, contract-complete, side-effect free
# ---------------------------------------------------------------------------

def test_execute_returns_null_result(tmp_path):
    runner = NullRunner()
    start = time.monotonic()
    result = runner.execute(target="x", args="y", workspace=tmp_path,
                            model="sonnet")
    elapsed = time.monotonic() - start

    assert isinstance(result, RunResult)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.duration_s == 0.0
    assert result.cost_usd == 0.0
    assert result.token_usage is None
    assert result.num_turns == 0
    assert result.resolved_model == "null"
    assert elapsed < 1.0  # returns immediately — nothing is spawned
    assert list(tmp_path.iterdir()) == []  # never touches the workspace


def test_execute_prompt_mode_and_full_signature(tmp_path):
    """target=None (prompt mode) + every optional argument accepted."""
    result = NullRunner().execute(
        target=None, args="do nothing", workspace=tmp_path, model="opus",
        settings_path=tmp_path / "missing-settings.json",
        system_prompt="ignored", max_budget_usd=0.0, timeout_s=1,
        extra_env={"FOO": "bar"})
    assert result.exit_code == 0
    assert result.cost_usd == 0.0
    assert list(tmp_path.iterdir()) == []


def test_name_and_version():
    runner = NullRunner()
    assert runner.name == "null"
    assert runner.version == "1"  # execute.py does getattr(runner, "version")


# ---------------------------------------------------------------------------
# Config-load rejection: runner.type "null" is CLI-only (--agent null)
# ---------------------------------------------------------------------------

def test_config_rejects_literal_null_runner_type(tmp_path):
    with pytest.raises(ValueError, match=r"--agent null"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
runner: {type: "null"}
"""))


def test_config_rejects_null_type_on_step_runner(tmp_path):
    with pytest.raises(ValueError, match=r"--agent null"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  steps:
    - skill: a
    - skill: b
      runner: {type: "null"}
"""))


def test_config_rejects_null_type_on_agent_judge_runner(tmp_path):
    with pytest.raises(ValueError, match=r"--agent null"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
judges:
  - name: j
    agent:
      instructions: judge it
      runner: {type: "null"}
"""))


def test_yaml_null_type_is_just_absent(tmp_path):
    """YAML `type: null` parses to None — treated as an absent key (default
    claude-code), never as the null probe runner."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
runner: {type: null}
"""))
    assert cfg.runner.type == "claude-code"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
