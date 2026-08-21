"""Tests for the AskUserQuestion hook's LLM answering call.

The hook is the one LLM call in the harness whose failure is *invisible*:
`_llm_answer` swallows every exception and the caller falls through to the
first option, so a rejected request feeds the agent under test an unvetted
answer while the run still reports a pass. These tests pin the two behaviors
that keep that from happening silently — the sampling-parameter retry, and the
stderr announcement on the fallback tier.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(
    0, str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"))

import tools as hook_tools


@pytest.fixture(autouse=True)
def _redirect_ledger(tmp_path, monkeypatch):
    """Point the provenance ledger away from the repo.

    tools.py anchors _LEDGER to its own directory; imported in-process from
    the repo, in-process _handle_ask_user calls would otherwise append into
    skills/eval-run/scripts/hook_answers.jsonl.
    """
    ledger = tmp_path / "hook_answers.jsonl"
    monkeypatch.setattr(hook_tools, "_LEDGER", ledger)
    return ledger


def _read_ledger(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


class _Rejected(Exception):
    """Stands in for anthropic.BadRequestError without importing the SDK."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class TestCreateMessage:

    def test_passes_through_on_success(self):
        client = MagicMock()
        client.messages.create.return_value = "ok"
        assert hook_tools._create_message(
            client, model="m", temperature=0, max_tokens=8) == "ok"
        assert client.messages.create.call_count == 1
        assert client.messages.create.call_args.kwargs["temperature"] == 0

    def test_retries_without_temperature_when_rejected(self, capsys):
        """Opus 4.7+ / Sonnet 5 removed the sampling parameters."""
        client = MagicMock()
        client.messages.create.side_effect = [
            _Rejected("temperature: Extra inputs are not permitted"),
            "ok",
        ]
        assert hook_tools._create_message(
            client, model="claude-opus-4-8", temperature=0, max_tokens=8) == "ok"
        assert client.messages.create.call_count == 2
        retry = client.messages.create.call_args_list[1].kwargs
        assert "temperature" not in retry
        assert retry["model"] == "claude-opus-4-8"
        assert retry["max_tokens"] == 8
        assert "rejects 'temperature'" in capsys.readouterr().err

    def test_works_for_a_gateway_alias(self):
        """The guard is behavioral, so an opaque model name is still covered."""
        client = MagicMock()
        client.messages.create.side_effect = [
            _Rejected("temperature is not supported"), "ok"]
        assert hook_tools._create_message(
            client, model="my-proxy-alias", temperature=0) == "ok"
        assert client.messages.create.call_count == 2

    def test_other_400s_propagate(self):
        client = MagicMock()
        client.messages.create.side_effect = _Rejected("model: not found")
        with pytest.raises(_Rejected):
            hook_tools._create_message(client, model="m", temperature=0)
        assert client.messages.create.call_count == 1

    def test_non_400_propagates(self):
        client = MagicMock()
        client.messages.create.side_effect = _Rejected("temperature", 500)
        with pytest.raises(_Rejected):
            hook_tools._create_message(client, model="m", temperature=0)
        assert client.messages.create.call_count == 1

    def test_no_retry_when_temperature_was_not_sent(self):
        client = MagicMock()
        client.messages.create.side_effect = _Rejected("temperature")
        with pytest.raises(_Rejected):
            hook_tools._create_message(client, model="m")
        assert client.messages.create.call_count == 1


class TestFallbackIsAnnounced:

    def test_first_option_fallback_warns(self, capsys, monkeypatch):
        # No case override and no LLM answer -> tier 3.
        monkeypatch.setattr(hook_tools, "_llm_answer",
                            lambda *a, **k: (None, {}))
        tool_input = {"questions": [
            {"question": "Which?", "options": [
                {"label": "Alpha"}, {"label": "Beta"}]},
        ]}
        hook_tools._handle_ask_user(tool_input, {}, {"prompt": "p"})
        captured = capsys.readouterr()
        assert "AskUserQuestion fallback" in captured.err
        assert "'Alpha'" in captured.err
        # The answer is still emitted — the fallback stays non-fatal.
        assert '"Alpha"' in captured.out

    def test_case_override_does_not_warn(self, capsys, monkeypatch):
        monkeypatch.setattr(hook_tools, "_llm_answer",
                            lambda *a, **k: (None, {}))
        tool_input = {"questions": [
            {"question": "Which?", "options": [{"label": "Alpha"}]},
        ]}
        hook_tools._handle_ask_user(
            tool_input, {"case_overrides": {"Which?": "Beta"}}, {"prompt": "p"})
        captured = capsys.readouterr()
        assert "AskUserQuestion fallback" not in captured.err
        assert '"Beta"' in captured.out


class TestCreateMessageMeta:
    """The strip-retry must report itself so the ledger records the
    decoding change — and the meta channel must never leak into the API."""

    def test_strip_retry_sets_temperature_stripped(self):
        client = MagicMock()
        client.messages.create.side_effect = [
            _Rejected("temperature: Extra inputs are not permitted"), "ok"]
        meta = {}
        assert hook_tools._create_message(
            client, meta=meta, model="m", temperature=0) == "ok"
        assert meta["temperature_stripped"] is True

    def test_meta_never_forwarded_to_messages_create(self):
        client = MagicMock()
        client.messages.create.return_value = "ok"
        meta = {}
        hook_tools._create_message(client, meta=meta, model="m", temperature=0)
        for call in client.messages.create.call_args_list:
            assert "meta" not in call.kwargs

    def test_meta_untouched_on_clean_success(self):
        client = MagicMock()
        client.messages.create.return_value = "ok"
        meta = {}
        hook_tools._create_message(client, meta=meta, model="m", temperature=0)
        assert meta == {}


class TestLlmAnswerMeta:
    """_llm_answer returns (label, meta) so fuzzy-match/rejected-reply/API
    failure details reach the provenance ledger."""

    def _fake_anthropic(self, monkeypatch, reply=None, ctor_error=None):
        client = MagicMock()
        if reply is not None:
            client.messages.create.return_value = SimpleNamespace(
                content=[SimpleNamespace(text=reply)])

        def anthropic_ctor(**kwargs):
            if ctor_error is not None:
                raise ctor_error
            return client

        monkeypatch.setitem(
            sys.modules, "anthropic",
            SimpleNamespace(Anthropic=anthropic_ctor))
        return client

    def test_exact_match(self, monkeypatch):
        self._fake_anthropic(monkeypatch, reply="Alpha")
        answer, meta = hook_tools._llm_answer(
            "Which?", [{"label": "Alpha"}, {"label": "Beta"}], "p")
        assert answer == "Alpha"
        assert meta["match"] == "exact"
        assert meta["model"] == "claude-haiku-4-5-20251001"

    def test_fuzzy_match(self, monkeypatch):
        self._fake_anthropic(monkeypatch, reply='"alpha"')
        answer, meta = hook_tools._llm_answer(
            "Which?", [{"label": "Alpha"}], "p", model="m-1")
        assert answer == "Alpha"
        assert meta["match"] == "fuzzy"
        assert meta["model"] == "m-1"

    def test_rejected_reply_carries_llm_raw(self, monkeypatch):
        self._fake_anthropic(monkeypatch, reply="Gamma, definitely")
        answer, meta = hook_tools._llm_answer(
            "Which?", [{"label": "Alpha"}], "p")
        assert answer is None
        assert meta["llm_raw"] == "Gamma, definitely"
        assert "match" not in meta

    def test_api_failure_carries_error(self, monkeypatch):
        self._fake_anthropic(monkeypatch,
                             ctor_error=RuntimeError("no API key"))
        answer, meta = hook_tools._llm_answer(
            "Which?", [{"label": "Alpha"}], "p")
        assert answer is None
        assert "no API key" in meta["error"]

    def test_client_is_built_deadline_safe(self, monkeypatch):
        """anthropic.Anthropic(timeout=<nominal>, max_retries=0): with SDK
        retries disabled, one call's wall time is bounded by its nominal
        timeout — the deadline-budget arithmetic depends on exactly this."""
        seen = {}
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="Alpha")])

        def anthropic_ctor(**kwargs):
            seen.update(kwargs)
            return client

        monkeypatch.setitem(sys.modules, "anthropic",
                            SimpleNamespace(Anthropic=anthropic_ctor))
        answer, _meta = hook_tools._llm_answer(
            "Which?", [{"label": "Alpha"}], "p", timeout=12.5)
        assert answer == "Alpha"
        assert seen == {"timeout": 12.5, "max_retries": 0}


class TestPrimaryDeadlineGate:
    """The tier-2 PRIMARY call is gated on the remaining deadline budget:
    a draw that cannot fit _PRIMARY_TIMEOUT is skipped straight to the
    tier-3 fallback with a ledger-recorded {"skipped": "deadline"} —
    distinguishable from an LLM failure. Tier-1 overrides make no API call
    and are never gated. With max_retries=0 on the client this bounds the
    hook's worst case regardless of question count."""

    def _two_questions(self):
        return {"questions": [
            {"question": "Q1?", "options": [
                {"label": "Alpha"}, {"label": "Beta"}]},
            {"question": "Q2?", "options": [
                {"label": "Gamma"}, {"label": "Delta"}]},
        ]}

    def test_budget_exhaustion_midway_deadline_skips_later_questions(
            self, monkeypatch, _redirect_ledger, capsys):
        budgets = iter([100.0])  # Q1 fits; everything after is busted

        def fake_budget():
            return next(budgets, 1.0)

        calls = []

        def fake_llm(*args, **kwargs):
            calls.append(args[0])
            return "Alpha", {"model": "m", "match": "exact"}

        monkeypatch.setattr(hook_tools, "_remaining_budget", fake_budget)
        monkeypatch.setattr(hook_tools, "_llm_answer", fake_llm)
        hook_tools._handle_ask_user(self._two_questions(), {}, {"prompt": "p"})

        assert calls == ["Q1?"]  # Q2's primary was never attempted
        records = _read_ledger(_redirect_ledger)
        assert records[0]["tier"] == "llm"
        assert "skipped" not in records[0]
        assert records[1]["tier"] == "fallback"
        assert records[1]["skipped"] == "deadline"
        assert records[1]["answer"] == "Gamma"  # first option, still answered
        captured = capsys.readouterr()
        assert "deadline budget exhausted" in captured.err
        assert '"Gamma"' in captured.out  # the hook still answers

    def test_deadline_skip_is_not_an_llm_error(self, monkeypatch,
                                               _redirect_ledger, capsys):
        """A deadline skip records no `error` and no hook_model — the
        ledger keeps deadline-skips distinguishable from LLM failures."""
        monkeypatch.setattr(hook_tools, "_remaining_budget", lambda: 1.0)
        monkeypatch.setattr(
            hook_tools, "_llm_answer",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                "primary must not be called under a busted budget")))
        tool_input = {"questions": [
            {"question": "Which?", "options": [{"label": "Alpha"}]}]}
        hook_tools._handle_ask_user(tool_input, {}, {"prompt": "p"})
        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["skipped"] == "deadline"
        assert "error" not in rec
        assert "hook_model" not in rec

    def test_tier_1_overrides_are_never_gated(self, monkeypatch,
                                              _redirect_ledger, capsys):
        monkeypatch.setattr(hook_tools, "_remaining_budget", lambda: 0.0)
        tool_input = {"questions": [
            {"question": "Which?", "options": [{"label": "Alpha"}]}]}
        hook_tools._handle_ask_user(
            tool_input, {"case_overrides": {"Which?": "Beta"}}, {"prompt": "p"})
        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["tier"] == "override"
        assert "skipped" not in rec
        assert '"Beta"' in capsys.readouterr().out


class TestLedgerWrites:
    """In-process _handle_ask_user writes one record per question."""

    def test_override_tier_recorded(self, _redirect_ledger, capsys):
        tool_input = {"questions": [
            {"question": "Which?", "options": [
                {"label": "Alpha"}, {"label": "Beta"}]},
        ]}
        hook_tools._handle_ask_user(
            tool_input, {"case_overrides": {"Which?": "Beta"}}, {"prompt": "p"})
        records = _read_ledger(_redirect_ledger)
        assert len(records) == 1
        rec = records[0]
        assert rec["tier"] == "override"
        assert rec["question"] == "Which?"
        assert rec["options"] == ["Alpha", "Beta"]
        assert rec["answer"] == "Beta"
        assert rec["ts"]
        assert "hook_model" not in rec  # no LLM attempt was made

    def test_llm_tier_records_hook_model(self, _redirect_ledger, monkeypatch,
                                         capsys):
        monkeypatch.setattr(
            hook_tools, "_llm_answer",
            lambda *a, **k: ("Alpha", {"model": "m", "match": "exact"}))
        tool_input = {"questions": [
            {"question": "Which?", "options": [{"label": "Alpha"}]},
        ]}
        hook_tools._handle_ask_user(tool_input, {}, {"prompt": "p"})
        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["tier"] == "llm"
        assert rec["hook_model"] == "m"
        assert rec["match"] == "exact"
        assert rec["answer"] == "Alpha"

    def test_fallback_after_llm_error_records_error(self, _redirect_ledger,
                                                    monkeypatch, capsys):
        monkeypatch.setattr(
            hook_tools, "_llm_answer",
            lambda *a, **k: (None, {"model": "m", "error": "boom"}))
        tool_input = {"questions": [
            {"question": "Which?", "options": [{"label": "Alpha"}]},
        ]}
        hook_tools._handle_ask_user(tool_input, {}, {"prompt": "p"})
        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["tier"] == "fallback"
        assert rec["hook_model"] == "m"
        assert rec["error"] == "boom"
        assert rec["answer"] == "Alpha"

    def test_unwritable_ledger_never_breaks_answering(self, tmp_path,
                                                      monkeypatch, capsys):
        # Best-effort contract: answers still emitted, no exception.
        monkeypatch.setattr(
            hook_tools, "_LEDGER",
            tmp_path / "no" / "such" / "dir" / "hook_answers.jsonl")
        monkeypatch.setattr(hook_tools, "_llm_answer",
                            lambda *a, **k: (None, {}))
        tool_input = {"questions": [
            {"question": "Which?", "options": [{"label": "Alpha"}]},
        ]}
        hook_tools._handle_ask_user(tool_input, {}, {"prompt": "p"})
        assert '"Alpha"' in capsys.readouterr().out
