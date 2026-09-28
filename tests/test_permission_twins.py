"""Tests for the absolute-workspace twins of relative-path Bash permission rules.

Claude Code matches a Bash rule literally up to its first ``*``, so a project
rule ``Bash(python3 scripts/foo.py *)`` never matches the
``python3 <workspace>/scripts/foo.py ...`` form weaker models emit for the same
script, and in headless mode that denial ends the run.  The directory the agent
runs in is known when the harness writes the run settings, so every
settings-assembly path of the local runner (batch and per-case: the isolated
workspace; repo mode: the project checkout; project carry-over and eval.yaml
``permissions``) adds a literal twin per relative-path Bash rule — allow twins
only when the target exists under the workspace, deny twins always — never a
wildcard, never for a rule that is already absolute, has no path, globs it, or
puts a ``*`` before it.

All tests run under a temp HOME and a temp project; none reads ``~/.claude``.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig
import workspace  # skills/eval-run/scripts (sys.path via conftest)


PROJECT_ALLOW = [
    "Bash(python3 scripts/foo.py *)",         # trailing wildcard: the common shape
    "Bash(bash scripts/run.sh)",              # exact rule
    "Bash(bash scripts/run.sh *)",
    "Bash(scripts/run.sh *)",                 # the path is the first token
    "Bash(python3 ./scripts/foo.py *)",       # ./-prefixed: same file, same twin
    "Bash(python3 scripts/foo.py:*)",         # the :* trailing-wildcard form
    "Bash(ls scripts/)",                      # a directory, trailing slash kept verbatim
    "Bash(python3 scripts/missing.py *)",     # nothing under the workspace: untouched
    "Bash(python3 /opt/tool.py *)",           # already absolute
    "Bash(python3 * scripts/foo.py)",         # a wildcard before the path: never widened
    "Bash(python3 scripts/*.py *)",           # globbed path: never widened
    "Bash(python3 ../scripts/foo.py *)",      # leaves the workspace: untouched
    "Bash(ls)",
    "Bash(git status)",
    "Edit(artifacts/**)",
    "Skill",
]
PROJECT_DENY = [
    "Bash(python3 scripts/foo.py --purge *)",  # narrows an allowed script
    "Bash(python3 scripts/nuke.py *)",         # not under the workspace: a deny twin still lands
    "Bash(python3 scripts/*)",                 # a globbed deny keeps covering the allow twins
    "Bash(python3 * scripts/foo.py)",          # a wildcard before the path is kept for a deny
    "Bash(curl *)",
]


def _project(tmp_path, monkeypatch, allow=PROJECT_ALLOW, deny=None):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts" / "foo.py").write_text("print(1)\n")
    (project / "scripts" / "run.sh").write_text("true\n")
    (project / ".claude").mkdir()
    perms = {"allow": allow}
    if deny:
        perms["deny"] = deny
    (project / ".claude" / "settings.json").write_text(json.dumps({"permissions": perms}))
    monkeypatch.chdir(project)
    return project


def _workspace(tmp_path, project, name="ws"):
    """A workspace with the project's scripts/ symlinked in, as workspace.py does."""
    ws = tmp_path / name
    ws.mkdir()
    (ws / "scripts").symlink_to((project / "scripts").resolve())
    return ws


def _config(directory, extra_yaml="", runner_yaml="  type: claude-code\n"):
    p = directory / "eval.yaml"
    p.write_text("name: t\nexecution:\n  skill: s\n" + extra_yaml + "runner:\n" + runner_yaml)
    return EvalConfig.from_yaml(p)


def _settings(ws):
    return json.loads((ws / ".claude" / "settings.json").read_text())["permissions"]


def _allow_twins(base):
    return [
        f"Bash(python3 {base}/scripts/foo.py *)",
        f"Bash(bash {base}/scripts/run.sh)",
        f"Bash(bash {base}/scripts/run.sh *)",
        f"Bash({base}/scripts/run.sh *)",
        f"Bash(python3 {base}/scripts/foo.py:*)",
        f"Bash(ls {base}/scripts/)",
    ]


def test_twins_are_literal_and_only_for_workspace_resources(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    ws = _workspace(tmp_path, project)
    allow = workspace._expand_workspace_bash_permissions(list(PROJECT_ALLOW), ws)

    assert allow[: len(PROJECT_ALLOW)] == PROJECT_ALLOW, "originals kept, in order"
    extras = allow[len(PROJECT_ALLOW):]
    # tmp_path is already resolved, so exactly one twin per eligible rule
    assert extras == _allow_twins(ws), extras
    for rule in extras:
        body = rule[len("Bash("):-1]
        path = next(t for t in body.split(" ") if t.startswith("/"))
        assert "*" not in body.split(path)[0], "no wildcard before the path"
        assert Path(path.removesuffix(":*")).exists()
    assert not [r for r in extras if "missing.py" in r or "/opt/tool.py" in r or "*.py" in r or ".." in r]


def test_deny_twins_need_no_existing_target_and_keep_their_globs():
    deny = workspace._expand_workspace_bash_permissions(list(PROJECT_DENY), Path("/ws"), require_exists=False)
    assert deny == PROJECT_DENY + [
        "Bash(python3 /ws/scripts/foo.py --purge *)",
        "Bash(python3 /ws/scripts/nuke.py *)",
        "Bash(python3 /ws/scripts/*)",
        "Bash(python3 * /ws/scripts/foo.py)",
    ]


def test_a_globbed_relative_deny_still_covers_the_allow_twins(tmp_path, monkeypatch):
    """Deny beats allow in Claude Code, but only when a deny rule matches the
    command text: the allow twin `python3 <ws>/scripts/foo.py` would slip past a
    relative `Bash(python3 scripts/*)` unless that deny is twinned as well."""
    project = _project(tmp_path, monkeypatch, allow=["Bash(python3 scripts/foo.py *)"],
                       deny=["Bash(python3 scripts/*)"])
    ws = _workspace(tmp_path, project)
    workspace._setup_subagent_only_hook(ws, _config(project))
    perms = _settings(ws)
    assert f"Bash(python3 {ws}/scripts/foo.py *)" in perms["allow"]
    assert f"Bash(python3 {ws}/scripts/*)" in perms["deny"]
    # and an allow rule with a globbed path is still never widened
    assert not [r for r in perms["allow"] if "*" in r.split(" ")[1] if " " in r]


def test_without_a_workspace_nothing_changes():
    assert workspace._expand_workspace_bash_permissions(list(PROJECT_ALLOW), None) == PROJECT_ALLOW


def test_twins_are_not_duplicated(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    ws = _workspace(tmp_path, project)
    once = workspace._expand_workspace_bash_permissions(list(PROJECT_ALLOW), ws)
    assert workspace._expand_workspace_bash_permissions(list(once), ws) == once


def test_a_symlinked_workspace_path_gets_both_forms(tmp_path, monkeypatch):
    """The batch workspace is resolved before the builders run; a caller that
    passes a symlink (macOS /var -> /private/var) gets the literal form it gave
    and the real path the CLI reports as cwd."""
    project = _project(tmp_path, monkeypatch)
    real_ws = _workspace(tmp_path, project, name="real_ws")
    link = tmp_path / "ws_link"
    link.symlink_to(real_ws)
    allow = workspace._expand_workspace_bash_permissions(["Bash(python3 scripts/foo.py *)"], link)
    assert allow == [
        "Bash(python3 scripts/foo.py *)",
        f"Bash(python3 {link}/scripts/foo.py *)",
        f"Bash(python3 {os.path.realpath(link)}/scripts/foo.py *)",
    ]
    assert os.path.realpath(link) != str(link)


@pytest.mark.parametrize("base", ["/tmp/agent eval/ws", "/tmp/agent*eval/ws", "/tmp/a'b/ws"])
def test_a_workspace_path_that_cannot_be_spliced_disables_the_twins(base, capsys):
    rules = ["Bash(python3 scripts/foo.py *)"]
    assert workspace._expand_workspace_bash_permissions(rules, Path(base), require_exists=False) == rules
    assert "not adding absolute-path permission twins" in capsys.readouterr().err


def test_batch_settings_carry_allow_and_deny_twins(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch, deny=PROJECT_DENY)
    ws = _workspace(tmp_path, project)
    workspace._setup_subagent_only_hook(ws, _config(project))
    perms = _settings(ws)
    assert set(_allow_twins(ws)) <= set(perms["allow"])
    assert set(PROJECT_ALLOW) <= set(perms["allow"])
    assert f"Bash(python3 {ws}/scripts/foo.py --purge *)" in perms["deny"]
    assert f"Bash(python3 {ws}/scripts/nuke.py *)" in perms["deny"]
    assert f"Bash(python3 {ws}/scripts/*)" in perms["deny"]
    assert "Bash(curl *)" in perms["deny"]


def test_eval_yaml_allow_rules_get_twins_too(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch, allow=["Bash(ls)"])
    ws = _workspace(tmp_path, project)
    config = _config(project, "permissions:\n  allow:\n    - Bash(python3 scripts/foo.py *)\n    - Agent\n")
    workspace._setup_subagent_only_hook(ws, config)
    allow = _settings(ws)["allow"]
    assert f"Bash(python3 {ws}/scripts/foo.py *)" in allow
    assert "Agent" in allow and "Bash(ls)" in allow


def test_tool_interception_settings_carry_the_twins(tmp_path, monkeypatch):
    """The builder the interception evals run through: generate_interception
    writes eval.yaml's allow and deny first; the carry-over and the merge add
    the twins for both sources."""
    project = _project(tmp_path, monkeypatch, deny=["Bash(python3 scripts/foo.py --purge *)"])
    ws = _workspace(tmp_path, project)
    config = _config(project,
                     "  arguments: ''\n"
                     "inputs:\n  tools:\n    - match: AskUserQuestion\n      prompt: answer yes\n"
                     "permissions:\n  allow:\n    - Bash(bash scripts/run.sh *)\n"
                     "  deny:\n    - Bash(bash scripts/run.sh --force *)\n")
    workspace._setup_tool_hooks(ws, config)
    perms = _settings(ws)
    assert f"Bash(python3 {ws}/scripts/foo.py *)" in perms["allow"], "project carry-over"
    assert f"Bash(bash {ws}/scripts/run.sh *)" in perms["allow"], "eval.yaml permissions.allow"
    assert f"Bash(python3 {ws}/scripts/foo.py --purge *)" in perms["deny"], "project deny"
    assert f"Bash(bash {ws}/scripts/run.sh --force *)" in perms["deny"], "eval.yaml permissions.deny"


def test_repo_mode_settings_root_the_twins_at_the_project(tmp_path, monkeypatch):
    """In repo mode the agent runs in the project checkout and case_ws holds
    only its I/O, so the twins name the project's scripts, not case_ws."""
    project = _project(tmp_path, monkeypatch, deny=PROJECT_DENY)
    case_ws = tmp_path / "run" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    config = _config(project, runner_yaml="  type: claude-code\n  workspace_mode: repo\n")
    workspace._create_repo_mode_settings(case_ws, project, config)
    perms = _settings(case_ws)
    assert set(_allow_twins(project)) <= set(perms["allow"])
    assert f"Bash(python3 {project}/scripts/foo.py --purge *)" in perms["deny"]
    assert not [r for r in perms["allow"] + perms["deny"] if str(case_ws) in r and r.startswith("Bash(")]
