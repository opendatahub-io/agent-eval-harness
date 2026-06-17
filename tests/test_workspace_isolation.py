"""Unit tests for workspace isolation (spec 006).

Verifies that the solver workspace does not expose answer keys,
evaluation infrastructure, or awareness that it's being benchmarked.

Source-analysis tests read file contents directly to avoid importing
modules that require Python 3.10+ (Path | str type syntax).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"))

TOOLS_PY = str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts" / "tools.py")
WORKSPACE_PY = Path(__file__).parent.parent / "skills" / "eval-run" / "scripts" / "workspace.py"
PROJECT_ROOT = str(Path(__file__).parent.parent)


@pytest.fixture(scope="module")
def workspace_src():
    """Read workspace.py source once for all source-analysis tests."""
    return WORKSPACE_PY.read_text()


# ── P1: Core isolation ──────────────────────────────────────────


def test_answers_not_in_workspace(workspace_src):
    """answers.yaml must not be copied into the per-case workspace."""
    assert 'shutil.copy2(answers_src, case_ws / "answers.yaml")' not in workspace_src
    assert 'answers_src = case_dir / "answers.yaml"' not in workspace_src


def test_hooks_in_harness_dir(workspace_src):
    """tool_handlers.yaml and tools.py are written to harness dir, not workspace."""
    assert 'harness_dir / "tool_handlers.yaml"' in workspace_src
    assert 'hooks_dir = harness_dir / "hooks"' in workspace_src
    # Old workspace references should be gone
    assert 'hooks_dir = workspace / "hooks"' not in workspace_src
    assert 'workspace / "tool_handlers.yaml"' not in workspace_src


def test_hook_command_points_to_harness(workspace_src):
    """settings.json hook command must reference the harness path, not workspace."""
    assert 'tools_path = shlex.quote(str((hooks_dir / "tools.py").resolve()))' in workspace_src
    assert 'config_path = shlex.quote(str((harness_dir / "tool_handlers.yaml").resolve()))' in workspace_src
    assert 'cmd = f"python3 {tools_path} --config {config_path}"' in workspace_src
    # Old: bare workspace path in command
    assert 'f"python3 {workspace}/hooks/tools.py"' not in workspace_src


# ── P2: Access scoping ──────────────────────────────────────────


def test_additional_dirs_scoped(workspace_src):
    """additionalDirectories must not contain the project root —
    only resolved directory symlink targets."""
    assert '"additionalDirectories", []).append(project_root)' not in workspace_src
    assert "if symlink_dirs:" in workspace_src
    assert "if d not in additional:" in workspace_src


def test_carry_over_skips_additional_dirs(workspace_src):
    """_carry_over_permissions must not copy additionalDirectories
    from the project's settings.json."""
    # The old extend pattern should be gone from _carry_over_permissions
    func_src = workspace_src.split("def _carry_over_permissions")[1].split("\ndef ")[0]
    assert ".extend(" not in func_src
    # Must warn about dropped entries
    assert "dropping" in func_src
    assert "additionalDirectories" in func_src


def test_claude_md_copied_not_symlinked(workspace_src):
    """CLAUDE.md (and any file) must be copied, not symlinked, into the workspace."""
    assert "if target.is_file():" in workspace_src
    assert "shutil.copy2(target, workspace / name)" in workspace_src  # batch mode
    assert "shutil.copy2(target, dest)" in workspace_src  # case mode


def test_skills_not_in_default_symlinks(workspace_src):
    """'skills' must not appear in either default_symlinks list."""
    for m in re.finditer(r'default_symlinks\s*=\s*\[([^\]]+)\]', workspace_src):
        items = m.group(1)
        assert '"skills"' not in items and "'skills'" not in items, \
            f"'skills' found in default_symlinks: [{items}]"


# ── P3: Decontamination ─────────────────────────────────────────


def test_harness_prompt_neutral():
    """_HARNESS_SYSTEM_PROMPT must not contain evaluation-revealing language."""
    execute_src = (Path(__file__).parent.parent
                   / "skills" / "eval-run" / "scripts" / "execute.py").read_text()
    m = re.search(r'_HARNESS_SYSTEM_PROMPT\s*=\s*\((.*?)\)', execute_src, re.DOTALL)
    assert m, "_HARNESS_SYSTEM_PROMPT not found in execute.py"
    prompt = m.group(1).lower()
    for word in ("evaluation", "harness", "benchmark", "eval "):
        assert word not in prompt, \
            f"System prompt contains '{word}'"


def test_safe_env_no_eval_runs_dir():
    """AGENT_EVAL_RUNS_DIR must not be in _SAFE_ENV_KEYS."""
    claude_code_src = (Path(__file__).parent.parent
                       / "agent_eval" / "agent" / "claude_code.py").read_text()
    # Find the _SAFE_ENV_KEYS block
    m = re.search(r'_SAFE_ENV_KEYS\s*=\s*\{(.*?)\}', claude_code_src, re.DOTALL)
    assert m, "_SAFE_ENV_KEYS not found"
    assert "AGENT_EVAL_RUNS_DIR" not in m.group(1)


# ── Review fixes: workspace.files and collect.py ────────────────


def test_workspace_files_skips_eval_only():
    """workspace_files.py must filter eval-only files (answers, annotations, etc.)."""
    ws_files_src = (Path(__file__).parent.parent
                    / "skills" / "eval-run" / "scripts" / "workspace_files.py").read_text()
    assert "_EVAL_ONLY_NAMES" in ws_files_src
    assert '"answers"' in ws_files_src
    assert '"annotations"' in ws_files_src
    assert "_is_eval_only" in ws_files_src
    # The "." entry must be rejected
    assert 'entry == "."' in ws_files_src


def test_collect_excludes_claude_md():
    """collect.py _HARNESS_PATHS must include CLAUDE.md to prevent false _modified."""
    collect_src = (Path(__file__).parent.parent
                   / "skills" / "eval-run" / "scripts" / "collect.py").read_text()
    m = re.search(r'_HARNESS_PATHS\s*=\s*\{([^}]+)\}', collect_src, re.DOTALL)
    assert m, "_HARNESS_PATHS not found in collect.py"
    assert '"CLAUDE.md"' in m.group(1), \
        "CLAUDE.md must be in _HARNESS_PATHS to prevent false _modified collection"


# ── P1: Fail-closed hook ────────────────────────────────────────


def test_tools_fail_closed():
    """tools.py with --config pointing to a missing file must return JSON deny."""
    result = subprocess.run(
        [sys.executable, TOOLS_PY,
         "--config", "/nonexistent/tool_handlers.yaml"],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHONPATH": PROJECT_ROOT},
    )
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "not found" in output["hookSpecificOutput"]["reason"].lower()
