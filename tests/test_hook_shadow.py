"""Cross-family shadow simulators (models.hook_shadow, PR10).

Config surface: `models.hook_shadow` — at most 2 distinct, non-empty model
ids, none equal to `models.hook`. Serialization: `merge_handler_knobs`
carries `hook_shadow_models` onto tool_handlers.yaml from BOTH handler
sources (heuristic build_handlers AND a pre-resolved file) plus the in-repo
mirror. Hook behavior: EVERY intercepted AskUserQuestion — whatever tier
answered it — is also put to each shadow model with the question's NORMAL
context (NOT held out: shadows measure cross-simulator agreement, not
answer-key independence), logged into the record's `shadows` array, never
injected, and skipped FIRST (before the calibration shadow) when the
in-hook deadline budget runs tight.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import tools as hook_tools  # noqa: E402
import workspace  # noqa: E402

from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.tools.interception import (  # noqa: E402
    build_handlers, generate_interception, merge_handler_knobs,
)


def _write(tmp_path, body, name="eval.yaml"):
    p = tmp_path / name
    p.write_text(body)
    return p


def _config(tmp_path, *, hook="claude-haiku-4-5",
            hook_shadow=("gemini-2.5-flash",), tools=True):
    raw = {
        "name": "t",
        "execution": {"skill": "s"},
        "models": {},
    }
    if hook:
        raw["models"]["hook"] = hook
    if hook_shadow is not None:
        raw["models"]["hook_shadow"] = list(hook_shadow)
    if tools:
        raw["inputs"] = {"tools": [{
            "match": "Questions asked to the user via AskUserQuestion.",
            "prompt": "answer from input.yaml",
        }]}
    return EvalConfig.from_yaml(
        _write(tmp_path, yaml.safe_dump(raw, sort_keys=False)))


# --- config validation --------------------------------------------------------

def test_hook_shadow_defaults_to_empty_list(tmp_path):
    cfg = _config(tmp_path, hook_shadow=None)
    assert cfg.models.hook_shadow == []


def test_hook_shadow_parses_up_to_two_entries(tmp_path):
    cfg = _config(tmp_path, hook_shadow=["gemini-2.5-flash", "gpt-4o"])
    assert cfg.models.hook_shadow == ["gemini-2.5-flash", "gpt-4o"]


@pytest.mark.parametrize("shadow,match", [
    (["a", "b", "c"], r"at most 2"),
    (["gemini-2.5-flash", "gemini-2.5-flash"], r"distinct"),
    (["claude-haiku-4-5"], r"duplicates models\.hook"),
    ([""], r"non-empty string"),
    (["   "], r"non-empty string"),
    ([42], r"non-empty string"),
    ("gemini-2.5-flash", r"must be a list"),
], ids=["three-entries", "duplicate", "equals-hook", "empty-string",
        "whitespace", "non-string", "not-a-list"])
def test_hook_shadow_invalid_values_rejected(tmp_path, shadow, match):
    with pytest.raises(ValueError, match=match):
        _config(tmp_path, hook_shadow=shadow)


def test_hook_shadow_without_interception_warns(tmp_path):
    with pytest.warns(UserWarning, match="inputs.tools is empty"):
        _config(tmp_path, tools=False)


def test_hook_shadow_with_interception_loads_quietly(tmp_path):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg = _config(tmp_path)
    assert cfg.models.hook_shadow == ["gemini-2.5-flash"]


# --- serialization onto tool_handlers.yaml -------------------------------------

RESOLVED = {
    "handlers": [{
        "match": "Questions asked to the user via AskUserQuestion.",
        "patterns": ["AskUserQuestion"],
        "prompt": "answer from input.yaml",
    }],
    "case_overrides": {"What priority?": "Normal"},
}


def _generated(tmp_path, config, resolved=None):
    target = tmp_path / "ws"
    target.mkdir(exist_ok=True)
    resolved_path = None
    if resolved is not None:
        resolved_path = tmp_path / "resolved" / "tool_handlers.yaml"
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(yaml.safe_dump(resolved, sort_keys=False))
    generate_interception(target, config, "python3 hooks/tools.py",
                          resolved_handlers_path=resolved_path)
    return yaml.safe_load((target / "tool_handlers.yaml").read_text())


def test_heuristic_branch_carries_hook_shadow_models(tmp_path):
    handlers = _generated(tmp_path, _config(tmp_path))
    assert handlers["hook_shadow_models"] == ["gemini-2.5-flash"]


def test_resolved_branch_carries_hook_shadow_models(tmp_path, capsys):
    handlers = _generated(tmp_path, _config(tmp_path), resolved=RESOLVED)
    assert handlers["hook_shadow_models"] == ["gemini-2.5-flash"]
    # the resolved file's own keys still flow through untouched
    assert handlers["case_overrides"] == {"What priority?": "Normal"}


def test_eval_yaml_overwrites_a_stale_resolved_value(tmp_path):
    """eval.yaml owns the models: a hook_shadow_models value left in a
    resolved file is overwritten, never setdefault'd (unlike hook_model)."""
    stale = dict(RESOLVED)
    stale["hook_shadow_models"] = ["some-old-model"]
    handlers = _generated(tmp_path, _config(tmp_path), resolved=stale)
    assert handlers["hook_shadow_models"] == ["gemini-2.5-flash"]


def test_no_hook_shadow_leaves_the_key_out(tmp_path):
    handlers = _generated(tmp_path, _config(tmp_path, hook_shadow=None))
    assert "hook_shadow_models" not in handlers


def test_in_repo_mirror_carries_hook_shadow_models(tmp_path):
    """_setup_in_repo_tool_hooks routes through the SAME merge helper, so
    the in-repo path cannot drift."""
    config = _config(tmp_path)
    case_ws = tmp_path / "case_ws"
    (case_ws / "hooks").mkdir(parents=True)
    workspace._setup_in_repo_tool_hooks(case_ws, config, {})
    written = yaml.safe_load((case_ws / "tool_handlers.yaml").read_text())
    assert written["hook_shadow_models"] == ["gemini-2.5-flash"]
    expected, _ = build_handlers(config)
    assert written == merge_handler_knobs(expected, config)


# --- hook behavior --------------------------------------------------------------

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


SHADOWED = {"hook_shadow_models": ["shadow-a", "shadow-b"]}
HANDLER = {"prompt": "p"}


def _fake_llm(calls, answers_by_model=None):
    def fake(question, options, prompt, model=None, **kwargs):
        calls.append({"model": model, **kwargs})
        answer = (answers_by_model or {}).get(model, "Alpha")
        return answer, {"model": model or "default"}
    return fake


class TestShadowQueriesEveryTier:

    def test_override_tier_queries_every_shadow(self, monkeypatch,
                                                _redirect_ledger, capsys):
        calls = []
        monkeypatch.setattr(hook_tools, "_llm_answer", _fake_llm(calls))
        config = dict(SHADOWED, case_overrides={"Which?": "Beta"})
        hook_tools._handle_ask_user(_questions(), config, HANDLER)
        # no calibration knob -> exactly the 2 shadow calls, no primary
        assert [c["model"] for c in calls] == ["shadow-a", "shadow-b"]
        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["tier"] == "override"
        assert rec["shadows"] == [
            {"model": "shadow-a", "answer": "Alpha", "held_out": False},
            {"model": "shadow-b", "answer": "Alpha", "held_out": False},
        ]
        # the override is injected — never a shadow answer
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which?": "Beta"}

    def test_llm_tier_queries_primary_then_shadows(self, monkeypatch,
                                                   _redirect_ledger, capsys):
        calls = []
        monkeypatch.setattr(
            hook_tools, "_llm_answer",
            _fake_llm(calls, answers_by_model={None: "Beta"}))
        hook_tools._handle_ask_user(_questions(), dict(SHADOWED), HANDLER)
        assert [c["model"] for c in calls] == [None, "shadow-a", "shadow-b"]
        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["tier"] == "llm"
        assert rec["answer"] == "Beta"
        assert [s["answer"] for s in rec["shadows"]] == ["Alpha", "Alpha"]

    def test_fallback_tier_still_queries_shadows(self, monkeypatch,
                                                 _redirect_ledger, capsys):
        def fake(question, options, prompt, model=None, **kwargs):
            if model is None:  # primary LLM attempt fails
                return None, {"model": "default", "error": "boom"}
            return "Beta", {"model": model}

        monkeypatch.setattr(hook_tools, "_llm_answer", fake)
        hook_tools._handle_ask_user(_questions(), dict(SHADOWED), HANDLER)
        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["tier"] == "fallback"
        assert [s["answer"] for s in rec["shadows"]] == ["Beta", "Beta"]

    def test_shadow_context_is_not_held_out(self, monkeypatch,
                                            _redirect_ledger, capsys):
        """Shadows measure cross-simulator agreement under the question's
        NORMAL conditions — answers.yaml stays in the context (unlike the
        held-out calibration shadow)."""
        calls = []
        monkeypatch.setattr(hook_tools, "_llm_answer", _fake_llm(calls))
        config = dict(SHADOWED, case_overrides={"Which?": "Beta"})
        hook_tools._handle_ask_user(_questions(), config, HANDLER)
        for c in calls:
            assert c["context_files"] == ("input.yaml", "answers.yaml")
            assert c["timeout"] == hook_tools._XSHADOW_TIMEOUT
        rec = _read_ledger(_redirect_ledger)[0]
        assert all(s["held_out"] is False for s in rec["shadows"])

    def test_at_most_two_shadow_models_are_queried(self, monkeypatch,
                                                   _redirect_ledger, capsys):
        """Defense in depth: a hand-edited handler file listing 3 models
        still costs at most 2 calls per question."""
        calls = []
        monkeypatch.setattr(hook_tools, "_llm_answer", _fake_llm(calls))
        config = {"hook_shadow_models": ["s1", "s2", "s3"],
                  "case_overrides": {"Which?": "Beta"}}
        hook_tools._handle_ask_user(_questions(), config, HANDLER)
        assert [c["model"] for c in calls] == ["s1", "s2"]

    def test_no_shadow_models_makes_zero_extra_calls(self, monkeypatch,
                                                     _redirect_ledger,
                                                     capsys):
        calls = []
        monkeypatch.setattr(hook_tools, "_llm_answer", _fake_llm(calls))
        hook_tools._handle_ask_user(
            _questions(), {"case_overrides": {"Which?": "Beta"}}, HANDLER)
        assert calls == []
        assert "shadows" not in _read_ledger(_redirect_ledger)[0]


class TestShadowNeverBreaksInjection:

    def test_shadow_error_is_captured_not_raised(self, monkeypatch,
                                                 _redirect_ledger, capsys):
        monkeypatch.setattr(
            hook_tools, "_llm_answer",
            lambda *a, **k: (None, {"model": "m", "error": "boom"}))
        config = dict(SHADOWED, case_overrides={"Which?": "Beta"})
        hook_tools._handle_ask_user(_questions(), config, HANDLER)
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which?": "Beta"}
        shadows = _read_ledger(_redirect_ledger)[0]["shadows"]
        assert all(s["answer"] is None and s["error"] == "boom"
                   for s in shadows)

    def test_shadow_exception_is_swallowed(self, monkeypatch,
                                           _redirect_ledger, capsys):
        def boom(*a, **k):
            raise RuntimeError("exploded")

        monkeypatch.setattr(hook_tools, "_llm_answer", boom)
        config = dict(SHADOWED, case_overrides={"Which?": "Beta"})
        hook_tools._handle_ask_user(_questions(), config, HANDLER)
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which?": "Beta"}
        shadows = _read_ledger(_redirect_ledger)[0]["shadows"]
        assert all(s["answer"] is None and "exploded" in s["error"]
                   for s in shadows)

    def test_no_options_skips_without_an_llm_call(self, monkeypatch,
                                                  _redirect_ledger, capsys):
        calls = []
        monkeypatch.setattr(hook_tools, "_llm_answer", _fake_llm(calls))
        config = dict(SHADOWED, case_overrides={"Which?": "Beta"})
        hook_tools._handle_ask_user(_questions(with_options=False), config,
                                    HANDLER)
        assert calls == []
        assert _read_ledger(_redirect_ledger)[0]["shadows"] == [
            {"model": "shadow-a", "skipped": "no_options"},
            {"model": "shadow-b", "skipped": "no_options"},
        ]


class TestDeadlineOrdering:
    """Shadows are the FIRST thing the deadline budget skips: their skip
    floor reserves the calibration shadow's slice
    (_XSHADOW_TIMEOUT + _SHADOW_TIMEOUT), so at any remaining budget the
    cross-simulator shadows yield before the calibration shadow does."""

    CONFIG = dict(SHADOWED, case_overrides={"Which?": "Beta"})
    CAL_HANDLER = {"prompt": "p", "calibration": True}

    def _run(self, monkeypatch, budget):
        calls = []
        monkeypatch.setattr(hook_tools, "_remaining_budget", lambda: budget)
        monkeypatch.setattr(hook_tools, "_llm_answer", _fake_llm(calls))
        hook_tools._handle_ask_user(_questions(), dict(self.CONFIG),
                                    self.CAL_HANDLER)
        return calls

    def test_tight_budget_skips_shadows_but_runs_calibration(
            self, monkeypatch, _redirect_ledger, capsys):
        # 15 <= budget < 25: the calibration shadow still fits, the
        # cross-simulator shadows already yield.
        calls = self._run(monkeypatch, 20.0)
        assert [c["timeout"] for c in calls] == [hook_tools._SHADOW_TIMEOUT]
        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["calibration"]["shadow"] == "Alpha"
        assert rec["shadows"] == [
            {"model": "shadow-a", "skipped": "deadline"},
            {"model": "shadow-b", "skipped": "deadline"},
        ]

    def test_comfortable_budget_runs_both(self, monkeypatch,
                                          _redirect_ledger, capsys):
        calls = self._run(monkeypatch, 90.0)
        assert [c["timeout"] for c in calls] == [
            hook_tools._SHADOW_TIMEOUT,
            hook_tools._XSHADOW_TIMEOUT, hook_tools._XSHADOW_TIMEOUT]

    def test_exhausted_budget_skips_both_with_records(self, monkeypatch,
                                                      _redirect_ledger,
                                                      capsys):
        calls = self._run(monkeypatch, 1.0)
        assert calls == []
        rec = _read_ledger(_redirect_ledger)[0]
        assert rec["calibration"]["skipped"] == "deadline"
        assert all(s["skipped"] == "deadline" for s in rec["shadows"])
        # the override is still injected
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which?": "Beta"}

    def test_skip_floors_guarantee_the_ordering(self):
        """The constants themselves enforce 'shadows first': the shadow
        skip floor sits strictly above the calibration shadow's."""
        assert (hook_tools._XSHADOW_TIMEOUT + hook_tools._SHADOW_TIMEOUT
                > hook_tools._SHADOW_TIMEOUT)
