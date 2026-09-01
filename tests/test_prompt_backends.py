"""Tests for prompt-only runner backends (judges, synthetic generation)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent.base import RunResult
from agent_eval.config import EvalConfig
from agent_eval.prompt_backends import (
    RUNNERS,
    extract_runner_text,
    is_anthropic_model,
    resolve_judge_backend,
    run_prompt_via_runner,
    split_model_uri,
)
import pytest


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


# ---------------------------------------------------------------------------
# Provider routing (judge backend decoupled from the runner)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("openai:/gpt-4o", ("openai", "gpt-4o")),
    ("openai://gpt-4o", ("openai", "gpt-4o")),
    ("anthropic:/claude-sonnet-4-5", ("anthropic", "claude-sonnet-4-5")),
    ("runner:/gpt-5.4-medium", ("runner", "gpt-5.4-medium")),
    ("sonnet", (None, "sonnet")),
    ("gpt-4o", (None, "gpt-4o")),
    ("  Sonnet  ", (None, "Sonnet")),
])
def test_split_model_uri(model, expected):
    assert split_model_uri(model) == expected


@pytest.mark.parametrize("model,expected", [
    ("sonnet", True),
    ("sonnet-4-5", True),          # versioned alias (regression: was misclassified)
    ("opus[1m]", True),            # bracketed alias
    ("haiku-4-5", True),
    ("claude-3-5-sonnet", True),
    ("anthropic:/some-model", True),
    ("anthropic/claude", True),    # LiteLLM single-slash form
    ("openai:/gpt-4o", False),
    ("gpt-4o", False),
    ("gemini-2.5-flash", False),
    ("", False),
    (None, False),
])
def test_is_anthropic_model(model, expected):
    assert is_anthropic_model(model) is expected


@pytest.mark.parametrize("model,expected", [
    # Anthropic: bare alias, full id, explicit URI (prefix stripped).
    ("sonnet", ("anthropic", "sonnet")),
    ("claude-sonnet-4-5", ("anthropic", "claude-sonnet-4-5")),
    ("sonnet-4-5", ("anthropic", "sonnet-4-5")),
    ("anthropic:/claude-x", ("anthropic", "claude-x")),
    # OpenAI: bare family id, o-series, explicit URI, gateway-served id.
    ("gpt-4o", ("openai", "gpt-4o")),
    ("o3-mini", ("openai", "o3-mini")),
    ("openai:/gpt-4o", ("openai", "gpt-4o")),
    ("openai:/gemini-2.5-flash", ("openai", "gemini-2.5-flash")),
    # Runner: explicit opt-in for runner-managed ids.
    ("runner:/gpt-5.4-medium", ("runner", "gpt-5.4-medium")),
    # A bare custom/gateway id defaults to OpenAI (served via OPENAI_BASE_URL).
    ("mistral-large", ("openai", "mistral-large")),
    ("gpt-5.4-medium", ("openai", "gpt-5.4-medium")),
])
def test_resolve_judge_backend(model, expected):
    assert resolve_judge_backend(model) == expected


@pytest.mark.parametrize("model", [
    "gemini:/gemini-2.5-flash",   # unsupported explicit provider
    "mistral:/mistral-large",     # unsupported explicit provider
    "runner:/",                   # runner opt-in with no model name
])
def test_resolve_judge_backend_rejects(model):
    with pytest.raises(ValueError):
        resolve_judge_backend(model)
