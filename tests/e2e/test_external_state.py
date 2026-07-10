"""E2E tests for external-state field awareness in eval-analyze and eval-dataset.

These tests invoke real Claude API calls and are skipped by default.
Run with: python3 -m pytest tests/ -v -m e2e
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from agent_eval.agent.claude_code import ClaudeCodeRunner

pytestmark = pytest.mark.e2e

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_SKILL = FIXTURES / "fake-jira-skill"

# Tools needed by eval-analyze and eval-dataset skills
SKILL_PERMISSIONS = {
    "allow": [
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
        "Agent", "AskUserQuestion", "Skill",
    ],
}


HARNESS_SKILLS = ["eval-analyze", "eval-dataset"]


def _init_project(tmp_path, repo_root):
    """Set up a temporary project directory with the fake Jira skill."""
    skills_dir = tmp_path / ".claude" / "skills"
    skill_dst = skills_dir / "fake-jira-skill"
    skill_dst.mkdir(parents=True)
    shutil.copy(FAKE_SKILL / "SKILL.md", skill_dst / "SKILL.md")

    # Symlink harness skills so Claude Code discovers them as slash commands
    for skill_name in HARNESS_SKILLS:
        (skills_dir / skill_name).symlink_to(repo_root / "skills" / skill_name)

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_eval_analyze_marks_external_fields(tmp_path, repo_root):
    """eval-analyze should annotate external-state fields with [EXTERNAL]."""
    workspace = _init_project(tmp_path, repo_root)

    runner = ClaudeCodeRunner(
        permissions=SKILL_PERMISSIONS,
        log_prefix="e2e",
    )
    result = runner.run_skill(
        skill_name="eval-analyze",
        args="--skill fake-jira-skill",
        workspace=workspace,
        model="claude-opus-4-6",
        timeout_s=180,
        max_budget_usd=2.0,
    )

    assert result.exit_code == 0, (
        f"eval-analyze failed (exit {result.exit_code}):\n{result.stderr}")

    eval_yaml_path = workspace / "eval.yaml"
    assert eval_yaml_path.exists(), "eval-analyze did not generate eval.yaml"

    config = yaml.safe_load(eval_yaml_path.read_text())
    schema = (config.get("dataset", {}).get("schema", "") or "").lower()

    assert "[external:" in schema, (
        f"dataset.schema missing [EXTERNAL: ...] marker:\n{schema}")


def test_eval_dataset_generates_todo_placeholders(tmp_path, repo_root):
    """eval-dataset should use TODO_ placeholders for [EXTERNAL] fields."""
    workspace = _init_project(tmp_path, repo_root)

    # Write a pre-built eval.yaml with [EXTERNAL] markers already in the schema
    # so this test doesn't depend on eval-analyze's LLM output.
    eval_config = {
        "name": "fake-jira-eval",
        "models": {"skill": "claude-opus-4-6"},
        "execution": {
            "skill": "fake-jira-skill",
            "mode": "case",
            "arguments": "{prompt}",
        },
        "dataset": {
            "path": "eval/dataset/cases",
            "schema": (
                "Each case directory contains:\n"
                "- input.yaml: YAML file with:\n"
                "  - 'project_key' ([EXTERNAL: Jira] — must be a real Jira "
                "project key on the target instance, e.g. RHEL or MYPROJECT)\n"
                "  - 'component' (software component name, e.g. 'auth')\n"
                "  - 'prompt' (the analysis request to send to the skill)\n"
            ),
        },
        "outputs": [
            {"path": "output", "schema": "Coverage report markdown file"},
        ],
        "judges": [],
    }
    (workspace / "eval.yaml").write_text(yaml.dump(eval_config))

    # Create empty dataset directory for bootstrap strategy
    cases_dir = workspace / "eval" / "dataset" / "cases"
    cases_dir.mkdir(parents=True)

    runner = ClaudeCodeRunner(
        permissions=SKILL_PERMISSIONS,
        log_prefix="e2e",
    )
    result = runner.run_skill(
        skill_name="eval-dataset",
        args="--count 2",
        workspace=workspace,
        model="claude-opus-4-6",
        timeout_s=180,
        max_budget_usd=2.0,
    )

    assert result.exit_code == 0, (
        f"eval-dataset failed (exit {result.exit_code}):\n{result.stderr}")

    # Scan generated case directories for TODO_ placeholders
    input_files = list(cases_dir.glob("*/input.yaml"))
    assert len(input_files) > 0, (
        f"eval-dataset generated no cases in {cases_dir}")

    found_todo = False
    for input_file in input_files:
        content = input_file.read_text()
        if "TODO_" in content.upper():
            found_todo = True
            break

    assert found_todo, (
        "No TODO_ placeholder found in any generated input.yaml. "
        "Contents of first case:\n" + input_files[0].read_text())
