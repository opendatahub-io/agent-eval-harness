"""Pairwise comparison routes on the judge model's provider (cross-provider).

Regression coverage for the follow-up to the Cursor runner PR: the pairwise
path must strip a ``provider:/`` prefix and dispatch to the OpenAI backend, not
hand the raw prefixed id to the Anthropic Messages API.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import score  # noqa: E402
from agent_eval.config import EvalConfig  # noqa: E402


def _openai_client(*, arguments=None, content=None, finish_reason="stop"):
    def create(**kwargs):
        create.captured = kwargs
        tool_calls = None
        if arguments is not None:
            call = SimpleNamespace(function=SimpleNamespace(
                name="submit_comparison", arguments=json.dumps(arguments)))
            tool_calls = [call]
        message = SimpleNamespace(tool_calls=tool_calls, content=content)
        choice = SimpleNamespace(message=message, finish_reason=finish_reason)
        return SimpleNamespace(choices=[choice])

    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


def test_call_pairwise_openai_reads_tool_call():
    client = _openai_client(arguments={"reasoning": "A wins", "preferred": "A"})
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o")
    assert err is None and verdict["preferred"] == "A"


def test_call_pairwise_openai_text_fallback():
    client = _openai_client(content='{"reasoning": "r", "preferred": "tie"}')
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "gpt-4o")
    assert err is None and verdict["preferred"] == "tie"


def test_call_judge_dispatches_to_openai_backend():
    with patch("score._call_pairwise_openai",
               return_value=({"preferred": "B"}, None)) as mocked:
        verdict, err = score._call_judge("openai", object(), "sys", "msg", "gpt-4o")
    assert verdict["preferred"] == "B" and err is None
    mocked.assert_called_once()


def test_compare_runs_rejects_runner_model():
    config = EvalConfig(name="t", skill="s")
    result = score.compare_runs(Path("a"), Path("b"), config, [],
                                model="runner:/gpt-5.4-medium")
    assert "error" in result and "runner" in result["error"].lower()


def _run_one_comparison(model, *, openai_raises=False, anthropic_raises=False):
    """Drive compare_runs over a single stubbed case and capture the backend +
    stripped model handed to _call_judge, plus which client getter was used."""
    config = EvalConfig(name="t", skill="s")
    captured = {}

    def fake_call_judge(backend, client, system_prompt, user_message, m,
                        max_tokens=16384):
        captured["backend"] = backend
        captured["model"] = m
        return {"reasoning": "r", "preferred": "A"}, None

    oai = (AssertionError("must not use OpenAI") if openai_raises
           else None)
    anth = (AssertionError("must not use Anthropic") if anthropic_raises
            else None)
    with patch("score.load_case_record", return_value={"files": {}}), \
            patch("score._format_outputs_for_pairwise", return_value="output"), \
            patch("score._call_judge", side_effect=fake_call_judge), \
            patch("score._get_openai_client",
                  **({"side_effect": oai} if oai else {"return_value": object()})), \
            patch("score._get_anthropic_client",
                  **({"side_effect": anth} if anth else {"return_value": object()})):
        result = score.compare_runs(Path("a"), Path("b"), config, ["case-1"],
                                    model=model)
    return result, captured


def test_compare_runs_openai_backend_strips_prefix():
    result, captured = _run_one_comparison("openai:/gpt-4o", anthropic_raises=True)
    assert "error" not in result
    assert captured["backend"] == "openai" and captured["model"] == "gpt-4o"


def test_compare_runs_anthropic_backend_strips_prefix():
    result, captured = _run_one_comparison("anthropic:/claude-sonnet-4-5",
                                           openai_raises=True)
    assert "error" not in result
    assert captured["backend"] == "anthropic"
    assert captured["model"] == "claude-sonnet-4-5"
