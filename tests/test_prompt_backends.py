"""Tests for prompt-only runner backends (judges, synthetic generation)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent.base import RunResult
from agent_eval.config import EvalConfig
from agent_eval.prompt_backends import (
    RUNNERS,
    extract_runner_text,
    run_prompt_via_runner,
)


def _write_config(tmp_path, body: str) -> Path:
    path = tmp_path / "eval.yaml"
    path.write_text(body)
    return path


class _FakeRunner:
    captured = None

    @classmethod
    def from_config(cls, config, **kwargs):
        cls.captured = {
            "permission_mode": config.runner.permission_mode,
            "settings": dict(config.runner.settings or {}),
            "system_prompt": config.runner.system_prompt,
            "workspace_mode": config.runner.workspace_mode,
        }
        return cls()

    def execute(self, **kwargs):
        return RunResult(exit_code=0, stdout='{"score": 5}', stderr="", duration_s=0)


def test_run_prompt_via_runner_clears_plan_mode(tmp_path, monkeypatch):
    monkeypatch.setitem(RUNNERS, "cursor", _FakeRunner)
    _FakeRunner.captured = None

    cfg = EvalConfig.from_yaml(_write_config(tmp_path, """
name: t
execution:
  prompt: "{{ input.prompt }}"
runner:
  type: cursor
  permission_mode: plan
  system_prompt: Read AGENTS.md first
  settings:
    mode: plan
    endpoint: https://cursor.example
    add_dirs: [/outside]
"""))

    # Exercise the helper's defensive copy without making the persisted eval
    # config itself invalid for Cursor.
    cfg.runner.workspace_mode = "repo"

    run_prompt_via_runner(cfg, "score this", "gpt-5.4-medium", workspace=tmp_path)

    captured = _FakeRunner.captured
    assert captured["permission_mode"] is None
    assert captured["workspace_mode"] is None
    assert captured["system_prompt"] == ""
    assert captured["settings"]["mode"] == "plan"
    assert captured["settings"]["add_dirs"] == ["/outside"]
    assert captured["settings"]["endpoint"] == "https://cursor.example"


def test_run_prompt_via_runner_keeps_non_plan_settings_mode(tmp_path, monkeypatch):
    monkeypatch.setitem(RUNNERS, "cursor", _FakeRunner)
    _FakeRunner.captured = None

    cfg = EvalConfig.from_yaml(_write_config(tmp_path, """
name: t
execution:
  prompt: "{{ input.prompt }}"
runner:
  type: cursor
  permission_mode: plan
  settings:
    mode: ask
"""))

    run_prompt_via_runner(cfg, "hello", "gpt-5.4-medium", workspace=tmp_path)

    captured = _FakeRunner.captured
    assert captured["permission_mode"] is None
    assert captured["settings"]["mode"] == "ask"


def test_extract_runner_text_prefers_cursor_terminal_result():
    stdout = "\n".join([
        '{"type":"assistant","session_id":"s","message":{"content":[{"type":"text","text":"{\\"sc"}]}}',
        '{"type":"assistant","session_id":"s","message":{"content":[{"type":"text","text":"ore\\": 2}"}]}}',
        '{"type":"result","session_id":"s","result":"{\\"score\\": 2}"}',
    ])
    result = RunResult(exit_code=0, stdout=stdout, stderr="", duration_s=0)
    assert extract_runner_text(result) == '{"score": 2}'
