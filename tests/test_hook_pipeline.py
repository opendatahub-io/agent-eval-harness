"""Integration test for the full hooks lifecycle pipeline.

Exercises the real harness scripts end-to-end via subprocess:
workspace.py → execute.py (with hooks + CLI runner) → score.py

Uses runner.type=cli so no API keys are needed.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"


def _run_script(script_name, args, cwd, env=None):
    """Run a harness script and return the completed process."""
    cmd = [sys.executable, str(SCRIPTS / script_name)] + args
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd), env=env)


@pytest.fixture
def hook_eval_project(tmp_path):
    """Set up a minimal eval project with hooks at every lifecycle phase."""
    project = tmp_path / "project"
    project.mkdir()

    cases_dir = project / "cases"
    for case_id in ("case-001", "case-002"):
        case = cases_dir / case_id
        case.mkdir(parents=True)
        (case / "input.yaml").write_text(
            yaml.dump({"prompt": f"hello from {case_id}"}))

    eval_config = {
        "name": "hooks-pipeline-test",
        "skill": "test-echo",
        "execution": {
            "mode": "case",
            "arguments": "{prompt}",
        },
        "runner": {
            "type": "cli",
            "command": [
                "bash", "-c",
                "echo PROMPT={args} TEST_VAR=$TEST_VAR",
            ],
        },
        "models": {"skill": "test-model"},
        "dataset": {
            "path": "cases",
            "schema": "Each case has a prompt field",
        },
        "outputs": [
            {"path": "output", "schema": "Text file with echo output"},
        ],
        "hooks": {
            "before_all": [{
                "command": "touch $AGENT_EVAL_WORKSPACE/before_all.marker",
                "description": "Global setup marker",
            }],
            "before_each": [{
                "command": (
                    "touch $CASE_WORKSPACE/before_each.marker\n"
                    "cat > .hook-outputs.yaml <<'HOOKEOF'\n"
                    "env:\n"
                    "  TEST_VAR: hook_injected_value\n"
                    "data:\n"
                    "  setup_ts: '12345'\n"
                    "HOOKEOF\n"
                ),
                "description": "Per-case setup with outputs",
            }],
            "after_each": [{
                "command": "touch $CASE_WORKSPACE/after_each.marker",
                "description": "Per-case cleanup",
                "on_failure": "continue",
            }],
            "before_scoring": [{
                "command": "touch $AGENT_EVAL_WORKSPACE/before_scoring.marker",
                "description": "Pre-scoring hook",
            }],
            "after_all": [{
                "command": "touch $AGENT_EVAL_WORKSPACE/after_all.marker",
                "description": "Final teardown",
                "on_failure": "continue",
            }],
        },
        "judges": [{
            "name": "hook_data_check",
            "description": "Verify hook outputs reached judges",
            "check": (
                'hook_data = outputs.get("hook_outputs", {})\n'
                'if hook_data.get("setup_ts") == "12345":\n'
                '    return True, "Hook data present"\n'
                'return False, f"Missing hook data: {hook_data}"'
            ),
        }],
    }
    (project / "eval.yaml").write_text(
        yaml.dump(eval_config, default_flow_style=False))

    return project


def test_hooks_lifecycle_pipeline(hook_eval_project, tmp_path):
    """Full pipeline: workspace → execute (with hooks) → score → verify."""
    project = hook_eval_project
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_id = "test-run-001"

    env = {**os.environ, "AGENT_EVAL_RUNS_DIR": str(runs_dir)}

    # Step 1: Create workspace
    ws_result = _run_script("workspace.py", [
        "--config", "eval.yaml",
        "--run-id", run_id,
    ], cwd=project, env=env)
    assert ws_result.returncode == 0, f"workspace.py failed:\n{ws_result.stderr}"

    workspace = None
    for line in ws_result.stdout.splitlines():
        if line.startswith("WORKSPACE:"):
            workspace = Path(line.split(":", 1)[1].strip())
            break
    assert workspace and workspace.exists(), \
        f"No workspace found. stdout:\n{ws_result.stdout}"

    # Step 2: Execute with hooks + CLI runner
    # score.py uses _get_runs_dir(config.skill) = AGENT_EVAL_RUNS_DIR/test-echo
    output_dir = runs_dir / "test-echo" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    exec_result = _run_script("execute.py", [
        "--workspace", str(workspace),
        "--skill", "test-echo",
        "--model", "test-model",
        "--output", str(output_dir),
        "--config", str(project / "eval.yaml"),
        "--agent", "cli",
        "--run-id", run_id,
    ], cwd=project, env=env)
    assert exec_result.returncode == 0, \
        f"execute.py failed:\n{exec_result.stderr}"

    # -- Verify all hook phases ran --

    assert (workspace / "before_all.marker").exists(), \
        "before_all hook did not run"
    assert (workspace / "after_all.marker").exists(), \
        "after_all hook did not run"

    for case_id in ("case-001", "case-002"):
        case_ws = workspace / "cases" / case_id
        assert (case_ws / "before_each.marker").exists(), \
            f"before_each did not run for {case_id}"
        assert (case_ws / "after_each.marker").exists(), \
            f"after_each did not run for {case_id}"

    # -- Verify hook env vars were injected into the CLI runner --
    # The CLI runner writes result.txt to the workspace's output/ dir,
    # and execute.py captures stdout. Check stdout.log for the env var.
    for case_id in ("case-001", "case-002"):
        stdout_file = output_dir / "cases" / case_id / "stdout.log"
        assert stdout_file.exists(), f"stdout.log missing for {case_id}"
        content = stdout_file.read_text()
        assert "hook_injected_value" in content, \
            f"Hook env not injected for {case_id}. Got: {content}"

    # -- Verify hook data saved for judges --
    for case_id in ("case-001", "case-002"):
        ho_path = output_dir / "cases" / case_id / "hook_outputs.yaml"
        assert ho_path.exists(), f"hook_outputs.yaml missing for {case_id}"
        ho = yaml.safe_load(ho_path.read_text())
        assert ho.get("setup_ts") == "12345", \
            f"Wrong hook data for {case_id}: {ho}"

    # Step 3: Score (runs before_scoring hooks + judges)
    score_result = _run_script("score.py", [
        "judges",
        "--run-id", run_id,
        "--config", str(project / "eval.yaml"),
        "--workspace", str(workspace),
        "--model", "test-model",
    ], cwd=project, env=env)
    assert score_result.returncode == 0, \
        f"score.py failed:\n{score_result.stderr}"

    assert (workspace / "before_scoring.marker").exists(), \
        "before_scoring hook did not run"

    # -- Verify judge passed (hook data was accessible) --
    summary_path = output_dir / "summary.yaml"
    assert summary_path.exists(), f"No summary.yaml at {summary_path}"
    summary = yaml.safe_load(summary_path.read_text())
    judges = summary.get("judges", {})
    assert "hook_data_check" in judges, f"Judge missing from summary: {summary}"
    assert judges["hook_data_check"]["pass_rate"] == 1.0, \
        f"Judge failed: {judges['hook_data_check']}"
