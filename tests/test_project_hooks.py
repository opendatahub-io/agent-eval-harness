"""Tests for carrying the project's settings hooks into the run settings.

The project's ``.claude/settings.json`` (the file the permission carry-over
already reads) may declare ``hooks`` the skill under test relies on — a
``SessionStart``/``compact`` recovery banner, a ``Stop`` guard.  The harness
appends them per event after its own hooks (``SubagentStop`` capture,
``PreToolUse`` interception) and before ``runner.settings``, in every
settings-assembly path.  ``execution.project_hooks: false`` opts out.

All tests run under a temp HOME and a temp project; none reads the real
``~/.claude``.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig
import workspace  # skills/eval-run/scripts (sys.path via conftest)


COMPACT_GROUP = {
    "matcher": "compact",
    "hooks": [{"type": "command",
               "command": "python3 scripts/pipeline_state.py post-compact-hook"}],
}
STOP_GROUP = {
    "hooks": [{"type": "command", "command": "python3 scripts/stop_guard.py"}],
}
PROJECT_SUBAGENT_GROUP = {
    "hooks": [{"type": "command", "command": "echo project-subagent-stop"}],
}
PROJECT_HOOKS = {
    "SessionStart": [COMPACT_GROUP],
    "Stop": [STOP_GROUP],
    "SubagentStop": [PROJECT_SUBAGENT_GROUP],
}


def _project(tmp_path, monkeypatch, settings=None, text=None):
    """A temp project as cwd (the carry-over reads cwd/.claude/settings.json)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    if settings is not None or text is not None:
        target = project / ".claude" / "settings.json"
        target.parent.mkdir(parents=True)
        target.write_text(text if text is not None else json.dumps(settings))
    monkeypatch.chdir(project)
    return project


def _config(directory, execution_yaml="", runner_yaml="  type: claude-code\n"):
    p = directory / "eval.yaml"
    p.write_text("name: t\nexecution:\n  skill: s\n" + execution_yaml
                 + "runner:\n" + runner_yaml)
    return EvalConfig.from_yaml(p)


def _is_harness_subagent_stop(group):
    return any("subagent_stop.py" in h.get("command", "")
               for h in group.get("hooks", []))


# ── Batch path (no tool interception) ───────────────────────────────────


def test_project_hooks_appended_after_harness_hooks(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch,
                       {"permissions": {"allow": ["Bash(ls)"]}, "hooks": PROJECT_HOOKS})
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace._setup_subagent_only_hook(ws, _config(project))
    hooks = json.loads((ws / ".claude" / "settings.json").read_text())["hooks"]

    # Events the harness does not own arrive verbatim.
    assert hooks["SessionStart"] == [COMPACT_GROUP]
    assert hooks["Stop"] == [STOP_GROUP]
    # On a shared event the harness's own hook keeps running first.
    assert _is_harness_subagent_stop(hooks["SubagentStop"][0])
    assert hooks["SubagentStop"][1:] == [PROJECT_SUBAGENT_GROUP]


def test_project_hooks_opt_out(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch, {"hooks": PROJECT_HOOKS})
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace._setup_subagent_only_hook(
        ws, _config(project, "  project_hooks: false\n"))
    hooks = json.loads((ws / ".claude" / "settings.json").read_text())["hooks"]

    assert set(hooks) == {"SubagentStop"}
    assert len(hooks["SubagentStop"]) == 1
    assert _is_harness_subagent_stop(hooks["SubagentStop"][0])


def test_project_hooks_precede_runner_settings_hooks(tmp_path, monkeypatch):
    """runner.settings is merged last: its hooks land after the project's."""
    project = _project(tmp_path, monkeypatch, {"hooks": PROJECT_HOOKS})
    ws = tmp_path / "ws"
    ws.mkdir()
    config = _config(
        project,
        runner_yaml=(
            "  type: claude-code\n"
            "  settings:\n"
            "    hooks:\n"
            "      SessionStart:\n"
            "        - matcher: startup\n"
            "          hooks:\n"
            "            - type: command\n"
            "              command: echo runner-settings\n"))
    workspace._setup_subagent_only_hook(ws, config)
    hooks = json.loads((ws / ".claude" / "settings.json").read_text())["hooks"]

    assert hooks["SessionStart"][0] == COMPACT_GROUP
    assert hooks["SessionStart"][1]["matcher"] == "startup"


def test_no_project_settings_leaves_harness_hooks_alone(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace._setup_subagent_only_hook(ws, _config(project))
    hooks = json.loads((ws / ".claude" / "settings.json").read_text())["hooks"]

    assert set(hooks) == {"SubagentStop"}
    assert len(hooks["SubagentStop"]) == 1


# ── Repo-mode path ──────────────────────────────────────────────────────


def test_project_hooks_carried_in_repo_mode(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch, {"hooks": PROJECT_HOOKS})
    case_ws = tmp_path / "run" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    config = _config(project, runner_yaml=(
        "  type: claude-code\n"
        "  workspace_mode: repo\n"))
    workspace._create_repo_mode_settings(case_ws, project, config)
    settings = json.loads((case_ws / ".claude" / "settings.json").read_text())
    hooks = settings["hooks"]

    assert hooks["SessionStart"] == [COMPACT_GROUP]
    assert _is_harness_subagent_stop(hooks["SubagentStop"][0])
    assert hooks["SubagentStop"][1:] == [PROJECT_SUBAGENT_GROUP]
    # The repo write-protection is untouched by the carry-over.
    assert f"Write({project}/**)" in settings["permissions"]["deny"]


# ── Helper-level: coexistence and malformed input ────────────────────────


def test_project_hooks_coexist_with_tool_interception(tmp_path, monkeypatch):
    """PreToolUse interception groups stay first; the project's follow."""
    project_pre = {"matcher": "Bash",
                   "hooks": [{"type": "command", "command": "echo project-pre"}]}
    project = _project(tmp_path, monkeypatch, {"hooks": {"PreToolUse": [project_pre]}})
    harness_pre = {"matcher": "AskUserQuestion",
                   "hooks": [{"type": "command", "command": "python3 hooks/tools.py"}]}
    settings = {"hooks": {"PreToolUse": [harness_pre]}}

    workspace._carry_over_hooks(settings, _config(project))

    assert settings["hooks"]["PreToolUse"] == [harness_pre, project_pre]


def test_project_hooks_are_copies(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch, {"hooks": PROJECT_HOOKS})
    config = _config(project)
    first, second = {}, {}
    workspace._carry_over_hooks(first, config)
    workspace._carry_over_hooks(second, config)

    first["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "mutated"
    assert second["hooks"]["SessionStart"] == [COMPACT_GROUP]


@pytest.mark.parametrize("text", [
    "{not json",
    json.dumps([]),                                   # top level is not an object
    json.dumps({"hooks": ["SessionStart"]}),          # hooks is not a mapping
    json.dumps({"hooks": None}),
])
def test_malformed_project_settings_are_ignored(tmp_path, monkeypatch, text):
    project = _project(tmp_path, monkeypatch, text=text)
    harness = {"hooks": [{"type": "command", "command": "harness"}]}
    settings = {"hooks": {"SubagentStop": [harness]}}

    workspace._carry_over_hooks(settings, _config(project))

    assert settings == {"hooks": {"SubagentStop": [harness]}}


def test_malformed_events_are_skipped_individually(tmp_path, monkeypatch):
    """A bad event or group does not take the well-formed ones down with it."""
    project = _project(tmp_path, monkeypatch, {"hooks": {
        "SessionStart": [COMPACT_GROUP, "not-a-group", 3],
        "Stop": {"hooks": []},                        # event is not a list
        "Notification": [],
    }})
    settings = {}

    workspace._carry_over_hooks(settings, _config(project))

    assert settings == {"hooks": {"SessionStart": [COMPACT_GROUP]}}


# ── Config parsing ──────────────────────────────────────────────────────


def test_project_hooks_defaults_to_true(tmp_path):
    assert _config(tmp_path).execution.project_hooks is True
    assert _config(tmp_path, "  project_hooks: false\n").execution.project_hooks is False


@pytest.mark.parametrize("value", ['"yes"', "1", "null"])
def test_project_hooks_must_be_boolean(tmp_path, value):
    with pytest.raises(ValueError, match="execution.project_hooks must be a boolean"):
        _config(tmp_path, f"  project_hooks: {value}\n")
