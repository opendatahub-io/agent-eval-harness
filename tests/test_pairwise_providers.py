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


# --- openrouter:/ pairwise judges (spec 014) ---------------------------------

from agent_eval.config import (  # noqa: E402
    JudgeClientOptions, ModelsConfig, OpenRouterConfig, ProvidersConfig, RoutingConfig)
from agent_eval.providers.openrouter import RoutingTable  # noqa: E402


def _openrouter_config(inherit_pins=True):
    table = RoutingTable.from_dict(
        {"models": {"z-ai/glm-5.2": {"order": ["z-ai"], "allow_fallbacks": False}}})
    config = EvalConfig(name="t", skill="s")
    config.models = ModelsConfig(providers=ProvidersConfig(openrouter=OpenRouterConfig(
        routing=RoutingConfig(defaults=table.defaults, models=table.models),
        judge=JudgeClientOptions(inherit_pins=inherit_pins))))
    return config


def _raw_client(*script):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        step = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(step, BaseException):
            raise step
        return step

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    client.calls = calls
    return client


def _pw_response(tool_name="submit_comparison", arguments=None, content=None,
                 finish_reason="stop", error=None):
    tool_calls = None
    if tool_name is not None:
        tool_calls = [SimpleNamespace(function=SimpleNamespace(
            name=tool_name, arguments=arguments))]
    message = SimpleNamespace(tool_calls=tool_calls, content=content)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])
    if error is not None:
        response.error = error
    return response


class _NotFound(Exception):
    status_code = 404
    message = "No endpoints found for z-ai/glm-5.2 that support tool use."


def test_compare_runs_openrouter_uses_the_dedicated_client_and_extra_body():
    config = _openrouter_config()
    captured = {}
    sentinel = object()

    def fake_call_judge(backend, client, system_prompt, user_message, m,
                        max_tokens=16384, **kw):
        captured.update(backend=backend, client=client, model=m,
                        max_tokens=max_tokens, **kw)
        return {"reasoning": "r", "preferred": "A"}, None

    def must_not(*a, **k):
        raise AssertionError("wrong client getter used")

    with patch("score.load_case_record", return_value={"files": {}}), \
            patch("score._format_outputs_for_pairwise", return_value="output"), \
            patch("score._call_judge", side_effect=fake_call_judge), \
            patch("score._client_for", return_value=sentinel) as client_for, \
            patch("score._get_openai_client", side_effect=must_not), \
            patch("score._get_anthropic_client", side_effect=must_not):
        result = score.compare_runs(Path("a"), Path("b"), config, ["case-1"],
                                    model="openrouter:/z-ai/glm-5.2",
                                    provider_options={"max_tokens": 4096})

    assert result.get("error") is None
    assert captured["backend"] == "openai" and captured["client"] is sentinel
    assert captured["model"] == "z-ai/glm-5.2"
    assert captured["client_cfg"].name == "openrouter"
    assert captured["token_param"] == "max_tokens"
    assert captured["max_tokens"] == 4096
    assert captured["extra_body"]["provider"]["order"] == ["z-ai"]
    assert captured["extra_body"]["provider"]["require_parameters"] is True
    assert client_for.call_args.args[0].name == "openrouter"


def test_compare_runs_plain_openai_model_passes_no_binding():
    """Existing openai:/ pairwise judges keep the bare call signature."""
    captured = {}

    def fake_call_judge(backend, client, system_prompt, user_message, m,
                        max_tokens=16384, **kw):
        captured.update(kw)
        return {"reasoning": "r", "preferred": "A"}, None

    with patch("score.load_case_record", return_value={"files": {}}), \
            patch("score._format_outputs_for_pairwise", return_value="output"), \
            patch("score._call_judge", side_effect=fake_call_judge), \
            patch("score._get_openai_client", return_value=object()), \
            patch("score._client_for", side_effect=AssertionError("no cfg expected")):
        score.compare_runs(Path("a"), Path("b"), EvalConfig(name="t", skill="s"),
                           ["case-1"], model="openai:/gpt-4o")
    assert captured == {}


def test_call_pairwise_openai_accepts_dict_arguments():
    client = _raw_client(_pw_response(arguments={"reasoning": "A wins", "preferred": "A"}))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "z-ai/glm-5.2")
    assert err is None and verdict["preferred"] == "A"


def test_call_pairwise_openai_reports_a_committed_200_error():
    client = _raw_client(_pw_response(
        tool_name=None, content="", finish_reason="error",
        error={"code": 502, "message": "Provider returned error",
               "metadata": {"provider_name": "Novita"}}))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "z-ai/glm-5.2")
    assert verdict is None and "Novita" in err and "200" in err


def test_call_pairwise_openai_ladder_strict_parses_the_fallback():
    client = _raw_client(_NotFound(), _pw_response(
        tool_name="other", arguments="{}", content='{"preferred": "A"}'))
    verdict, err = score._call_pairwise_openai(client, "compare", "A vs B", "z-ai/glm-5.2")
    assert verdict is None and "instead of" in err
    assert client.calls[1]["tool_choice"] == "required"


def test_call_pairwise_openai_passes_extra_body_and_token_param():
    client = _raw_client(_pw_response(arguments={"reasoning": "r", "preferred": "tie"}))
    verdict, err = score._call_pairwise_openai(
        client, "compare", "A vs B", "openai/gpt-5.2",
        extra_body={"provider": {"sort": "price"}}, token_param="max_tokens")
    assert err is None and verdict["preferred"] == "tie"
    sent = client.calls[0]
    assert sent["extra_body"] == {"provider": {"sort": "price"}}
    assert "max_tokens" in sent and "max_completion_tokens" not in sent
