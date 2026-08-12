"""Tests for the Codex CLI runner."""

import json
from pathlib import Path

import yaml

from agent_eval.agent import RUNNERS
from agent_eval.agent.codex import CodexRunner, _extract_usage
from agent_eval.config import EvalConfig


def test_codex_is_registered():
    assert RUNNERS["codex"] is CodexRunner


def test_codex_from_config_accepts_effort_and_settings(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    (plugin / "skills" / "parent").mkdir(parents=True)
    (plugin / "skills" / "parent" / "SKILL.md").write_text("# Parent\n")
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
    config = EvalConfig.from_yaml(config_path)
    runner = CodexRunner.from_config(config)

    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
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
    assert (workspace / ".agents" / "skills" / "parent" / "SKILL.md").is_file()
    command = captured["command"]
    assert command[:2] == ["codex", "exec"]
    assert ["--model", "gpt-5.6-luna"] == command[
        command.index("--model"):command.index("--model") + 2]
    assert "model_reasoning_effort=\"xhigh\"" in command
    assert "model_reasoning_summary=\"concise\"" in command
    assert command[-1] == "Use the parent skill with arguments: --flag value"


def test_codex_stages_all_sibling_skills(tmp_path):
    plugin = tmp_path / "plugin"
    for name in ("parent", "dependency"):
        path = plugin / "skills" / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"# {name}\n")
    runner = CodexRunner(plugin_dirs=[str(plugin)])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    runner._stage_skills(workspace)

    staged = workspace / ".agents" / "skills"
    assert (staged / "parent" / "SKILL.md").is_file()
    assert (staged / "dependency" / "SKILL.md").is_file()


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
