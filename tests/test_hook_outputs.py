"""Integration tests for hook outputs (.hook-outputs.yaml) flow."""

import json
import os
import sys
import textwrap
import unittest.mock
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

# Neutralize _bootstrap before importing execute.py/score.py — prevents
# os.execv from replacing the pytest process when running under system python.
_real_execv = os.execv
os.execv = lambda *a, **kw: None  # no-op during import

from agent_eval.agent.base import RunResult
from agent_eval.config import EvalConfig, HookEntry, HooksConfig
from agent_eval.hooks import (
    build_hook_env, collect_hook_outputs, inject_hook_env, run_hooks,
    save_hook_data,
)
from execute import _run_single_case
from score import load_case_record

os.execv = _real_execv  # restore


# ---------------------------------------------------------------------------
# Hook writes .hook-outputs.yaml → harness collects it
# ---------------------------------------------------------------------------

def test_hook_writes_outputs_yaml(tmp_path):
    """A before_each hook writes .hook-outputs.yaml; harness collects it."""
    log_dir = tmp_path / "hooks"
    case_ws = tmp_path / "case-ws"
    case_ws.mkdir()

    entries = [HookEntry(
        command=(
            'printf "env:\\n  FIXTURE_URL: https://example.com/123\\n'
            'data:\\n  issue_id: 42\\n" > .hook-outputs.yaml'
        ),
    )]
    run_hooks(entries, dict(os.environ), case_ws, log_dir, "before_each",
              case_id="case-001")

    assert (case_ws / ".hook-outputs.yaml").exists()
    outputs = collect_hook_outputs(case_ws)

    assert outputs["env"]["FIXTURE_URL"] == "https://example.com/123"
    assert outputs["data"]["issue_id"] == 42
    assert not (case_ws / ".hook-outputs.yaml").exists()


def test_hook_writes_outputs_json(tmp_path):
    """Hook writes .hook-outputs.json instead of YAML."""
    log_dir = tmp_path / "hooks"
    case_ws = tmp_path / "case-ws"
    case_ws.mkdir()

    payload = json.dumps({"env": {"API_KEY": "test-key-123"}})
    entries = [HookEntry(
        command=f"echo '{payload}' > .hook-outputs.json",
    )]
    run_hooks(entries, dict(os.environ), case_ws, log_dir, "before_each",
              case_id="case-001")

    outputs = collect_hook_outputs(case_ws)
    assert outputs["env"]["API_KEY"] == "test-key-123"


# ---------------------------------------------------------------------------
# inject_hook_env patches settings.json
# ---------------------------------------------------------------------------

def test_inject_hook_env_creates_settings(tmp_path):
    """Hook env vars are written into .claude/settings.json."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    inject_hook_env(workspace, {"FIXTURE_URL": "https://example.com/1"})

    settings_path = workspace / ".claude" / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert settings["env"]["FIXTURE_URL"] == "https://example.com/1"


def test_inject_hook_env_merges_with_existing(tmp_path):
    """Hook env vars merge into existing settings.json env block."""
    workspace = tmp_path / "workspace"
    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"env": {"EXISTING": "keep"}, "permissions": {"allow": []}}))

    inject_hook_env(workspace, {"NEW_VAR": "added"})

    settings = json.loads((settings_dir / "settings.json").read_text())
    assert settings["env"]["EXISTING"] == "keep"
    assert settings["env"]["NEW_VAR"] == "added"
    assert settings["permissions"] == {"allow": []}


def test_inject_hook_env_noop_when_empty(tmp_path):
    """No settings.json created when env dict is empty."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    inject_hook_env(workspace, {})

    assert not (workspace / ".claude" / "settings.json").exists()


# ---------------------------------------------------------------------------
# save_hook_data round-trip (write + read back as YAML)
# ---------------------------------------------------------------------------

def test_save_hook_data_creates_file(tmp_path):
    """save_hook_data writes hook_outputs.yaml."""
    case_dir = tmp_path / "cases" / "test-case"

    save_hook_data(case_dir, {"issue_id": 42, "repo": "org/test"})

    path = case_dir / "hook_outputs.yaml"
    assert path.exists()
    loaded = yaml.safe_load(path.read_text())
    assert loaded == {"issue_id": 42, "repo": "org/test"}


def test_save_hook_data_noop_when_empty(tmp_path):
    """No file created when data is empty or None."""
    case_dir = tmp_path / "cases" / "test-case"
    case_dir.mkdir(parents=True)

    save_hook_data(case_dir, {})
    save_hook_data(case_dir, None)

    assert not (case_dir / "hook_outputs.yaml").exists()


# ---------------------------------------------------------------------------
# End-to-end: hook → collect → inject → save
# ---------------------------------------------------------------------------

def test_full_hook_outputs_flow(tmp_path):
    """Full flow: hook writes outputs → harness collects, injects env,
    saves data."""
    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    log_dir = tmp_path / "hooks"
    case_output = tmp_path / "output" / "cases" / "case-001"

    # 1. Hook writes .hook-outputs.yaml
    entries = [HookEntry(
        command=(
            'printf "env:\\n  SERVICE_URL: http://localhost:8080\\n'
            'data:\\n  container_id: abc123\\n" > .hook-outputs.yaml'
        ),
    )]
    run_hooks(entries, dict(os.environ), case_ws, log_dir, "before_each",
              case_id="case-001")

    # 2. Harness collects outputs
    outputs = collect_hook_outputs(case_ws)
    assert outputs["env"]["SERVICE_URL"] == "http://localhost:8080"
    assert outputs["data"]["container_id"] == "abc123"

    # 3. Harness injects env into settings.json
    inject_hook_env(case_ws, outputs.get("env"))
    settings = json.loads(
        (case_ws / ".claude" / "settings.json").read_text())
    assert settings["env"]["SERVICE_URL"] == "http://localhost:8080"

    # 4. Harness saves data for judges
    save_hook_data(case_output, outputs.get("data"))
    assert (case_output / "hook_outputs.yaml").exists()
    loaded = yaml.safe_load(
        (case_output / "hook_outputs.yaml").read_text())
    assert loaded["container_id"] == "abc123"


def test_global_and_case_outputs_merge(tmp_path):
    """Global (before_all) and per-case (before_each) outputs merge correctly."""
    global_ws = tmp_path / "workspace"
    global_ws.mkdir()
    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    log_dir = tmp_path / "hooks"

    # before_all hook writes global outputs
    entries_all = [HookEntry(
        command=(
            'printf "env:\\n  SHARED_URL: http://shared\\n'
            'data:\\n  shared_key: global_val\\n" '
            '> "$AGENT_EVAL_WORKSPACE/.hook-outputs.yaml"'
        ),
    )]
    env = {**os.environ, "AGENT_EVAL_WORKSPACE": str(global_ws)}
    run_hooks(entries_all, env, Path.cwd(), log_dir, "before_all")
    global_outputs = collect_hook_outputs(global_ws)

    # before_each hook writes case-specific outputs
    entries_each = [HookEntry(
        command=(
            'printf "env:\\n  CASE_URL: http://case1\\n  SHARED_URL: http://override\\n'
            'data:\\n  case_key: case_val\\n" > .hook-outputs.yaml'
        ),
    )]
    run_hooks(entries_each, env, case_ws, log_dir, "before_each",
              case_id="case-001")
    case_outputs = collect_hook_outputs(case_ws)

    # Merge: case overrides global
    merged_env = {
        **global_outputs.get("env", {}),
        **case_outputs.get("env", {}),
    }
    merged_data = {
        **global_outputs.get("data", {}),
        **case_outputs.get("data", {}),
    }

    assert merged_env["SHARED_URL"] == "http://override"
    assert merged_env["CASE_URL"] == "http://case1"
    assert merged_data["shared_key"] == "global_val"
    assert merged_data["case_key"] == "case_val"


# ---------------------------------------------------------------------------
# _run_single_case integration tests (mock runner, real hooks)
# ---------------------------------------------------------------------------

def _make_mock_runner():
    """Create a mock runner that records call args and returns a RunResult."""
    runner = unittest.mock.MagicMock()
    runner.run_skill.return_value = RunResult(
        exit_code=0, stdout="ok", stderr="", duration_s=1.0,
        cost_usd=0.01, num_turns=1,
    )
    return runner


def _make_config(before_each=None, after_each=None, dataset_path=None):
    """Build an EvalConfig with hooks configured."""
    config = EvalConfig()
    config.skill = "test-skill"
    config.hooks = HooksConfig(
        before_each=before_each or [],
        after_each=after_each or [],
    )
    if dataset_path:
        config.dataset_path = str(dataset_path)
    return config


def test_run_single_case_injects_hook_env(tmp_path):
    """before_each hook writes .hook-outputs.yaml → env injected into
    settings.json before runner.run_skill is called."""
    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    dataset = tmp_path / "dataset"
    (dataset / "case-001").mkdir(parents=True)

    config = _make_config(
        before_each=[HookEntry(
            command=(
                'printf "env:\\n  ISSUE_URL: https://github.com/org/repo/issues/1\\n'
                'data:\\n  repo: org/repo\\n" > .hook-outputs.yaml'
            ),
        )],
        dataset_path=dataset,
    )
    hook_env = build_hook_env(
        workspace=str(tmp_path / "workspace"),
        run_id="test-run",
        config_path="eval.yaml",
        project_root=str(Path.cwd()),
        model="sonnet",
    )

    runner = _make_mock_runner()

    case_id, result = _run_single_case(
        runner, "test-skill", "case-001", case_ws, output_dir,
        "--arg1 val1", "sonnet", None, None, None, 5.0, 600,
        1, 1, config=config, hook_env=hook_env,
    )

    assert case_id == "case-001"
    assert result is not None
    assert result["exit_code"] == 0

    # Verify env was injected into settings.json
    settings_path = case_ws / ".claude" / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert settings["env"]["ISSUE_URL"] == "https://github.com/org/repo/issues/1"

    # Verify settings_path was passed to runner
    call_kwargs = runner.run_skill.call_args
    assert call_kwargs.kwargs.get("settings_path") == settings_path or \
        call_kwargs[1].get("settings_path") == settings_path


def test_run_single_case_saves_hook_data(tmp_path):
    """Hook data is saved to case output dir as hook_outputs.yaml."""
    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    dataset = tmp_path / "dataset"
    (dataset / "case-001").mkdir(parents=True)

    config = _make_config(
        before_each=[HookEntry(
            command=(
                'printf "data:\\n  container_id: abc123\\n  port: 5432\\n"'
                ' > .hook-outputs.yaml'
            ),
        )],
        dataset_path=dataset,
    )
    hook_env = build_hook_env(
        workspace=str(tmp_path / "workspace"),
        run_id="test-run",
        config_path="eval.yaml",
        project_root=str(Path.cwd()),
        model="sonnet",
    )

    _run_single_case(
        _make_mock_runner(), "test-skill", "case-001", case_ws, output_dir,
        "", "sonnet", None, None, None, 5.0, 600,
        1, 1, config=config, hook_env=hook_env,
    )

    # Verify hook data was saved and is loadable by score.py
    case_output = output_dir / "cases" / "case-001"
    record = load_case_record(case_output, EvalConfig())
    assert record["hook_outputs"]["container_id"] == "abc123"
    assert record["hook_outputs"]["port"] == 5432


def test_run_single_case_forward_propagates_env_to_after_each(tmp_path):
    """Env vars from before_each .hook-outputs.yaml are available as
    environment variables in after_each hooks."""
    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    dataset = tmp_path / "dataset"
    (dataset / "case-001").mkdir(parents=True)

    marker = tmp_path / "after_each_saw_env.txt"

    config = _make_config(
        before_each=[HookEntry(
            command=(
                'printf "env:\\n  DYNAMIC_VAR: injected_value\\n"'
                ' > .hook-outputs.yaml'
            ),
        )],
        after_each=[HookEntry(
            command=f'echo "$DYNAMIC_VAR" > {marker}',
        )],
        dataset_path=dataset,
    )
    hook_env = build_hook_env(
        workspace=str(tmp_path / "workspace"),
        run_id="test-run",
        config_path="eval.yaml",
        project_root=str(Path.cwd()),
        model="sonnet",
    )

    _run_single_case(
        _make_mock_runner(), "test-skill", "case-001", case_ws, output_dir,
        "", "sonnet", None, None, None, 5.0, 600,
        1, 1, config=config, hook_env=hook_env,
    )

    assert marker.exists()
    assert marker.read_text().strip() == "injected_value"


def test_run_single_case_merges_global_and_case_outputs(tmp_path):
    """Global hook outputs merge with per-case outputs, case wins on conflict."""
    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    dataset = tmp_path / "dataset"
    (dataset / "case-001").mkdir(parents=True)

    config = _make_config(
        before_each=[HookEntry(
            command=(
                'printf "env:\\n  CASE_VAR: from_case\\n  SHARED: from_case\\n'
                'data:\\n  case_key: case_val\\n  shared_key: from_case\\n"'
                ' > .hook-outputs.yaml'
            ),
        )],
        dataset_path=dataset,
    )
    hook_env = build_hook_env(
        workspace=str(tmp_path / "workspace"),
        run_id="test-run",
        config_path="eval.yaml",
        project_root=str(Path.cwd()),
        model="sonnet",
    )

    global_hook_outputs = {
        "env": {"GLOBAL_VAR": "from_global", "SHARED": "from_global"},
        "data": {"global_key": "global_val", "shared_key": "from_global"},
    }

    _run_single_case(
        _make_mock_runner(), "test-skill", "case-001", case_ws, output_dir,
        "", "sonnet", None, None, None, 5.0, 600,
        1, 1, config=config, hook_env=hook_env,
        global_hook_outputs=global_hook_outputs,
    )

    # Check env: case overrides global for SHARED, both GLOBAL_VAR and CASE_VAR present
    settings = json.loads(
        (case_ws / ".claude" / "settings.json").read_text())
    assert settings["env"]["GLOBAL_VAR"] == "from_global"
    assert settings["env"]["CASE_VAR"] == "from_case"
    assert settings["env"]["SHARED"] == "from_case"

    # Check data: merged and available to judges
    case_output = output_dir / "cases" / "case-001"
    record = load_case_record(case_output, EvalConfig())
    assert record["hook_outputs"]["global_key"] == "global_val"
    assert record["hook_outputs"]["case_key"] == "case_val"
    assert record["hook_outputs"]["shared_key"] == "from_case"


def test_run_single_case_after_each_runs_on_runner_exception(tmp_path):
    """after_each hooks run even when runner.run_skill() raises an exception,
    so cleanup hooks (e.g. deleting ephemeral repos) are guaranteed to fire."""
    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    dataset = tmp_path / "dataset"
    (dataset / "case-001").mkdir(parents=True)

    marker = tmp_path / "after_each_ran.txt"

    config = _make_config(
        after_each=[HookEntry(
            command=f'echo cleanup > {marker}',
        )],
        dataset_path=dataset,
    )
    hook_env = build_hook_env(
        workspace=str(tmp_path / "workspace"),
        run_id="test-run",
        config_path="eval.yaml",
        project_root=str(Path.cwd()),
        model="sonnet",
    )

    runner = _make_mock_runner()
    runner.run_skill.side_effect = RuntimeError("unexpected crash")

    case_id, result = _run_single_case(
        runner, "test-skill", "case-001", case_ws, output_dir,
        "", "sonnet", None, None, None, 5.0, 600,
        1, 1, config=config, hook_env=hook_env,
    )

    # after_each must have run despite the exception
    assert marker.exists(), "after_each hook did not run after runner exception"
    assert marker.read_text().strip() == "cleanup"
    # The case should report as failed
    assert result is None or result["exit_code"] != 0


def test_run_single_case_after_each_runs_on_before_each_failure(tmp_path):
    """after_each hooks run even when a before_each hook fails with
    on_failure: fail, so resources created by earlier hooks get cleaned up."""
    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    dataset = tmp_path / "dataset"
    (dataset / "case-001").mkdir(parents=True)

    marker = tmp_path / "after_each_ran.txt"

    config = _make_config(
        before_each=[
            HookEntry(command="echo setup-resource"),
            HookEntry(command="exit 1", on_failure="fail"),
        ],
        after_each=[HookEntry(
            command=f'echo cleanup > {marker}',
        )],
        dataset_path=dataset,
    )
    hook_env = build_hook_env(
        workspace=str(tmp_path / "workspace"),
        run_id="test-run",
        config_path="eval.yaml",
        project_root=str(Path.cwd()),
        model="sonnet",
    )

    case_id, result = _run_single_case(
        _make_mock_runner(), "test-skill", "case-001", case_ws, output_dir,
        "", "sonnet", None, None, None, 5.0, 600,
        1, 1, config=config, hook_env=hook_env,
    )

    # after_each must have run despite before_each failure
    assert marker.exists(), "after_each hook did not run after before_each failure"
    assert marker.read_text().strip() == "cleanup"
    assert result is None


def test_cli_runner_receives_hook_output_env_vars(tmp_path):
    """Hook output env vars injected via inject_hook_env flow into the CLI
    runner's subprocess environment, not just settings.json."""
    from agent_eval.agent.cli_runner import CliRunner

    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)

    # Inject hook env vars (simulating what the harness does after before_each)
    inject_hook_env(case_ws, {"FIXTURE_URL": "https://example.com/42"})

    # Verify settings.json was written
    settings_path = case_ws / ".claude" / "settings.json"
    assert settings_path.exists()

    # Create a CLI runner and run a command that echoes the env var
    runner = CliRunner(command="echo {agent}")

    # The runner should pick up FIXTURE_URL from settings.json
    # and pass it through to the subprocess environment
    env = runner._build_env(settings_path=settings_path)
    assert env.get("FIXTURE_URL") == "https://example.com/42", \
        "CLI runner _build_env() should read hook env vars from settings.json"


def test_run_single_case_no_hooks_no_side_effects(tmp_path):
    """Without hooks configured, no settings.json or hook_outputs.yaml created."""
    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    config = EvalConfig()
    config.skill = "test-skill"

    _run_single_case(
        _make_mock_runner(), "test-skill", "case-001", case_ws, output_dir,
        "", "sonnet", None, None, None, 5.0, 600,
        1, 1, config=config,
    )

    case_output = output_dir / "cases" / "case-001"
    assert not (case_ws / ".claude" / "settings.json").exists()
    assert not (case_output / "hook_outputs.yaml").exists()
