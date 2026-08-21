"""The report and the MLflow tag must agree with the CLI's exit code.

`score.py::detect_regressions` is the gate CI exits on. `report.py` and
`log_results.py` each reimplemented a subset of its rules and had drifted to
two of the four threshold keys, so a run could exit 1 on `min_win_rate` or
`max_error_rate` while report.html showed no Regressions table and MLflow
tagged `regressions_detected=no`. Both now call the detector.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import report  # noqa: E402
from score import detect_regressions  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _irr_judges(**irr):
    return {"q": {"mean": 4.0, "scored_cases": 3,
                  "stability": {"samples": 3, "stable_cases": 1,
                                "total_cases": 3, "irr": irr}}}


def _ha_judges(**ha):
    return {"q": {"mean": 4.0, "scored_cases": 5, "human_agreement": ha}}


def _panel_judges(**panel):
    return {"q": {"mean": 4.0, "scored_cases": 4, "panel": panel}}


# judges aggregate, thresholds, run-level human_calibration block (None =
# never calibrated), and whether the CLI would regress
CASES = [
    ("min_mean", {"q": {"mean": 0.5, "scored_cases": 10}},
     {"q": {"min_mean": 1.0}}, None, True),
    ("min_pass_rate", {"q": {"pass_rate": 0.5}},
     {"q": {"min_pass_rate": 0.9}}, None, True),
    ("min_win_rate", {"pairwise": {"win_rate": 0.3}},
     {"pairwise": {"min_win_rate": 0.6}}, None, True),
    ("max_error_rate", {"q": {"scored_cases": 1, "errored_cases": 9}},
     {"q": {"max_error_rate": 0.2}}, None, True),
    ("clean", {"q": {"mean": 4.0, "scored_cases": 10, "errored_cases": 0}},
     {"q": {"min_mean": 1.0}}, None, False),
    # Reliability gates (measurement-validity PR2). Later commits add rows.
    ("min_alpha breach",
     _irr_judges(value=0.40, metric="krippendorff_alpha", level="ordinal",
                 n_units=3),
     {"q": {"min_alpha": 0.7}}, None, True),
    ("min_alpha degenerate pass",
     _irr_judges(value=None, reason_code="perfect_agreement",
                 reason="all ratings identical"),
     {"q": {"min_alpha": 0.7}}, None, False),
    ("min_alpha configured but unavailable",
     {"q": {"mean": 4.0, "scored_cases": 3}},
     {"q": {"min_alpha": 0.7}}, None, True),
    # Human-calibration gate (measurement-validity PR6). Three-state rule:
    # breach / perfect-agreement pass / never-calibrated silent skip — plus
    # the stale-calibration regression when the run-level block survives a
    # re-score that dropped the per-judge human_agreement.
    ("min_human_agreement never calibrated (silent skip)",
     {"q": {"mean": 4.0, "scored_cases": 5}},
     {"q": {"min_human_agreement": 0.6}}, None, False),
    ("min_human_agreement breach",
     _ha_judges(metric="cohen_kappa", level="nominal", value=0.2, n_units=6),
     {"q": {"min_human_agreement": 0.6}}, {"judges": ["q"]}, True),
    ("min_human_agreement clean",
     _ha_judges(metric="cohen_kappa", level="nominal", value=0.9, n_units=6),
     {"q": {"min_human_agreement": 0.6}}, {"judges": ["q"]}, False),
    ("min_human_agreement perfect-agreement pass",
     _ha_judges(metric="cohen_kappa", level="nominal", value=None,
                reason_code="perfect_agreement",
                reason="both raters used one identical category"),
     {"q": {"min_human_agreement": 0.6}}, {"judges": ["q"]}, False),
    ("min_human_agreement stale calibration",
     {"q": {"mean": 4.0, "scored_cases": 5}},
     {"q": {"min_human_agreement": 0.6}}, {"judges": ["q"]}, True),
    ("min_human_agreement below-floor (insufficient_data) regresses",
     _ha_judges(metric="cohen_kappa", level="nominal", value=None,
                reason_code="insufficient_data",
                reason="n=3 joined pairs, below the calibration floor (5)"),
     {"q": {"min_human_agreement": 0.6}}, {"judges": ["q"]}, True),
    # Judge-panel gate (measurement-validity PR8). Same three-state rule as
    # min_alpha, routed under the same include_irr scoping.
    ("min_panel_alpha breach",
     _panel_judges(metric="krippendorff_alpha", level="nominal", value=0.4,
                   n_units=4, models=["a", "b", "c"],
                   families={"unknown": 3}),
     {"q": {"min_panel_alpha": 0.67}}, None, True),
    ("min_panel_alpha clean",
     _panel_judges(metric="krippendorff_alpha", level="nominal", value=0.8,
                   n_units=4),
     {"q": {"min_panel_alpha": 0.67}}, None, False),
    ("min_panel_alpha degenerate pass",
     _panel_judges(metric="krippendorff_alpha", level="nominal", value=None,
                   reason_code="perfect_agreement",
                   reason="all ratings identical"),
     {"q": {"min_panel_alpha": 0.67}}, None, False),
    ("min_panel_alpha configured but unavailable",
     {"q": {"mean": 4.0, "scored_cases": 4}},
     {"q": {"min_panel_alpha": 0.67}}, None, True),
]


@pytest.mark.parametrize("key,judges,thresholds,hc,regresses", CASES)
def test_the_cli_gate_behaves_as_the_table_says(key, judges, thresholds, hc,
                                                regresses):
    assert bool(detect_regressions(judges, thresholds,
                                   human_calibration=hc)) is regresses


# --- reserved thresholds.simulator key (measurement-validity PR9) -------------
# judges aggregate, thresholds, summary['simulator'] block (None = never
# aggregated), and whether the CLI would regress. The judges dict always
# carries a REAL judge so a phantom judge-loop lookup for 'simulator'
# ("n/a pass_rate") would show up as an unexpected regression.

def _sim_summary(fallback_rate=0.0, human_n=2, human_agree=2,
                 cross_rate=None):
    human_rate = round(human_agree / human_n, 3) if human_n else None
    cross = None
    if cross_rate is not None:
        cross = {
            "models": ["claude-haiku-4-5", "gemini-2.5-flash"],
            "families": {"anthropic": 1, "google": 1},
            "single_family": False,
            "n_questions": 5,
            "all_agree_rate": cross_rate,
            "all_agree_label": "cross-simulator all-agree rate (uncorrected)",
            "per_model_agreement": {"gemini-2.5-flash": cross_rate},
            "alpha": {"metric": "krippendorff_alpha", "level": "nominal",
                      "value": None, "n_units": 5,
                      "reason_code": "insufficient_data",
                      "reason": "alpha suppressed: 5 < 10"},
            "disagreements": [],
        }
    out = {
        "status": "calibrated" if human_n else "uncalibrated simulator",
        "tiers": {"override": 4, "llm": 1, "fallback": 0, "disabled": 0},
        "n_questions": 5,
        "fallback_rate": fallback_rate,
        "calibration": {
            "n_pairs": human_n + 1,
            "by_source": {
                "human": {"n": human_n, "agree": human_agree,
                          "rate": human_rate, "label": "…(uncorrected)",
                          "pairs": []},
                "agent": {"n": 1, "agree": 0, "rate": 0.0,
                          "label": "LLM-vs-LLM consistency "
                                   "(not human calibration) — uncorrected"},
            },
            "gold_agreement": human_rate,
            "validated": bool(human_n),
        },
        "deadline_skips": 0,
        "ledger_scope": "case",
    }
    if cross is not None:
        out["cross_simulator"] = cross
    return out


_SIM_JUDGES = {"q": {"mean": 4.0, "pass_rate": 1.0, "scored_cases": 5}}

SIM_CASES = [
    ("fallback-rate breach",
     _sim_summary(fallback_rate=0.4),
     {"simulator": {"max_fallback_rate": 0.0}}, True),
    ("gold-agreement breach (human stratum)",
     _sim_summary(human_n=4, human_agree=1),
     {"simulator": {"min_gold_agreement": 0.8}}, True),
    ("clean block — agent stratum worse than the gate is NOT gated",
     _sim_summary(),
     {"simulator": {"max_fallback_rate": 0.1,
                    "min_gold_agreement": 0.8}}, False),
    ("zero human-provenance pairs fail the gold gate loudly",
     _sim_summary(human_n=0, human_agree=0),
     {"simulator": {"min_gold_agreement": 0.5}}, True),
    ("configured but no simulator block in the summary",
     None,
     {"simulator": {"max_fallback_rate": 0.0}}, True),
    # Cross-simulator gate (measurement-validity PR10). Three-state rule:
    # breach / clean / configured-but-unavailable (no cross_simulator
    # block — models.hook_shadow never answered) = regression.
    ("cross-simulator agreement breach",
     _sim_summary(cross_rate=0.5),
     {"simulator": {"min_cross_simulator_agreement": 0.8}}, True),
    ("cross-simulator agreement clean",
     _sim_summary(cross_rate=0.9),
     {"simulator": {"min_cross_simulator_agreement": 0.8}}, False),
    ("cross-simulator configured but no shadow answers recorded",
     _sim_summary(),
     {"simulator": {"min_cross_simulator_agreement": 0.8}}, True),
    ("cross-simulator configured but no simulator block at all",
     None,
     {"simulator": {"min_cross_simulator_agreement": 0.8}}, True),
]


@pytest.mark.parametrize("key,sim,thresholds,regresses", SIM_CASES)
def test_the_cli_simulator_gate_behaves_as_the_table_says(key, sim,
                                                          thresholds,
                                                          regresses):
    assert bool(detect_regressions(_SIM_JUDGES, thresholds,
                                   simulator=sim)) is regresses


@pytest.mark.parametrize("key,sim,thresholds,regresses", SIM_CASES)
def test_the_report_shows_every_simulator_breach_the_cli_exits_on(
        key, sim, thresholds, regresses):
    summary = {"judges": _SIM_JUDGES}
    if sim is not None:
        summary["simulator"] = sim
    html = report._render_regressions(summary, {"thresholds": thresholds})
    assert bool(html) is regresses, f"{key}: report and CLI disagree"
    if regresses:
        assert "Regressions" in html
        assert "simulator" in html


@pytest.mark.parametrize("key,sim,thresholds,regresses", SIM_CASES)
def test_the_mlflow_tag_matches_the_cli_for_simulator_gates(key, sim,
                                                            thresholds,
                                                            regresses):
    log_results = _load(
        REPO_ROOT / "skills" / "eval-mlflow" / "scripts" / "log_results.py",
        "_log_results_sim_test")
    assert bool(log_results._detect_regressions(
        _SIM_JUDGES, thresholds, simulator=sim)) is regresses


def test_harbor_paths_skip_the_simulator_gates_like_the_cli():
    """include_irr=False (the Harbor/EvalHub call shape — those paths also
    strip the WHOLE reserved key with a notice, so the active
    min_cross_simulator_agreement is covered for free) evaluates no
    simulator gate, and the harbor-mode report agrees."""
    thresholds = {"simulator": {"max_fallback_rate": 0.0,
                                "min_gold_agreement": 0.9,
                                "min_cross_simulator_agreement": 0.9}}
    assert detect_regressions(_SIM_JUDGES, thresholds,
                              include_irr=False, simulator=None) == []
    assert not report._render_regressions(
        {"judges": _SIM_JUDGES}, {"thresholds": thresholds},
        run_result={"execution_mode": "harbor"})


@pytest.mark.parametrize("key,judges,thresholds,hc,regresses", CASES)
def test_the_report_shows_every_breach_the_cli_exits_on(key, judges, thresholds,
                                                        hc, regresses):
    summary = {"judges": judges}
    if hc is not None:
        summary["human_calibration"] = hc
    html = report._render_regressions(summary, {"thresholds": thresholds})
    assert bool(html) is regresses, f"{key}: report and CLI disagree"
    if regresses:
        assert "Regressions" in html


@pytest.mark.parametrize("key,judges,thresholds,hc,regresses", CASES)
def test_the_mlflow_tag_matches_the_cli(key, judges, thresholds, hc, regresses):
    log_results = _load(
        REPO_ROOT / "skills" / "eval-mlflow" / "scripts" / "log_results.py",
        "_log_results_under_test")
    assert bool(log_results._detect_regressions(
        judges, thresholds, human_calibration=hc)) is regresses


def test_a_judge_gated_only_on_coverage_is_not_reported_as_passing():
    """The summary table picks one bound to display; the PASS/FAIL verdict has
    to come from the detector, or a judge whose only gate is `max_error_rate`
    renders green while the run exits 1."""
    summary = {"judges": {"q": {"mean": 4.0, "scored_cases": 1,
                                "errored_cases": 9}}, "per_case": {}}
    html = report._render_scoring_summary(
        summary, {"thresholds": {"q": {"max_error_rate": 0.2}}, "judges": []})
    assert "FAIL" in html and ">PASS<" not in html


# --- a metric the table cannot display is still an authoritative breach ------

def _statuses(judges, thresholds):
    import re
    html = report._render_scoring_summary({"judges": judges, "per_case": {}},
                                          {"thresholds": thresholds, "judges": []})
    return re.findall(r">(PASS|FAIL|SKIP|ERROR)<", html)


@pytest.mark.parametrize("label,judges,thresholds,expected", [
    # win_rate and error rate are not columns in this table, so the old
    # "no mean and no pass_rate" branch swallowed them into SKIP.
    ("win_rate breach", {"pairwise": {"win_rate": 0.3}},
     {"pairwise": {"min_win_rate": 0.6}}, "FAIL"),
    ("win_rate satisfied", {"pairwise": {"win_rate": 0.8}},
     {"pairwise": {"min_win_rate": 0.6}}, "PASS"),
    ("coverage breach, every case errored",
     {"q": {"mean": None, "pass_rate": None, "scored_cases": 0,
            "errored_cases": 10}}, {"q": {"max_error_rate": 0.2}}, "FAIL"),
    ("min_mean with no surviving case",
     {"q": {"mean": None, "pass_rate": None, "scored_cases": 0,
            "errored_cases": 10}}, {"q": {"min_mean": 1.0}}, "FAIL"),
    ("clean numeric", {"q": {"mean": 4.0, "scored_cases": 10}},
     {"q": {"min_mean": 1.0}}, "PASS"),
])
def test_the_summary_status_matches_the_detector(label, judges, thresholds,
                                                 expected):
    assert _statuses(judges, thresholds) == [expected], label


# --- execution-path scoping: harbor/evalhub skip min_alpha -------------------

def test_harbor_report_shows_no_fail_for_a_min_alpha_only_judge():
    """The same breach that FAILs a local report is skipped (with the CLI)
    when the run executed on harbor/evalhub — those aggregations carry no
    sampling stability data."""
    judges = _irr_judges(value=0.40, metric="krippendorff_alpha",
                         level="ordinal", n_units=3)
    summary = {"judges": judges, "per_case": {}}
    config = {"thresholds": {"q": {"min_alpha": 0.7}}, "judges": []}

    local = report._render_scoring_summary(summary, config)
    assert "FAIL" in local

    harbor = report._render_scoring_summary(
        summary, config, run_result={"execution_mode": "harbor"})
    assert "FAIL" not in harbor
    assert not report._render_regressions(
        summary, config, run_result={"execution_mode": "harbor"})


def test_harbor_report_shows_no_fail_for_a_min_panel_alpha_only_judge():
    """min_panel_alpha rides the SAME include_irr scoping as min_alpha —
    harbor/evalhub aggregations carry no judge-panel data."""
    judges = _panel_judges(metric="krippendorff_alpha", level="nominal",
                           value=0.4, n_units=4)
    summary = {"judges": judges, "per_case": {}}
    config = {"thresholds": {"q": {"min_panel_alpha": 0.67}}, "judges": []}

    local = report._render_scoring_summary(summary, config)
    assert "FAIL" in local

    harbor = report._render_scoring_summary(
        summary, config, run_result={"execution_mode": "harbor"})
    assert "FAIL" not in harbor
    assert not report._render_regressions(
        summary, config, run_result={"execution_mode": "harbor"})
    assert detect_regressions(judges, config["thresholds"],
                              include_irr=False) == []


def test_harbor_min_human_agreement_never_calibrated_is_silently_skipped():
    """include_irr=False does NOT govern min_human_agreement (calibration is
    post-hoc, not sampling-derived) — but the Harbor/EvalHub paths pass raw
    aggregates without human_agreement and no human_calibration kwarg, so
    the never-calibrated silent skip applies naturally on that path."""
    judges = {"q": {"mean": 4.0, "scored_cases": 5}}
    thresholds = {"q": {"min_human_agreement": 0.6}}

    # The exact Harbor/EvalHub call shape: include_irr=False, no
    # human_calibration kwarg, aggregate without human_agreement.
    assert detect_regressions(judges, thresholds, include_irr=False) == []

    # And the harbor-mode report agrees.
    summary = {"judges": judges, "per_case": {}}
    config = {"thresholds": thresholds, "judges": []}
    assert not report._render_regressions(
        summary, config, run_result={"execution_mode": "harbor"})
    assert "FAIL" not in report._render_scoring_summary(
        summary, config, run_result={"execution_mode": "harbor"})


def test_consequence_tier_bound_renders_without_a_thresholds_block():
    """Detection-time tier resolution is visible to the report: a
    consequence-tagged judge shows its 0.70 bound in the Threshold column
    via _effective_thresholds, with no thresholds block in the config."""
    judges = _irr_judges(value=0.85, metric="krippendorff_alpha",
                         level="ordinal", n_units=3)
    html = report._render_scoring_summary(
        {"judges": judges, "per_case": {}},
        {"judges": [{"name": "q", "consequence": "safety",
                     "llm_rubric": "score it"}]})
    assert "&alpha; 0.7" in html


# --- a detector that cannot run is not a clean run ---------------------------

def _break_detector(monkeypatch, module):
    def boom(*_a, **_k):
        raise RuntimeError("engine unavailable")
    monkeypatch.setattr(module, "_detect_regressions", boom)


def test_the_regressions_section_says_so_when_it_cannot_evaluate(monkeypatch):
    _break_detector(monkeypatch, report)
    html = report._render_regressions({"judges": {"q": {"mean": 0.5}}},
                                      {"thresholds": {"q": {"min_mean": 1.0}}})
    assert "Could not evaluate thresholds" in html
    assert "engine unavailable" in html


def test_the_summary_does_not_claim_pass_when_it_cannot_evaluate(monkeypatch):
    """An empty breach set has to mean "checked, none" — not "never checked"."""
    _break_detector(monkeypatch, report)
    assert _statuses({"q": {"mean": 4.0, "scored_cases": 10}},
                     {"q": {"min_mean": 1.0}}) != ["PASS"]


def test_the_mlflow_helper_raises_rather_than_reporting_clean(monkeypatch):
    """It used to swallow every failure and return [], which the caller then
    tagged `regressions_detected=no` — indistinguishable from a clean run."""
    log_results = _load(
        REPO_ROOT / "skills" / "eval-mlflow" / "scripts" / "log_results.py",
        "_log_results_tag_test")

    # A working call still returns a list.
    assert log_results._detect_regressions(
        {"q": {"mean": 0.5, "scored_cases": 10}}, {"q": {"min_mean": 1.0}})

    # An unreachable engine raises instead of looking clean.
    monkeypatch.setattr(log_results, "Path", _MissingEnginePath)
    with pytest.raises(FileNotFoundError):
        log_results._detect_regressions({"q": {"mean": 1.0}},
                                        {"q": {"min_mean": 1.0}})


class _MissingEnginePath(type(Path())):
    """A Path whose resolved root has no score.py."""

    def resolve(self):
        return Path("/nonexistent-harness-root/skills/x/scripts/log_results.py")
