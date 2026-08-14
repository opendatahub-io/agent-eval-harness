"""Tests for the LLM-judge reasoning-effort knob (`output_config.effort`).

Two invariants carry the whole feature:

1. An unconfigured judge must issue a byte-identical request to the one it
   issued before the parameter existed — `effort` is not accepted on every
   model, and a default would silently change existing scores.
2. A configured effort must reach *every* single-call judge path (builtin LLM,
   inline LLM, pairwise), because a knob that only lands on some of them
   produces a run scored under two different settings.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig, JudgeConfig, ModelsConfig
from score import (_call_judge, _call_structured_judge, _effort_kwargs,
                   _resolve_judge_effort, load_judges)


def _write(tmp_path, body):
    p = tmp_path / "eval.yaml"
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------
# Config parsing and validation
# ---------------------------------------------------------------------------

class TestConfigParsing:

    def test_models_judge_effort_parses(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
models: {judge: claude-opus-4-6, judge_effort: medium}
"""))
        assert cfg.models.judge_effort == "medium"

    def test_models_judge_effort_defaults_to_none(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
models: {judge: claude-opus-4-6}
"""))
        assert cfg.models.judge_effort is None

    def test_per_judge_effort_parses(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
models: {judge: claude-opus-4-6}
judges:
  - name: q
    llm_rubric: "good?"
    feedback_type: bool
    effort: xhigh
"""))
        assert cfg.judges[0].effort == "xhigh"

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_every_ladder_level_accepted(self, tmp_path, level):
        cfg = EvalConfig.from_yaml(_write(tmp_path, f"""
name: t
execution: {{skill: s}}
models: {{judge: claude-opus-4-6, judge_effort: {level}}}
"""))
        assert cfg.models.judge_effort == level

    def test_invalid_models_effort_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="models.judge_effort"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
models: {judge: claude-opus-4-6, judge_effort: turbo}
"""))

    def test_invalid_per_judge_effort_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="effort must be one of"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
models: {judge: claude-opus-4-6}
judges:
  - name: q
    llm_rubric: "good?"
    feedback_type: bool
    effort: HIGH
"""))


class TestConfigCoherence:
    """`effort` reaches the model only on the single-call LLM path — anywhere
    else it would be accepted and silently ignored, which is the failure mode
    the surrounding scale checks exist to prevent."""

    def test_effort_on_agent_judge_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="agent.runner.effort"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
models: {judge: claude-opus-4-6}
judges:
  - name: q
    llm_rubric: "good?"
    feedback_type: bool
    effort: high
    agent:
      runner: {type: claude-code}
"""))

    def test_effort_on_inline_check_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="only applies to LLM judges"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
judges:
  - name: q
    check: "return True, 'ok'"
    feedback_type: bool
    effort: high
"""))

    def test_effort_on_builtin_code_judge_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="only applies to LLM judges"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
judges:
  - name: budget
    builtin: cost_budget
    effort: high
"""))

    def test_effort_on_builtin_llm_judge_allowed(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
models: {judge: claude-opus-4-6}
judges:
  - name: safety
    builtin: no_harmful_content
    effort: high
"""))
        assert cfg.judges[0].effort == "high"


# ---------------------------------------------------------------------------
# Resolution and request shape
# ---------------------------------------------------------------------------

class TestResolution:

    def _config(self, judge_effort=None):
        cfg = EvalConfig(name="t", skill="s")
        cfg.models = ModelsConfig(judge="claude-opus-4-6",
                                  judge_effort=judge_effort)
        return cfg

    def test_per_judge_wins(self):
        jc = JudgeConfig(name="q", effort="max")
        assert _resolve_judge_effort(jc, self._config("low")) == "max"

    def test_falls_back_to_models_default(self):
        jc = JudgeConfig(name="q")
        assert _resolve_judge_effort(jc, self._config("low")) == "low"

    def test_unset_everywhere_is_none(self):
        jc = JudgeConfig(name="q")
        assert _resolve_judge_effort(jc, self._config()) is None


class TestEffortKwargs:

    def test_none_produces_no_kwarg(self):
        assert _effort_kwargs(None) == {}

    def test_empty_string_produces_no_kwarg(self):
        assert _effort_kwargs("") == {}

    def test_set_produces_output_config(self):
        assert _effort_kwargs("high") == {"output_config": {"effort": "high"}}


def _mock_client(tool_name, tool_input):
    """A client whose response carries one forced tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = tool_input
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "tool_use"
    client = MagicMock()
    client.messages.create.return_value = response
    return client


class TestStructuredJudgeRequest:

    def test_unset_effort_omits_output_config_entirely(self):
        client = _mock_client("submit_evaluation",
                              {"passed": True, "rationale": "ok"})
        with patch("score._get_anthropic_client", return_value=client):
            _call_structured_judge("p", "claude-opus-4-6", "bool")
        assert "output_config" not in client.messages.create.call_args.kwargs

    def test_set_effort_sends_output_config(self):
        client = _mock_client("submit_evaluation",
                              {"passed": True, "rationale": "ok"})
        with patch("score._get_anthropic_client", return_value=client):
            _call_structured_judge("p", "claude-opus-4-6", "bool",
                                   effort="xhigh")
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["output_config"] == {"effort": "xhigh"}
        # The forced tool call is what makes the verdict parseable; effort
        # must not displace it.
        assert kwargs["tool_choice"]["type"] == "tool"


class TestTruncationRetry:
    """At the upper effort levels `max_tokens` bounds thinking *and* text, so
    the 4096 default can be spent reasoning and never reach the forced tool.
    Falling through to the text parser would score that as a failing case."""

    def _truncated(self):
        r = MagicMock()
        r.content = []
        r.stop_reason = "max_tokens"
        return r

    def _verdict(self):
        block = MagicMock()
        block.type = "tool_use"
        block.name = "submit_evaluation"
        block.input = {"passed": True, "rationale": "ok"}
        r = MagicMock()
        r.content = [block]
        r.stop_reason = "tool_use"
        return r

    def test_retries_with_double_budget_and_keeps_effort(self):
        client = MagicMock()
        client.messages.create.side_effect = [self._truncated(), self._verdict()]
        with patch("score._get_anthropic_client", return_value=client):
            value, rationale = _call_structured_judge(
                "p", "claude-opus-4-6", "bool", effort="max")
        assert value is True and rationale == "ok"
        assert client.messages.create.call_count == 2
        budgets = [c.kwargs["max_tokens"]
                   for c in client.messages.create.call_args_list]
        assert budgets == [4096, 8192]
        for call in client.messages.create.call_args_list:
            assert call.kwargs["output_config"] == {"effort": "max"}

    def test_retry_gives_up_at_the_ceiling(self):
        """Bounded, so a persistently truncating judge errors instead of
        looping — and never silently becomes a FAIL through the parser."""
        client = MagicMock()
        client.messages.create.return_value = self._truncated()
        with patch("score._get_anthropic_client", return_value=client):
            with pytest.raises(ValueError):
                _call_structured_judge("p", "claude-opus-4-6", "bool",
                                       max_tokens=32768)
        assert client.messages.create.call_count == 1

    def test_no_retry_when_not_truncated(self):
        client = MagicMock()
        client.messages.create.return_value = self._verdict()
        with patch("score._get_anthropic_client", return_value=client):
            _call_structured_judge("p", "claude-opus-4-6", "bool")
        assert client.messages.create.call_count == 1


class TestPairwiseRequest:

    def test_unset_effort_omits_output_config(self):
        client = _mock_client("submit_comparison",
                              {"preferred": "A", "reasoning": "r"})
        _call_judge(client, "prompt", "msg", "claude-opus-4-6")
        assert "output_config" not in client.messages.create.call_args.kwargs

    def test_set_effort_sends_output_config(self):
        client = _mock_client("submit_comparison",
                              {"preferred": "A", "reasoning": "r"})
        _call_judge(client, "prompt", "msg", "claude-opus-4-6", effort="low")
        assert (client.messages.create.call_args.kwargs["output_config"]
                == {"effort": "low"})

    def test_truncation_retry_keeps_effort(self):
        """The max_tokens-doubling retry must not silently drop the setting."""
        empty = MagicMock()
        empty.content = []
        empty.stop_reason = "max_tokens"
        done = MagicMock()
        block = MagicMock()
        block.type = "tool_use"
        block.name = "submit_comparison"
        block.input = {"preferred": "B", "reasoning": "r"}
        done.content = [block]
        done.stop_reason = "tool_use"
        client = MagicMock()
        client.messages.create.side_effect = [empty, done]

        result, err = _call_judge(client, "prompt", "msg", "claude-opus-4-6",
                                  max_tokens=16384, effort="high")
        assert err is None and result["preferred"] == "B"
        assert client.messages.create.call_count == 2
        for call in client.messages.create.call_args_list:
            assert call.kwargs["output_config"] == {"effort": "high"}


# ---------------------------------------------------------------------------
# End-to-end through load_judges
# ---------------------------------------------------------------------------

class TestThreadedThroughLoadJudges:

    def test_builtin_llm_judge_receives_effort(self):
        config = EvalConfig(name="t", skill="s")
        config.models = ModelsConfig(judge="claude-opus-4-6",
                                     judge_effort="medium")
        config.judges = [JudgeConfig(name="safety",
                                     builtin="no_harmful_content")]
        _, scorer, _, _, _ = load_judges(config)[0]
        with patch("score._call_structured_judge",
                   return_value=(True, "ok")) as mock:
            scorer(outputs={"conversation": "c", "files": {}})
        assert mock.call_args.kwargs["effort"] == "medium"

    def test_inline_llm_judge_prefers_per_judge_effort(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = EvalConfig(name="t", skill="s")
        config.models = ModelsConfig(judge="claude-opus-4-6",
                                     judge_effort="medium")
        config.judges = [JudgeConfig(name="q", llm_rubric="good?",
                                     feedback_type="bool", effort="max")]
        _, scorer, _, _, _ = load_judges(config)[0]
        with patch("score._call_structured_judge",
                   return_value=(True, "ok")) as mock:
            scorer(outputs={"conversation": "c", "files": {}})
        assert mock.call_args.kwargs["effort"] == "max"

    def test_unconfigured_judge_passes_none(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = EvalConfig(name="t", skill="s")
        config.models = ModelsConfig(judge="claude-opus-4-6")
        config.judges = [JudgeConfig(name="q", llm_rubric="good?",
                                     feedback_type="bool")]
        _, scorer, _, _, _ = load_judges(config)[0]
        with patch("score._call_structured_judge",
                   return_value=(True, "ok")) as mock:
            scorer(outputs={"conversation": "c", "files": {}})
        assert mock.call_args.kwargs["effort"] is None
