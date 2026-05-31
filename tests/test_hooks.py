"""Tests for lifecycle hook execution."""

import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig, HookEntry, HooksConfig
from agent_eval.hooks import HookError, HookResult, run_hooks, run_hooks_safe


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

def _write(tmp_path, body):
    p = tmp_path / "eval.yaml"
    p.write_text(body)
    return p


def test_hooks_config_parses_all_phases(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, textwrap.dedent("""\
        name: t
        skill: s
        hooks:
          before_all:
            - command: "echo setup"
              timeout: 60
              description: "Start services"
            - command: "echo ready"
          before_each:
            - command: "echo case-setup"
              condition: "test -f snapshot.tar.gz"
          after_each:
            - command: "echo case-teardown"
              on_failure: continue
          before_scoring:
            - command: "python3 aggregate.py"
              timeout: 30
          after_all:
            - command: "echo cleanup"
              on_failure: continue
              description: "Tear down services"
    """)))
    assert len(cfg.hooks.before_all) == 2
    assert cfg.hooks.before_all[0].command == "echo setup"
    assert cfg.hooks.before_all[0].timeout == 60
    assert cfg.hooks.before_all[0].description == "Start services"
    assert cfg.hooks.before_all[1].command == "echo ready"
    assert cfg.hooks.before_all[1].timeout == 120  # default

    assert len(cfg.hooks.before_each) == 1
    assert cfg.hooks.before_each[0].condition == "test -f snapshot.tar.gz"

    assert len(cfg.hooks.after_each) == 1
    assert cfg.hooks.after_each[0].on_failure == "continue"

    assert len(cfg.hooks.before_scoring) == 1
    assert cfg.hooks.before_scoring[0].timeout == 30

    assert len(cfg.hooks.after_all) == 1
    assert cfg.hooks.after_all[0].description == "Tear down services"


def test_hooks_config_defaults_to_empty(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: t\nskill: s\n"))
    assert cfg.hooks.before_all == []
    assert cfg.hooks.before_each == []
    assert cfg.hooks.after_each == []
    assert cfg.hooks.before_scoring == []
    assert cfg.hooks.after_all == []


def test_hooks_config_empty_block(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, textwrap.dedent("""\
        name: t
        skill: s
        hooks:
    """)))
    assert cfg.hooks.before_all == []


# ---------------------------------------------------------------------------
# Hook execution
# ---------------------------------------------------------------------------

def test_successful_hook(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="echo hello")]
    env = dict(os.environ)
    results = run_hooks(entries, env, tmp_path, log_dir, "before_all")
    assert len(results) == 1
    assert results[0].exit_code == 0
    assert not results[0].skipped
    assert not results[0].timed_out
    assert results[0].log_file.exists()
    assert "hello" in results[0].log_file.read_text()


def test_failing_hook_raises(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="exit 1", on_failure="fail")]
    with pytest.raises(HookError) as exc_info:
        run_hooks(entries, dict(os.environ), tmp_path, log_dir, "before_all")
    assert exc_info.value.exit_code == 1
    assert exc_info.value.phase == "before_all"


def test_failing_hook_continue(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [
        HookEntry(command="exit 1", on_failure="continue"),
        HookEntry(command="echo second"),
    ]
    results = run_hooks(entries, dict(os.environ), tmp_path, log_dir, "after_each")
    assert len(results) == 2
    assert results[0].exit_code == 1
    assert results[1].exit_code == 0


def test_failing_hook_fail_stops_remaining(tmp_path):
    log_dir = tmp_path / "hooks"
    marker = tmp_path / "should_not_exist"
    entries = [
        HookEntry(command="exit 1", on_failure="fail"),
        HookEntry(command=f"touch {marker}"),
    ]
    with pytest.raises(HookError):
        run_hooks(entries, dict(os.environ), tmp_path, log_dir, "before_all")
    assert not marker.exists()


def test_condition_pass(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="echo ran", condition="true")]
    results = run_hooks(entries, dict(os.environ), tmp_path, log_dir, "before_each")
    assert len(results) == 1
    assert not results[0].skipped
    assert results[0].exit_code == 0


def test_condition_fail_skips(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="echo should-not-run", condition="false")]
    results = run_hooks(entries, dict(os.environ), tmp_path, log_dir, "before_each")
    assert len(results) == 1
    assert results[0].skipped
    assert results[0].log_file is None


def test_condition_checks_file_existence(tmp_path):
    log_dir = tmp_path / "hooks"
    target = tmp_path / "marker.txt"
    entries = [HookEntry(
        command="echo found",
        condition=f"test -f {target}",
    )]

    # Without the file: skipped
    results = run_hooks(entries, dict(os.environ), tmp_path, log_dir, "before_each")
    assert results[0].skipped

    # With the file: runs
    target.write_text("x")
    results = run_hooks(entries, dict(os.environ), tmp_path, log_dir, "before_each")
    assert not results[0].skipped
    assert results[0].exit_code == 0


def test_timeout_kills_hook(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="sleep 60", timeout=1, on_failure="continue")]
    results = run_hooks(entries, dict(os.environ), tmp_path, log_dir, "before_all")
    assert len(results) == 1
    assert results[0].timed_out
    assert results[0].duration_s < 10  # Should be ~1s, not 60s


def test_timeout_raises_when_fail(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="sleep 60", timeout=1, on_failure="fail")]
    with pytest.raises(HookError) as exc_info:
        run_hooks(entries, dict(os.environ), tmp_path, log_dir, "before_all")
    assert exc_info.value.timed_out


def test_env_vars_available(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="echo $MY_TEST_VAR")]
    env = {**os.environ, "MY_TEST_VAR": "hook_value_42"}
    results = run_hooks(entries, env, tmp_path, log_dir, "before_all")
    assert results[0].exit_code == 0
    assert "hook_value_42" in results[0].log_file.read_text()


def test_cwd_is_respected(tmp_path):
    log_dir = tmp_path / "hooks"
    work_dir = tmp_path / "workdir"
    work_dir.mkdir()
    entries = [HookEntry(command="pwd")]
    results = run_hooks(entries, dict(os.environ), work_dir, log_dir, "before_each")
    assert results[0].exit_code == 0
    assert str(work_dir) in results[0].log_file.read_text()


def test_case_id_in_log_filename(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="echo case")]
    results = run_hooks(entries, dict(os.environ), tmp_path, log_dir,
                        "before_each", case_id="case-001")
    assert results[0].log_file.name == "before_each.case-001.0.log"


def test_log_filename_without_case_id(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="echo global")]
    results = run_hooks(entries, dict(os.environ), tmp_path, log_dir, "before_all")
    assert results[0].log_file.name == "before_all.0.log"


def test_multiline_command(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="echo line1\necho line2")]
    results = run_hooks(entries, dict(os.environ), tmp_path, log_dir, "before_all")
    assert results[0].exit_code == 0
    content = results[0].log_file.read_text()
    assert "line1" in content
    assert "line2" in content


def test_empty_entries_returns_empty(tmp_path):
    log_dir = tmp_path / "hooks"
    results = run_hooks([], dict(os.environ), tmp_path, log_dir, "before_all")
    assert results == []


# ---------------------------------------------------------------------------
# run_hooks_safe
# ---------------------------------------------------------------------------

def test_invalid_on_failure_rejected(tmp_path):
    with pytest.raises(ValueError, match="on_failure must be"):
        EvalConfig.from_yaml(_write(tmp_path, textwrap.dedent("""\
            name: t
            skill: s
            hooks:
              before_all:
                - command: "echo x"
                  on_failure: fial
        """)))


def test_negative_timeout_rejected(tmp_path):
    with pytest.raises(ValueError, match="timeout must be a positive"):
        EvalConfig.from_yaml(_write(tmp_path, textwrap.dedent("""\
            name: t
            skill: s
            hooks:
              before_all:
                - command: "echo x"
                  timeout: -1
        """)))


def test_zero_timeout_rejected(tmp_path):
    with pytest.raises(ValueError, match="timeout must be a positive"):
        EvalConfig.from_yaml(_write(tmp_path, textwrap.dedent("""\
            name: t
            skill: s
            hooks:
              before_all:
                - command: "echo x"
                  timeout: 0
        """)))


def test_case_id_sanitized_in_log_filename(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [HookEntry(command="echo safe")]
    results = run_hooks(entries, dict(os.environ), tmp_path, log_dir,
                        "before_each", case_id="../../etc/passwd")
    # Log file must stay inside log_dir regardless of malicious case_id
    assert results[0].log_file.resolve().parent == log_dir.resolve()
    assert "/" not in results[0].log_file.name


def test_run_hooks_safe_never_raises(tmp_path):
    log_dir = tmp_path / "hooks"
    entries = [
        HookEntry(command="exit 1", on_failure="fail"),
        HookEntry(command="echo second"),
    ]
    results = run_hooks_safe(entries, dict(os.environ), tmp_path, log_dir, "after_all")
    assert len(results) == 2
    assert results[0].exit_code == 1
    assert results[1].exit_code == 0
