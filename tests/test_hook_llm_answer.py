"""Tests for the AskUserQuestion hook's LLM answering call.

The hook is the one LLM call in the harness whose failure is *invisible*:
`_llm_answer` swallows every exception and the caller falls through to the
first option, so a rejected request feeds the agent under test an unvetted
answer while the run still reports a pass. These tests pin the two behaviors
that keep that from happening silently — the sampling-parameter retry, and the
stderr announcement on the fallback tier.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(
    0, str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"))

import tools as hook_tools


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
        monkeypatch.setattr(hook_tools, "_llm_answer", lambda *a, **k: None)
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
        monkeypatch.setattr(hook_tools, "_llm_answer", lambda *a, **k: None)
        tool_input = {"questions": [
            {"question": "Which?", "options": [{"label": "Alpha"}]},
        ]}
        hook_tools._handle_ask_user(
            tool_input, {"case_overrides": {"Which?": "Beta"}}, {"prompt": "p"})
        captured = capsys.readouterr()
        assert "AskUserQuestion fallback" not in captured.err
        assert '"Beta"' in captured.out
