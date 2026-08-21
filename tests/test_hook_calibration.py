"""The AskUserQuestion calibration shadow (PR9).

When a handler carries `calibration: true` and a tier-1 case_override
answers a question, the hook ALSO shadow-runs the LLM tier — HELD OUT
(answers.yaml stripped from the context) and logged into the same ledger
record's reserved `calibration` object. The shadow must never change the
injected answer, never crash the hook, and must degrade to a recorded skip
when the in-hook deadline budget is exhausted.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(
    0, str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"))

import tools as hook_tools  # noqa: E402


@pytest.fixture(autouse=True)
def _redirect_ledger(tmp_path, monkeypatch):
    """Point the provenance ledger away from the repo (see
    test_hook_llm_answer.py)."""
    ledger = tmp_path / "hook_answers.jsonl"
    monkeypatch.setattr(hook_tools, "_LEDGER", ledger)
    return ledger


def _read_ledger(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _questions(with_options=True):
    q = {"question": "Which?"}
    if with_options:
        q["options"] = [{"label": "Alpha"}, {"label": "Beta"}]
    return {"questions": [q]}


HANDLER = {"prompt": "p", "calibration": True}


class TestShadowRuns:

    def test_override_plus_calibration_records_the_shadow(self, monkeypatch,
                                                          _redirect_ledger,
                                                          capsys):
        calls = []

        def fake_llm(*args, **kwargs):
            calls.append((args, kwargs))
            return "Alpha", {"model": "m"}

        monkeypatch.setattr(hook_tools, "_llm_answer", fake_llm)
        hook_tools._handle_ask_user(
            _questions(), {"case_overrides": {"Which?": "Beta"}}, HANDLER)

        # The OVERRIDE is injected, never the shadow.
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which?": "Beta"}

        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["tier"] == "override"
        assert rec["source"] == "agent"
        cal = rec["calibration"]
        assert cal["gold"] == "Beta"
        assert cal["shadow"] == "Alpha"
        assert cal["agree"] is False
        assert cal["held_out"] is True
        assert cal["decoding"] == {"temperature": 0,
                                   "temperature_stripped": False}
        assert len(calls) == 1

    def test_shadow_agreement_is_true_on_match(self, monkeypatch,
                                               _redirect_ledger, capsys):
        monkeypatch.setattr(hook_tools, "_llm_answer",
                            lambda *a, **k: ("Beta", {"model": "m"}))
        hook_tools._handle_ask_user(
            _questions(), {"case_overrides": {"Which?": "Beta"}}, HANDLER)
        assert _read_ledger(_redirect_ledger)[0]["calibration"]["agree"] is True

    def test_shadow_context_is_held_out(self, monkeypatch, _redirect_ledger,
                                        capsys):
        """The calibration draw must not read the answer key: its
        context_files exclude answers.yaml."""
        seen = {}

        def fake_llm(*args, **kwargs):
            seen.update(kwargs)
            return "Beta", {"model": "m"}

        monkeypatch.setattr(hook_tools, "_llm_answer", fake_llm)
        hook_tools._handle_ask_user(
            _questions(), {"case_overrides": {"Which?": "Beta"}}, HANDLER)
        assert seen["context_files"] == ("input.yaml",)
        assert seen["timeout"] == hook_tools._SHADOW_TIMEOUT

    def test_temperature_strip_is_recorded_in_decoding(self, monkeypatch,
                                                       _redirect_ledger,
                                                       capsys):
        monkeypatch.setattr(
            hook_tools, "_llm_answer",
            lambda *a, **k: ("Beta", {"model": "m",
                                      "temperature_stripped": True}))
        hook_tools._handle_ask_user(
            _questions(), {"case_overrides": {"Which?": "Beta"}}, HANDLER)
        cal = _read_ledger(_redirect_ledger)[0]["calibration"]
        assert cal["decoding"]["temperature_stripped"] is True


class TestShadowNeverBreaksInjection:

    def test_shadow_api_failure_is_captured_not_raised(self, monkeypatch,
                                                       _redirect_ledger,
                                                       capsys):
        monkeypatch.setattr(
            hook_tools, "_llm_answer",
            lambda *a, **k: (None, {"model": "m", "error": "boom"}))
        hook_tools._handle_ask_user(
            _questions(), {"case_overrides": {"Which?": "Beta"}}, HANDLER)
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which?": "Beta"}
        cal = _read_ledger(_redirect_ledger)[0]["calibration"]
        assert cal["shadow"] is None
        assert cal["agree"] is None
        assert cal["error"] == "boom"

    def test_shadow_exception_is_swallowed(self, monkeypatch,
                                           _redirect_ledger, capsys):
        def boom(*a, **k):
            raise RuntimeError("exploded")

        monkeypatch.setattr(hook_tools, "_llm_answer", boom)
        hook_tools._handle_ask_user(
            _questions(), {"case_overrides": {"Which?": "Beta"}}, HANDLER)
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which?": "Beta"}
        cal = _read_ledger(_redirect_ledger)[0]["calibration"]
        assert cal["agree"] is None
        assert "exploded" in cal["error"]


class TestShadowSkips:

    def test_deadline_budget_skips_with_a_record(self, monkeypatch,
                                                 _redirect_ledger, capsys):
        monkeypatch.setattr(hook_tools, "_remaining_budget", lambda: 1.0)
        called = []
        monkeypatch.setattr(hook_tools, "_llm_answer",
                            lambda *a, **k: called.append(1))
        hook_tools._handle_ask_user(
            _questions(), {"case_overrides": {"Which?": "Beta"}}, HANDLER)
        assert not called
        cal = _read_ledger(_redirect_ledger)[0]["calibration"]
        assert cal["skipped"] == "deadline"
        assert cal["gold"] == "Beta"
        assert cal["shadow"] is None and cal["agree"] is None
        # The override is still injected.
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which?": "Beta"}

    def test_no_options_skips_without_an_llm_call(self, monkeypatch,
                                                  _redirect_ledger, capsys):
        called = []
        monkeypatch.setattr(hook_tools, "_llm_answer",
                            lambda *a, **k: called.append(1))
        hook_tools._handle_ask_user(
            _questions(with_options=False),
            {"case_overrides": {"Which?": "Beta"}}, HANDLER)
        assert not called
        cal = _read_ledger(_redirect_ledger)[0]["calibration"]
        assert cal == {"skipped": "no_options"}

    def test_calibration_disabled_makes_zero_extra_calls(self, monkeypatch,
                                                         _redirect_ledger,
                                                         capsys):
        """Cost guard: without the knob, an override answer triggers no LLM
        call at all."""
        called = []
        monkeypatch.setattr(hook_tools, "_llm_answer",
                            lambda *a, **k: called.append(1))
        hook_tools._handle_ask_user(
            _questions(), {"case_overrides": {"Which?": "Beta"}},
            {"prompt": "p"})
        assert not called
        assert "calibration" not in _read_ledger(_redirect_ledger)[0]

    def test_llm_tier_answers_never_get_a_calibration_object(self,
                                                             monkeypatch,
                                                             _redirect_ledger,
                                                             capsys):
        """The shadow exists to score the simulator against a gold override
        — an llm-tier answer has no gold, so no calibration object."""
        monkeypatch.setattr(hook_tools, "_llm_answer",
                            lambda *a, **k: ("Alpha", {"model": "m"}))
        hook_tools._handle_ask_user(_questions(), {}, HANDLER)
        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["tier"] == "llm"
        assert "calibration" not in rec


class TestProvenanceParsing:

    def _record_for(self, config, capsys, _redirect_ledger):
        hook_tools._handle_ask_user(_questions(), config, {"prompt": "p"})
        capsys.readouterr()
        return _read_ledger(_redirect_ledger)[0]

    def test_flat_entry_defaults_to_agent(self, _redirect_ledger, capsys):
        rec = self._record_for(
            {"case_overrides": {"Which?": "Beta"}}, capsys, _redirect_ledger)
        assert rec["source"] == "agent"
        assert rec["answer"] == "Beta"

    def test_file_level_human_default(self, _redirect_ledger, capsys):
        rec = self._record_for(
            {"case_overrides": {"Which?": "Beta"},
             "case_overrides_source": "human"}, capsys, _redirect_ledger)
        assert rec["source"] == "human"

    def test_per_entry_human_dict(self, _redirect_ledger, capsys):
        rec = self._record_for(
            {"case_overrides": {"Which?": {"answer": "Beta",
                                           "source": "human"}}},
            capsys, _redirect_ledger)
        assert rec["source"] == "human"
        assert rec["answer"] == "Beta"

    def test_bogus_source_normalizes_to_agent(self, _redirect_ledger, capsys):
        """Unmarked or mis-marked entries never count as human
        (conservative provenance)."""
        rec = self._record_for(
            {"case_overrides": {"Which?": {"answer": "Beta",
                                           "source": "HUMAN-ish"}}},
            capsys, _redirect_ledger)
        assert rec["source"] == "agent"

    def test_per_entry_source_beats_file_default(self, _redirect_ledger,
                                                 capsys):
        rec = self._record_for(
            {"case_overrides": {"Which?": {"answer": "Beta",
                                           "source": "agent"}},
             "case_overrides_source": "human"}, capsys, _redirect_ledger)
        assert rec["source"] == "agent"

    def test_dict_entry_without_answer_falls_through_tiers(self, monkeypatch,
                                                           _redirect_ledger,
                                                           capsys):
        monkeypatch.setattr(hook_tools, "_llm_answer",
                            lambda *a, **k: ("Alpha", {"model": "m"}))
        rec = self._record_for(
            {"case_overrides": {"Which?": {"source": "human"}}},
            capsys, _redirect_ledger)
        assert rec["tier"] == "llm"
        assert "source" not in rec
