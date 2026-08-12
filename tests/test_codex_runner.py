"""Tests for the Codex CLI runner."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from agent_eval.agent import RUNNERS
from agent_eval.agent.codex import CodexRunner, _extract_usage, _toml_value
from agent_eval.config import EvalConfig

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH")


def _plugin(tmp_path, *names):
    plugin = tmp_path / "plugin"
    for name in names or ("parent",):
        skill = plugin / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n")
    return plugin


def test_codex_is_registered():
    assert RUNNERS["codex"] is CodexRunner


def test_codex_from_config_accepts_effort_settings_and_stdin_prompt(
        tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, "parent")
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(yaml.safe_dump({
        "name": "codex-test",
        "execution": {"skill": "ci:parent"},
        "runner": {
            "type": "codex",
            "plugin_dirs": [str(plugin)],
            "effort": "xhigh",
            "settings": {"model_reasoning_summary": "concise"},
        },
    }))
    runner = CodexRunner.from_config(EvalConfig.from_yaml(config_path))
    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            return (json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 12, "output_tokens": 3,
                          "cached_input_tokens": 4},
            }) + "\n", "")

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("agent_eval.agent.codex.subprocess.Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = runner.execute(
        "ci:parent", "--flag value", workspace, "gpt-5.6-luna", timeout_s=5)

    assert result.exit_code == 0
    assert result.token_usage == {"input": 12, "output": 3, "cache_read": 4}
    assert not (workspace / ".agents").exists()  # staged copies are disposable
    assert captured["kwargs"]["stdin"] is subprocess.PIPE
    assert captured["input"] == "Use the parent skill with arguments: --flag value"
    command = captured["command"]
    assert command[:2] == ["codex", "exec"]
    assert ["--sandbox", "workspace-write"] == command[
        command.index("--sandbox"):command.index("--sandbox") + 2]
    assert ["--model", "gpt-5.6-luna"] == command[
        command.index("--model"):command.index("--model") + 2]
    assert "model_reasoning_effort=\"xhigh\"" in command
    assert "model_reasoning_summary=\"concise\"" in command
    assert command[-2:] == ["--", "-"]
    assert "--flag value" not in command


def test_codex_permission_mode_intent_mapping(tmp_path, monkeypatch):
    captured = []

    class FakeProcess:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return "", ""

    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen",
        lambda command, **kwargs: captured.append(command) or FakeProcess())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    CodexRunner(permission_mode="plan").execute(None, "p", workspace, "m")
    CodexRunner(permission_mode="bypassPermissions").execute(
        None, "p", workspace, "m")

    assert ["--sandbox", "read-only"] == captured[0][
        captured[0].index("--sandbox"):captured[0].index("--sandbox") + 2]
    assert "--dangerously-bypass-approvals-and-sandbox" in captured[1]
    assert "--sandbox" not in captured[1]


def test_codex_warns_for_untranslatable_permissions_and_budget(
        tmp_path, monkeypatch):
    with pytest.warns(RuntimeWarning, match="tool-level permission"):
        runner = CodexRunner(permissions={"deny": ["WebFetch"]})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    proc = type("FakeProcess", (), {
        "returncode": 0,
        "communicate": lambda self, input=None, timeout=None: ("", ""),
    })()
    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen", lambda *a, **k: proc)
    with pytest.warns(RuntimeWarning, match="does not enforce"):
        result = runner.execute(None, "p", workspace, "m", max_budget_usd=25)
    assert result.exit_code == 0


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh"])
def test_codex_effort_allowlist(effort):
    assert CodexRunner(effort=effort)._effort == effort


def test_codex_rejects_max_effort():
    with pytest.raises(ValueError, match="Invalid effort"):
        CodexRunner(effort="max")


def test_canonical_effort_overrides_legacy_setting():
    runner = CodexRunner(
        effort="xhigh", config_overrides={"model_reasoning_effort": "max"})
    assert runner._config_overrides["model_reasoning_effort"] == "xhigh"


def test_codex_stages_copies_all_sibling_skills_and_cleanup(tmp_path):
    plugin = _plugin(tmp_path, "parent", "dependency")
    runner = CodexRunner(plugin_dirs=[str(plugin)])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    manifest = runner._stage_skills(workspace)
    staged = workspace / ".agents" / "skills"
    assert (staged / "parent" / "SKILL.md").is_file()
    assert (staged / "dependency" / "SKILL.md").is_file()
    assert not (staged / "parent").is_symlink()
    (staged / "parent" / "SKILL.md").write_text("mutated\n")
    assert (plugin / "skills" / "parent" / "SKILL.md").read_text() == "# parent\n"

    runner._cleanup_staged_skills(workspace, manifest)
    assert not (workspace / ".agents").exists()


def test_codex_staging_is_idempotent_and_skips_non_skills(tmp_path):
    plugin = _plugin(tmp_path, "parent")
    (plugin / "skills" / "README.md").write_text("not a skill")
    (plugin / "skills" / "empty").mkdir()
    runner = CodexRunner(plugin_dirs=[str(plugin)])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = runner._stage_skills(workspace)
    second = runner._stage_skills(workspace)
    assert len(first["created"]) == 1
    assert second["created"] == []
    assert not (workspace / ".agents" / "skills" / "empty").exists()
    runner._cleanup_staged_skills(workspace, second)
    runner._cleanup_staged_skills(workspace, first)


def test_codex_rejects_dangling_staged_symlink(tmp_path):
    runner = CodexRunner(plugin_dirs=[str(_plugin(tmp_path, "parent"))])
    workspace = tmp_path / "workspace"
    staged = workspace / ".agents" / "skills"
    staged.mkdir(parents=True)
    (staged / "parent").symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="existing symlink"):
        runner._stage_skills(workspace)


def test_codex_validates_plugin_root_before_execution(tmp_path):
    with pytest.raises(FileNotFoundError, match="plugin directory"):
        CodexRunner(plugin_dirs=[str(tmp_path / "missing")])
    commands_only = tmp_path / "commands-only"
    commands_only.mkdir()
    with pytest.raises(FileNotFoundError, match="skill directory"):
        CodexRunner(plugin_dirs=[str(commands_only)])


@requires_git
def test_repo_staging_does_not_dirty_git_status(tmp_path):
    plugin = _plugin(tmp_path, "parent")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false",
        "commit", "-q", "--allow-empty", "-m", "init",
    ], cwd=repo, check=True)
    runner = CodexRunner(plugin_dirs=[str(plugin)])

    manifest = runner._stage_skills(repo)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        check=True, capture_output=True, text=True).stdout
    assert status == ""
    runner._cleanup_staged_skills(repo, manifest)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        check=True, capture_output=True, text=True).stdout
    assert status == ""


@requires_git
def test_nested_workspace_staging_does_not_dirty_git_status(tmp_path):
    # Workspaces commonly live in nested repo paths (eval/runs/<case>/workspace);
    # the exclude rule must be anchored to the workspace, not the repo root.
    plugin = _plugin(tmp_path, "parent")
    repo = tmp_path / "repo"
    workspace = repo / "eval" / "runs" / "case-1" / "workspace"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false",
        "commit", "-q", "--allow-empty", "-m", "init",
    ], cwd=repo, check=True)
    runner = CodexRunner(plugin_dirs=[str(plugin)])

    manifest = runner._stage_skills(workspace)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        check=True, capture_output=True, text=True).stdout
    assert status == ""
    exclude = repo / ".git" / "info" / "exclude"
    assert "/eval/runs/case-1/workspace/.agents/" in exclude.read_text()

    runner._cleanup_staged_skills(workspace, manifest)
    assert ".agents" not in exclude.read_text()


def test_partial_copytree_failure_is_cleaned_up_and_restaged(
        tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, "parent")
    (plugin / "skills" / "parent" / "extra.py").write_text("x = 1\n")
    runner = CodexRunner(plugin_dirs=[str(plugin)])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    real_copytree = shutil.copytree

    def failing_copytree(src, dst, **kwargs):
        Path(dst).mkdir(parents=True)
        (Path(dst) / "SKILL.md").write_text("truncated")
        raise shutil.Error([(str(src), str(dst), "simulated copy failure")])

    monkeypatch.setattr(
        "agent_eval.agent.codex.shutil.copytree", failing_copytree)
    with pytest.raises(shutil.Error):
        runner._stage_skills(workspace)
    # The truncated destination must not survive to be treated as a complete
    # skill by later stagings.
    assert not (workspace / ".agents").exists()

    monkeypatch.setattr("agent_eval.agent.codex.shutil.copytree", real_copytree)
    manifest = runner._stage_skills(workspace)
    assert (workspace / ".agents" / "skills" / "parent" / "extra.py").is_file()
    runner._cleanup_staged_skills(workspace, manifest)


def test_toml_value_serializes_mappings_codex_can_parse():
    # codex-cli parses -c values as TOML; JSON objects would fall back to a
    # plain string.
    assert _toml_value("concise") == '"concise"'
    assert _toml_value(4) == "4"
    assert _toml_value(True) == "true"
    assert _toml_value(["a", 1]) == '["a", 1]'
    assert _toml_value({"network_access": True, "writable_roots": ["/tmp"]}) \
        == '{network_access = true, writable_roots = ["/tmp"]}'


def test_codex_malformed_events_degrade_gracefully():
    events = [
        {"type": "turn.completed", "usage": "oops"},
        {"type": "turn.completed",
         "usage": {"input_tokens": "many", "output_tokens": 3}},
    ]
    usage = _extract_usage(events)
    assert usage["token_usage"] == {"input": 0, "output": 3, "cache_read": 0}
    assert usage["num_turns"] == 2
    assert CodexRunner._extract_progress(
        {"type": "item.started", "item": "oops"}) == ""


def test_codex_missing_binary_returns_failed_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("codex missing")))
    result = CodexRunner().execute(None, "p", tmp_path, "m")
    assert result.exit_code == -1
    assert "codex missing" in result.stderr


def test_codex_timeout_kills_group_bounds_reap_and_keeps_partial_usage(
        tmp_path, monkeypatch):
    calls = []

    class FakeProcess:
        pid = 4321
        returncode = -9

        def communicate(self, input=None, timeout=None):
            calls.append((input, timeout))
            event = json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }) + "\n"
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("codex", timeout, output=event)
            return event, "killed"

    killed = []
    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr("agent_eval.agent.codex.os.killpg",
                        lambda pid, sig: killed.append((pid, sig)))
    result = CodexRunner().execute(None, "p", tmp_path, "m", timeout_s=1)

    assert killed and killed[0][0] == 4321
    assert calls[-1][1] == 5
    assert result.exit_code == -1
    assert result.token_usage == {"input": 2, "output": 1, "cache_read": 0}


def test_codex_post_kill_communicate_timeout_is_bounded(tmp_path, monkeypatch):
    class FakeProcess:
        pid = 4321
        returncode = -9

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired("codex", timeout, output="partial\n")

    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr("agent_eval.agent.codex.os.killpg", lambda *a: None)
    result = CodexRunner().execute(None, "p", tmp_path, "m", timeout_s=1)
    assert result.exit_code == -1
    assert result.stdout == "partial\n"
    assert "termination hung" in result.stderr


@pytest.mark.parametrize("stdout,events", [
    ('not-json\n{"type":"thread.started"}\n', 1),
    ('[]\n{"type":"turn.completed","usage":{}}\n', 1),
    ("", 0),
])
def test_codex_malformed_mixed_and_empty_output(tmp_path, monkeypatch, stdout, events):
    class FakeProcess:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return stdout, ""

    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen", lambda *a, **k: FakeProcess())
    result = CodexRunner().execute(None, "p", tmp_path, "m")
    assert len(result.raw_output["events"]) == events
    assert result.stdout == stdout
    if not stdout:
        assert result.token_usage is None


def test_codex_env_is_allowlisted_resolves_refs_and_skips_none(monkeypatch):
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "allowed")
    monkeypatch.setenv("SOURCE_VALUE", "resolved")
    runner = CodexRunner(env={
        "COPIED": "$SOURCE_VALUE", "MISSING": "$DOES_NOT_EXIST", "NULL": None})
    env = runner._build_env({"HOOK": 3, "HOOK_NULL": None})
    assert env["OPENAI_API_KEY"] == "allowed"
    assert env["COPIED"] == "resolved"
    assert env["HOOK"] == "3"
    assert "UNRELATED_HOST_SECRET" not in env
    assert "MISSING" not in env
    assert "NULL" not in env
    assert "HOOK_NULL" not in env


def test_codex_progress_never_echoes_event_payload():
    secret = "progress-redaction-sentinel"
    assert CodexRunner._extract_progress({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": f"echo {secret}"},
    }) == "Shell command completed"
    assert CodexRunner._extract_progress({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": secret},
    }) == "Agent message completed"


def test_extract_usage_ignores_non_turn_events():
    assert _extract_usage([
        {"type": "item.completed"},
        {"type": "turn.completed", "usage": {
            "input_tokens": 2, "output_tokens": 1,
        }},
    ]) == {
        "token_usage": {"input": 2, "output": 1, "cache_read": 0},
        "num_turns": 1,
    }
