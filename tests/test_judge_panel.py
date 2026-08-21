"""Cross-family judge panels (judges[].model as a list) — P4.

Covers the config surface (list parsing, panel validity, the Q3 load
warning), the panel execution path (k samples per model, per-model reduction
before the cross-model majority/median, error-as-missing), the cross-case
panel alpha aggregation, the ``min_panel_alpha`` gate's three-state
semantics with its ``include_irr`` scoping, the MLflow-fallback panel
refusal, and the report badge.
"""

import sys
import threading
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

from agent_eval.config import EvalConfig, JudgeConfig  # noqa: E402
from agent_eval.reliability import (  # noqa: E402
    NOMINAL, ORDINAL, REASON_PERFECT_AGREEMENT, IRRResult,
)
import report  # noqa: E402
import score  # noqa: E402
from score import (  # noqa: E402
    PANEL_ALPHA_LABEL, PANEL_SINGLE_FAMILY_SUFFIX, _PanelScorer,
    _score_panel, detect_regressions, score_cases,
)


def _write(tmp_path, text):
    p = tmp_path / "eval.yaml"
    p.write_text(text)
    return p


# ---------------------------------------------------------------------------
# Config: judges[].model as a list
# ---------------------------------------------------------------------------

class TestPanelConfig:
    def test_string_model_is_byte_identical(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
judges:
  - {name: q, llm_rubric: score it, feedback_type: bool, model: claude-x}
"""))
        assert cfg.judges[0].model == "claude-x"
        assert cfg.judges[0].panel_models == []

    def test_explicit_yaml_null_model_still_loads(self, tmp_path):
        """A bare `model:` key (YAML null) loaded before lists existed and
        must keep loading — None normalizes BEFORE the type check."""
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
judges:
  - name: q
    llm_rubric: score it
    feedback_type: bool
    model:
"""))
        assert not cfg.judges[0].model
        assert cfg.judges[0].panel_models == []

    def test_list_model_becomes_a_panel(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
judges:
  - name: q
    llm_rubric: score it
    feedback_type: bool
    model: [claude-x, gpt-4o, gemini-2.5-pro]
"""))
        assert cfg.judges[0].model == "claude-x"
        assert cfg.judges[0].panel_models == [
            "claude-x", "gpt-4o", "gemini-2.5-pro"]

    def test_one_item_list_normalizes_to_a_plain_model(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
judges:
  - {name: q, llm_rubric: score it, feedback_type: bool, model: [claude-x]}
"""))
        assert cfg.judges[0].model == "claude-x"
        assert cfg.judges[0].panel_models == []

    @pytest.mark.parametrize("bad,match", [
        ("[]", "cannot be empty"),
        ("[claude-x, 3]", "non-empty strings"),
        ('[claude-x, ""]', "non-empty strings"),
        ("[claude-x, claude-x]", "duplicate model"),
        ("[m1, m2, m3, m4, m5]", "2-4 models"),
    ])
    def test_bad_lists_raise(self, tmp_path, bad, match):
        with pytest.raises(ValueError, match=match):
            EvalConfig.from_yaml(_write(tmp_path, f"""
name: t
execution: {{skill: s}}
judges:
  - {{name: q, llm_rubric: score it, feedback_type: bool, model: {bad}}}
"""))

    def test_non_string_non_list_model_raises(self, tmp_path):
        with pytest.raises(ValueError, match="string or a list"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
judges:
  - {name: q, llm_rubric: score it, feedback_type: bool, model: 42}
"""))

    @pytest.mark.parametrize("impl,match", [
        ('check: "return (True, \'ok\')"', "not valid on a check judge"),
        ("module: m\n    function: f", "not valid on a module judge"),
        ("builtin: quality/output_completeness",
         "not valid on a builtin judge"),
    ])
    def test_panel_on_a_non_llm_judge_raises(self, tmp_path, impl, match):
        with pytest.raises(ValueError, match=match):
            EvalConfig.from_yaml(_write(tmp_path, f"""
name: t
execution: {{skill: s}}
judges:
  - name: q
    {impl}
    model: [m1, m2]
"""))

    def test_panel_on_an_agent_judge_raises(self, tmp_path):
        """The agent-judge runner path is pinned to one model per judge —
        a panel there would silently run one model, so it is rejected."""
        with pytest.raises(ValueError, match="not supported for agent"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
judges:
  - name: q
    llm_rubric: score it
    feedback_type: bool
    agent: {allowed_tools: [Read]}
    model: [m1, m2]
"""))


# ---------------------------------------------------------------------------
# Q3: consequence tiers inject min_alpha ONLY — panel gates are explicit
# ---------------------------------------------------------------------------

class TestConsequencePanelWarning:
    CONFIG = """
name: t
execution: {{skill: s}}
judges:
  - name: q
    llm_rubric: score it
    feedback_type: bool
    samples: 3
    consequence: safety
    model: [claude-x, gpt-4o, gemini-2.5-pro]
{thresholds}
"""

    def test_consequence_panel_without_min_panel_alpha_warns(self, tmp_path):
        with pytest.warns(UserWarning,
                          match=r"panel alpha is NOT tier-gated"):
            EvalConfig.from_yaml(_write(
                tmp_path, self.CONFIG.format(thresholds="")))

    def test_explicit_min_panel_alpha_silences_the_warning(self, tmp_path):
        import warnings as _warnings
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            EvalConfig.from_yaml(_write(tmp_path, self.CONFIG.format(
                thresholds="thresholds:\n  q: {min_panel_alpha: 0.67}")))
        assert not [w for w in caught
                    if "NOT tier-gated" in str(w.message)]

    def test_no_tier_injection_for_min_panel_alpha(self, tmp_path):
        """effective_thresholds() injects min_alpha only (Q3)."""
        with pytest.warns(UserWarning):
            cfg = EvalConfig.from_yaml(_write(
                tmp_path, self.CONFIG.format(thresholds="")))
        assert cfg.effective_thresholds()["q"] == {"min_alpha": 0.70}


# ---------------------------------------------------------------------------
# Execution: _score_panel — k per model, reduce per model, then across models
# ---------------------------------------------------------------------------

class _FakePanel:
    """Panel-scorer stand-in: scripted per-model draw sequences."""

    def __init__(self, behavior):
        self.panel_models = list(behavior)
        self._behavior = {m: list(vs) for m, vs in behavior.items()}

    def for_model(self, model):
        draws = iter(self._behavior[model])

        def scorer(outputs=None, **kwargs):
            v = next(draws)
            if isinstance(v, Exception):
                raise v
            return v, f"{model} says {v}"

        return scorer


def _run_panel(behavior, n_samples=1, bounds=None, judge_type="llm"):
    scorer = _FakePanel(behavior)
    return _score_panel(scorer, {}, scorer.panel_models, n_samples, bounds,
                        "q", judge_type, "case-001")


class TestScorePanel:
    def test_bool_majority_two_to_one(self):
        rec = _run_panel({"a": [True], "b": [False], "c": [True]})
        assert rec["value"] is True
        assert rec["panel"]["models"] == ["a", "b", "c"]
        assert rec["panel"]["values"] == {"a": True, "b": False, "c": True}
        assert rec["panel"]["samples"] == {"a": [True], "b": [False],
                                           "c": [True]}

    def test_bool_tie_resolves_to_fail(self):
        rec = _run_panel({"a": [True], "b": [False]})
        assert rec["value"] is False

    def test_numeric_median_low_over_per_model_reduced(self):
        rec = _run_panel({"a": [2], "b": [5], "c": [3]})
        assert rec["value"] == 3  # median_low, an actually-observed score

    def test_errored_model_is_missing_and_majority_over_survivors(self):
        rec = _run_panel({"a": [True], "b": [RuntimeError("api down")],
                          "c": [True]})
        assert rec["value"] is True
        assert rec["panel"]["values"]["b"] is None
        assert rec["panel"]["samples"]["b"] == [None]

    def test_all_models_errored_is_an_error_record(self):
        rec = _run_panel({"a": [RuntimeError("x")], "b": [RuntimeError("y")]})
        assert rec["value"] is None
        assert rec["error"]

    def test_k_samples_reduce_per_model_before_the_cross_model_pass(self):
        # a: median_low(4, 4, 5) = 4; b: median_low(1, 1, 1) = 1.
        # Cross-model median_low over the REDUCED values [4, 1] is 1 —
        # never the median over the pooled 6 raw draws (which would be 4).
        rec = _run_panel({"a": [4, 4, 5], "b": [1, 1, 1]}, n_samples=3)
        assert rec["value"] == 1
        assert rec["panel"]["values"] == {"a": 4, "b": 1}
        assert rec["panel"]["samples"] == {"a": [4, 4, 5], "b": [1, 1, 1]}

    def test_off_scale_value_is_an_error_sample_never_clamped(self):
        rec = _run_panel({"a": [7], "b": [2], "c": [2]},
                         bounds=(1, 5, True))
        assert rec["panel"]["values"]["a"] is None
        assert rec["panel"]["samples"]["a"] == [None]
        assert rec["value"] == 2

    def test_sample_rationales_carry_model_prefixes(self):
        rec = _run_panel({"a": [True], "b": [False]})
        prefixes = [r["rationale"].split("]")[0] + "]"
                    for r in rec["sample_rationales"]]
        assert prefixes == ["[a]", "[b]"]

    def test_no_top_level_stability_key(self):
        """Model disagreement is not sampling instability — the cross-case
        stable_cases block must not conflate the two."""
        rec = _run_panel({"a": [4, 4], "b": [2, 2]}, n_samples=2)
        assert "stability" not in rec


# ---------------------------------------------------------------------------
# _PanelScorer facade
# ---------------------------------------------------------------------------

def test_panel_scorer_caches_and_falls_back_to_the_first_member():
    made = []

    def make(model):
        made.append(model)
        return lambda outputs=None, **kw: (model, "r")

    ps = _PanelScorer(["m1", "m2"], make)
    assert ps.for_model("m2")(outputs={})[0] == "m2"
    assert ps.for_model("m2")(outputs={})[0] == "m2"
    assert made.count("m2") == 1  # cached
    assert ps(outputs={})[0] == "m1"  # direct call = first member


# ---------------------------------------------------------------------------
# Aggregation: cross-case panel alpha through score_cases
# ---------------------------------------------------------------------------

def _panel_config(judge):
    config = EvalConfig()
    config.judges.append(judge)
    return config


def _case_dirs(tmp_path, n):
    dirs = []
    for i in range(n):
        d = tmp_path / "cases" / f"case-{i + 1:03d}"
        d.mkdir(parents=True)
        dirs.append(d)
    return dirs


def _case_scripted_panel(models, ratings):
    """Real _PanelScorer over a factory scripted per (model, case)."""
    calls = defaultdict(int)
    lock = threading.Lock()

    def make(model):
        def scorer(outputs=None, **kwargs):
            cid = Path((outputs or {})["case_dir"]).name
            with lock:
                i = calls[(model, cid)]
                calls[(model, cid)] += 1
            v = ratings[model][cid][i]
            if isinstance(v, Exception):
                raise v
            return v, f"{model}/{cid} sample {i}"
        return scorer

    return _PanelScorer(models, make)


class TestPanelAggregation:
    def test_panel_block_shape_and_unknown_families(self, tmp_path):
        models = ["m1", "m2", "m3"]
        jc = JudgeConfig(name="q", llm_rubric="score it",
                         feedback_type="bool", model="m1",
                         panel_models=models)
        config = _panel_config(jc)
        case_dirs = _case_dirs(tmp_path, 4)
        ratings = {m: {f"case-{i:03d}": [i % 2 == 0] for i in range(1, 5)}
                   for m in models}
        ratings["m3"]["case-002"] = [True]  # one disagreement
        judges = [("q", _case_scripted_panel(models, ratings), "", "llm", 1)]

        results = score_cases(judges, case_dirs, config)
        panel = results["aggregated"]["q"]["panel"]
        assert panel["metric"] == "krippendorff_alpha"
        assert panel["level"] == NOMINAL
        assert panel["models"] == models  # from config, never the first case
        assert panel["families"] == {"unknown": 3}
        assert panel["label"] == PANEL_ALPHA_LABEL  # unknown => no suffix
        assert panel["n_units"] == 4
        assert panel["k_samples"] == 1
        assert isinstance(panel["value"], float)
        for k in ("reason_code", "reason", "rationale"):
            assert k in panel

    def test_single_known_family_gets_the_caveat_suffix(self, tmp_path):
        models = ["claude-sonnet-4-5", "claude-haiku-4-5"]
        jc = JudgeConfig(name="q", llm_rubric="score it",
                         feedback_type="bool", model=models[0],
                         panel_models=models)
        config = _panel_config(jc)
        case_dirs = _case_dirs(tmp_path, 2)
        ratings = {m: {"case-001": [True], "case-002": [True]}
                   for m in models}
        judges = [("q", _case_scripted_panel(models, ratings), "", "llm", 1)]

        results = score_cases(judges, case_dirs, config)
        panel = results["aggregated"]["q"]["panel"]
        assert panel["families"] == {"anthropic": 2}
        assert panel["label"] == (PANEL_ALPHA_LABEL
                                  + PANEL_SINGLE_FAMILY_SUFFIX)
        # ALL ratings identical -> zero expected disagreement -> the healthy
        # degenerate, a reason code rather than a fabricated 1.0.
        assert panel["value"] is None
        assert panel["reason_code"] == REASON_PERFECT_AGREEMENT

    def test_cases_by_models_matrix_with_error_as_missing(self, tmp_path,
                                                          monkeypatch):
        models = ["m1", "m2", "m3"]
        jc = JudgeConfig(name="q", llm_rubric="score it",
                         score_range=[1.0, 5.0], model="m1",
                         panel_models=models)
        config = _panel_config(jc)
        case_dirs = _case_dirs(tmp_path, 3)
        ratings = {
            "m1": {"case-001": [4], "case-002": [2], "case-003": [5]},
            "m2": {"case-001": [4], "case-002": [3], "case-003": [5]},
            "m3": {"case-001": [RuntimeError("down")],
                   "case-002": [RuntimeError("down")],
                   "case-003": [RuntimeError("down")]},
        }
        captured = {}
        real = score.krippendorff_alpha

        def spy(units, level=NOMINAL, **kwargs):
            captured["units"] = sorted(list(units), key=str)
            captured["level"] = level
            return real(units, level, **kwargs)

        monkeypatch.setattr(score, "krippendorff_alpha", spy)
        judges = [("q", _case_scripted_panel(models, ratings), "", "llm", 1)]
        results = score_cases(judges, case_dirs, config)

        # Rows = cases, columns = the CONFIG's model order; the errored
        # model is a missing rating (None), never a category.
        assert captured["level"] == ORDINAL
        assert captured["units"] == sorted(
            [[4, 4, None], [2, 3, None], [5, 5, None]], key=str)
        panel = results["aggregated"]["q"]["panel"]
        assert isinstance(panel["value"], float)

    def test_panel_survives_the_summary_merge(self, tmp_path):
        models = ["m1", "m2"]
        jc = JudgeConfig(name="q", llm_rubric="score it",
                         feedback_type="bool", model="m1",
                         panel_models=models)
        config = _panel_config(jc)
        case_dirs = _case_dirs(tmp_path, 2)
        ratings = {"m1": {"case-001": [True], "case-002": [False]},
                   "m2": {"case-001": [False], "case-002": [False]}}
        judges = [("q", _case_scripted_panel(models, ratings), "", "llm", 1)]
        results = score_cases(judges, case_dirs, config)

        runs_dir = tmp_path / "runs"
        (runs_dir / "r1").mkdir(parents=True)
        score._merge_summary(
            "r1", "judges",
            score._strip_judge_values(results["aggregated"]), runs_dir)
        merged = yaml.safe_load(
            (runs_dir / "r1" / "summary.yaml").read_text())["judges"]["q"]
        assert "values" not in merged
        assert merged["panel"]["models"] == models


# ---------------------------------------------------------------------------
# Gating: min_panel_alpha three-state semantics + include_irr scoping
# ---------------------------------------------------------------------------

def _panel_agg(**panel):
    return {"q": {"mean": 4.0, "scored_cases": 4, "panel": panel}}


class TestMinPanelAlpha:
    THRESH = {"q": {"min_panel_alpha": 0.67}}

    def test_breach(self):
        regs = detect_regressions(
            _panel_agg(value=0.4, metric="krippendorff_alpha",
                       level="nominal", n_units=4,
                       models=["a", "b"], families={"unknown": 2},
                       label=PANEL_ALPHA_LABEL),
            self.THRESH)
        assert [r.metric for r in regs] == ["panel_alpha"]
        assert "cross-model panel alpha" in regs[0].detail

    def test_pass(self):
        assert detect_regressions(
            _panel_agg(value=0.8, n_units=4), self.THRESH) == []

    def test_perfect_agreement_degenerate_passes(self):
        assert detect_regressions(
            _panel_agg(value=None, reason_code="perfect_agreement",
                       reason="all ratings identical"),
            self.THRESH) == []

    def test_configured_but_unavailable_regresses(self):
        regs = detect_regressions(
            {"q": {"mean": 4.0, "scored_cases": 4}}, self.THRESH)
        assert [r.metric for r in regs] == ["panel_alpha"]
        assert "judges[].model" in regs[0].detail

    def test_include_irr_false_skips_the_gate(self):
        """Harbor/EvalHub scoping: the same breach is silently skipped."""
        breached = _panel_agg(value=0.4, n_units=4)
        assert detect_regressions(breached, self.THRESH,
                                  include_irr=False) == []
        # ... and the harbor-mode report agrees with the CLI.
        summary = {"judges": breached, "per_case": {}}
        config = {"thresholds": self.THRESH, "judges": []}
        assert "FAIL" in report._render_scoring_summary(summary, config)
        assert "FAIL" not in report._render_scoring_summary(
            summary, config, run_result={"execution_mode": "harbor"})
        assert not report._render_regressions(
            summary, config, run_result={"execution_mode": "harbor"})

    def test_breach_detail_names_the_families(self):
        regs = detect_regressions(
            _panel_agg(value=0.1, families={"anthropic": 3},
                       label=PANEL_ALPHA_LABEL + PANEL_SINGLE_FAMILY_SUFFIX),
            self.THRESH)
        assert "anthropic x3" in regs[0].detail
        assert "single-family panel" in regs[0].detail


# ---------------------------------------------------------------------------
# MLflow make_judge fallback refuses panels
# ---------------------------------------------------------------------------

def test_make_judge_fallback_rejects_panels(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_VERTEX_PROJECT_ID"):
        monkeypatch.delenv(var, raising=False)
    jc = JudgeConfig(name="q", llm_rubric="score it", feedback_type="bool",
                     model="m1", panel_models=["m1", "m2"])
    config = EvalConfig()
    config.judges.append(jc)
    with pytest.raises(RuntimeError, match="ANTHROPIC_BASE_URL"):
        score._load_llm_judge(jc, config, tmp_path)


def test_anthropic_path_returns_a_panel_scorer(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    jc = JudgeConfig(name="q", llm_rubric="score it", feedback_type="bool",
                     model="m1", panel_models=["m1", "m2"])
    config = EvalConfig()
    config.judges.append(jc)
    scorer = score._load_llm_judge(jc, config, tmp_path)
    assert isinstance(scorer, _PanelScorer)
    assert scorer.panel_models == ["m1", "m2"]


def test_single_model_scorer_exposes_for_model(monkeypatch, tmp_path):
    """`score.py clarity` re-rates cases with arbitrary rater models via
    the judge's own call path."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    jc = JudgeConfig(name="q", llm_rubric="score it", feedback_type="bool",
                     model="m1")
    config = EvalConfig()
    config.judges.append(jc)
    scorer = score._load_llm_judge(jc, config, tmp_path)
    assert callable(scorer.for_model("other-model"))


# ---------------------------------------------------------------------------
# Report smoke: panel badge + list-model column
# ---------------------------------------------------------------------------

def test_report_renders_the_panel_badge_and_no_list_repr():
    summary = {
        "judges": {"q": {
            "mean": 4.0, "scored_cases": 4,
            "panel": {"metric": "krippendorff_alpha", "level": "ordinal",
                      "value": 0.712, "reason_code": None, "reason": None,
                      "n_units": 4, "label": PANEL_ALPHA_LABEL,
                      "rationale": "r", "models": ["claude-x", "gpt-4o"],
                      "families": {"anthropic": 1, "openai": 1},
                      "k_samples": 1},
        }},
        "per_case": {},
    }
    config = {"judges": [{"name": "q", "prompt": "score it",
                          "model": ["claude-x", "gpt-4o"]}]}
    html = report._render_scoring_summary(summary, config)
    assert "panel α = 0.712" in html
    assert "panel: claude-x, gpt-4o" in html
    assert "[&#x27;" not in html and "['" not in html  # no list repr


def test_report_threshold_column_shows_min_panel_alpha():
    summary = {"judges": {"q": {"mean": 4.0, "scored_cases": 4,
                                "panel": {"value": 0.9, "n_units": 4}}},
               "per_case": {}}
    config = {"judges": [], "thresholds": {"q": {"min_panel_alpha": 0.67}}}
    html = report._render_scoring_summary(summary, config)
    assert "panel &alpha; 0.67" in html
    # Skipped on harbor: the bound the detector never checked is not shown.
    harbor = report._render_scoring_summary(
        summary, config, run_result={"execution_mode": "harbor"})
    assert "panel &alpha; 0.67" not in harbor
