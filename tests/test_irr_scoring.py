"""Chance-corrected IRR over the cross-case sampling matrix (score.py).

Covers the scoring-side adapter around agent_eval.reliability: measurement-
level selection from the judge config, error-samples-as-missing, per-case
Fleiss completeness, the nested stability.irr block surviving the summary
merge, bootstrap CI presence — plus the program-wide labeling-hygiene and
scoring-path purity guards (this file is their one home).
"""

import ast
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
    INTERVAL, NOMINAL, ORDINAL, REASON_INSUFFICIENT_DATA,
    REASON_PERFECT_AGREEMENT, krippendorff_alpha,
)
from score import (  # noqa: E402
    IRR_SELF_CONSISTENCY_LABEL, _compute_stability_irr, _irr_level,
    _merge_summary, _strip_judge_values, score_cases,
)

UPPER_BOUND_LABEL = ("single-judge self-consistency alpha "
                     "(upper bound on inter-rater reliability)")


def test_the_upper_bound_label_is_verbatim():
    """The label is a grep-enforced invariant across the whole program."""
    assert IRR_SELF_CONSISTENCY_LABEL == UPPER_BOUND_LABEL


def _scored(rows, samples):
    """Build per-case scored entries: rows = list of (values, error_count)."""
    return [{"value": (values[0] if values else None),
             "stability": {"samples": samples, "values": list(values),
                           "error_count": errors}}
            for values, errors in rows]


# ---------------------------------------------------------------------------
# Level selection
# ---------------------------------------------------------------------------

class TestIrrLevel:
    def test_bool_feedback_is_nominal(self):
        assert _irr_level(JudgeConfig(name="q", feedback_type="bool")) == NOMINAL

    def test_integer_score_range_is_ordinal(self):
        assert _irr_level(
            JudgeConfig(name="q", score_range=[1.0, 5.0])) == ORDINAL

    def test_float_feedback_is_interval(self):
        assert _irr_level(JudgeConfig(
            name="q", feedback_type="float", score_range=[0.0, 1.0])) == INTERVAL

    def test_fractional_bounds_are_interval(self):
        assert _irr_level(
            JudgeConfig(name="q", score_range=[0.0, 2.5])) == INTERVAL

    def test_no_config_defaults_to_interval(self):
        assert _irr_level(None) == INTERVAL


# ---------------------------------------------------------------------------
# The coefficient block
# ---------------------------------------------------------------------------

class TestComputeStabilityIrr:
    def test_perfect_agreement_is_a_reason_code_not_a_one(self):
        scored = _scored([([4, 4, 4], 0), ([4, 4, 4], 0), ([4, 4, 4], 0)], 3)
        irr = _compute_stability_irr(
            scored, JudgeConfig(name="q", score_range=[1.0, 5.0]), 3, {3})
        assert irr["value"] is None
        assert irr["reason_code"] == REASON_PERFECT_AGREEMENT
        assert irr["label"] == UPPER_BOUND_LABEL

    def test_single_pairable_unit_is_insufficient_data(self):
        scored = _scored([([3, 4], 0)], 2)
        irr = _compute_stability_irr(
            scored, JudgeConfig(name="q", score_range=[1.0, 5.0]), 2, {2})
        assert irr["value"] is None
        assert irr["reason_code"] == REASON_INSUFFICIENT_DATA

    def test_disagreeing_nominal_matrix_pins_the_primitive(self):
        rows = [([True, True, False], 0), ([True, True, True], 0),
                ([False, False, False], 0), ([True, False, False], 0)]
        scored = _scored(rows, 3)
        irr = _compute_stability_irr(
            scored, JudgeConfig(name="q", feedback_type="bool"), 3, {3})
        expected = krippendorff_alpha(
            [list(v) for v, _ in rows], NOMINAL).value
        assert irr["metric"] == "krippendorff_alpha"
        assert irr["level"] == NOMINAL
        assert irr["value"] == pytest.approx(expected)
        assert -1.0 <= irr["value"] <= 1.0
        assert irr["n_units"] == 4

    def test_error_samples_are_missing_ratings_never_a_category(self):
        # case 2 has one errored sample: its unit is [3, 4, None], so the
        # observed rating count drops and the matrix is incomplete.
        scored = _scored([([3, 4, 3], 0), ([3, 4], 1), ([2, 3, 4], 0)], 3)
        irr = _compute_stability_irr(
            scored, JudgeConfig(name="q", score_range=[1.0, 5.0]), 3, {3})
        assert irr["n_ratings"] == 8  # 3 + 2 + 3 observed, None dropped
        assert "fleiss_kappa" not in irr  # incomplete matrix, no kappa

    def test_fleiss_only_on_a_per_case_complete_matrix(self):
        complete = _scored(
            [([True, True], 0), ([True, False], 0), ([False, False], 0)], 2)
        irr = _compute_stability_irr(
            complete, JudgeConfig(name="q", feedback_type="bool"), 2, {2})
        assert "fleiss_kappa" in irr

        # Same data with ONE short case (completeness is judged per case,
        # never from the first case only) -> no kappa.
        one_short = _scored(
            [([True, True], 0), ([True], 1), ([False, False], 0)], 2)
        irr2 = _compute_stability_irr(
            one_short, JudgeConfig(name="q", feedback_type="bool"), 2, {2})
        assert "fleiss_kappa" not in irr2

    def test_no_fleiss_companion_on_interval_scales(self):
        scored = _scored([([0.1, 0.2], 0), ([0.9, 0.8], 0), ([0.5, 0.4], 0)], 2)
        irr = _compute_stability_irr(
            scored, JudgeConfig(name="q", feedback_type="float",
                                score_range=[0.0, 1.0]), 2, {2})
        assert irr["level"] == INTERVAL
        assert "fleiss_kappa" not in irr

    def test_non_uniform_samples_across_cases(self):
        # 3 and 5 samples: n_raters is the max, matrix incomplete, no crash.
        scored = [
            {"value": 3, "stability": {"samples": 3, "values": [3, 4, 3],
                                       "error_count": 0}},
            {"value": 4, "stability": {"samples": 5, "values": [4, 4, 5, 3, 4],
                                       "error_count": 0}},
        ]
        irr = _compute_stability_irr(
            scored, JudgeConfig(name="q", score_range=[1.0, 5.0]), 5, {3, 5})
        assert irr["value"] is not None
        assert "fleiss_kappa" not in irr

    def test_bootstrap_ci_present_when_alpha_is_defined(self):
        scored = _scored([([5, 5], 0), ([4, 4], 0), ([3, 3], 0), ([5, 4], 0),
                          ([2, 3], 0), ([4, 5], 0)], 2)
        irr = _compute_stability_irr(
            scored, JudgeConfig(name="q", score_range=[1.0, 5.0]), 2, {2})
        assert irr["value"] is not None
        assert isinstance(irr.get("ci"), list) and len(irr["ci"]) == 2
        lo, hi = irr["ci"]
        assert lo <= hi

    def test_no_ci_on_degenerate_results(self):
        scored = _scored([([4, 4], 0), ([4, 4], 0)], 2)
        irr = _compute_stability_irr(
            scored, JudgeConfig(name="q", score_range=[1.0, 5.0]), 2, {2})
        assert irr["value"] is None
        assert "ci" not in irr

    def test_rationale_and_label_are_clean(self):
        scored = _scored([([3, 4, 3], 0), ([2, 2, 2], 0), ([5, 4, 5], 0)], 3)
        irr = _compute_stability_irr(
            scored, JudgeConfig(name="q", score_range=[1.0, 5.0]), 3, {3})
        assert irr["label"] == UPPER_BOUND_LABEL
        assert irr["rationale"]  # report-ready selection rationale
        blob = f"{irr['label']} {irr['rationale']}"
        assert "Sec 6.4" not in blob
        for adjective in ("almost perfect", "substantial agreement",
                          "moderate agreement"):
            assert adjective not in blob.lower()


# ---------------------------------------------------------------------------
# Integration: score_cases -> aggregated stability.irr -> summary merge
# ---------------------------------------------------------------------------

def _make_config(judge):
    config = EvalConfig()
    config.judges.append(judge)
    return config


def _make_case_dirs(tmp_path, n):
    dirs = []
    for i in range(n):
        d = tmp_path / "cases" / f"case-{i + 1:03d}"
        d.mkdir(parents=True)
        dirs.append(d)
    return dirs


def _cycling_scorer(ratings):
    """Deterministic per-case sample sequence keyed on the case dir name."""
    calls = defaultdict(int)
    lock = threading.Lock()

    def scorer(outputs=None, **kwargs):
        cid = Path((outputs or {})["case_dir"]).name
        with lock:
            i = calls[cid]
            calls[cid] += 1
        value = ratings[cid][i]
        if isinstance(value, Exception):
            raise value
        return value, f"sample {i}"

    return scorer


def test_score_cases_builds_the_nested_irr_block(tmp_path):
    config = _make_config(JudgeConfig(name="q", llm_rubric="score it",
                                      score_range=[1.0, 5.0], samples=3))
    case_dirs = _make_case_dirs(tmp_path, 3)
    ratings = {"case-001": [3, 4, 3], "case-002": [2, 2, 2],
               "case-003": [5, 4, 5]}
    judges = [("q", _cycling_scorer(ratings), "", "llm", 3)]

    results = score_cases(judges, case_dirs, config)
    stability = results["aggregated"]["q"]["stability"]
    irr = stability["irr"]
    assert irr["metric"] == "krippendorff_alpha"
    assert irr["level"] == ORDINAL  # integer score_range
    assert irr["n_units"] == 3
    assert irr["label"] == UPPER_BOUND_LABEL
    assert isinstance(irr["value"], float)


def test_errored_samples_shrink_the_matrix_not_the_categories(tmp_path):
    config = _make_config(JudgeConfig(name="q", llm_rubric="score it",
                                      score_range=[1.0, 5.0], samples=3))
    case_dirs = _make_case_dirs(tmp_path, 2)
    ratings = {"case-001": [3, RuntimeError("api down"), 4],
               "case-002": [2, 3, 2]}
    judges = [("q", _cycling_scorer(ratings), "", "llm", 3)]

    results = score_cases(judges, case_dirs, config)
    irr = results["aggregated"]["q"]["stability"]["irr"]
    assert irr["n_ratings"] == 5  # 2 + 3 observed; the error is missing
    assert "fleiss_kappa" not in irr  # one case incomplete -> no kappa


def test_nested_irr_survives_the_real_summary_merge(tmp_path):
    """Assert through _strip_judge_values + _merge_summary output — the real
    persistence path — not a re-simulated comprehension."""
    config = _make_config(JudgeConfig(name="q", llm_rubric="score it",
                                      score_range=[1.0, 5.0], samples=3))
    case_dirs = _make_case_dirs(tmp_path, 3)
    ratings = {"case-001": [3, 4, 3], "case-002": [2, 2, 2],
               "case-003": [5, 4, 5]}
    judges = [("q", _cycling_scorer(ratings), "", "llm", 3)]
    results = score_cases(judges, case_dirs, config)

    runs_dir = tmp_path / "runs"
    (runs_dir / "r1").mkdir(parents=True)
    _merge_summary("r1", "judges",
                   _strip_judge_values(results["aggregated"]), runs_dir)

    summary = yaml.safe_load((runs_dir / "r1" / "summary.yaml").read_text())
    merged = summary["judges"]["q"]
    assert "values" not in merged  # the strip did its one job
    irr = merged["stability"]["irr"]
    assert irr["metric"] == "krippendorff_alpha"
    assert irr["label"] == UPPER_BOUND_LABEL
    assert isinstance(irr["value"], float)
    assert irr["n_units"] == 3


# ---------------------------------------------------------------------------
# Program-wide guards (one home: this file)
# ---------------------------------------------------------------------------

_USER_FACING_SOURCES = [
    REPO_ROOT / "agent_eval" / "reliability.py",
    REPO_ROOT / "skills" / "eval-run" / "scripts" / "score.py",
    REPO_ROOT / "skills" / "eval-run" / "scripts" / "report.py",
]


def test_labeling_hygiene_no_landis_koch_no_wrong_citation():
    """Grep-enforced labeling invariants for every user-facing string the
    measurement-validity program introduces: no strength-of-agreement
    adjectives, no 'Sec 6.4' (the ordinal material is Section 5.3), and the
    uncorrected percent-agreement surfaces say so."""
    for path in _USER_FACING_SOURCES:
        text = path.read_text()
        lowered = text.lower()
        for adjective in ("almost perfect", "substantial agreement",
                          "moderate agreement"):
            assert adjective not in lowered, f"{path.name}: {adjective!r}"
        assert "sec 6.4" not in lowered, f"{path.name}: wrong paper citation"
    # The swap-consistency rate is uncorrected agreement and must say so
    # wherever it is printed/rendered.
    score_src = (REPO_ROOT / "skills" / "eval-run" / "scripts"
                 / "score.py").read_text()
    report_src = (REPO_ROOT / "skills" / "eval-run" / "scripts"
                  / "report.py").read_text()
    assert "uncorrected agreement" in score_src
    assert "uncorrected agreement" in report_src


_FORBIDDEN_SCORING_IMPORTS = {
    "scipy", "pandas", "pingouin", "statsmodels", "numpy"}


@pytest.mark.parametrize("path", [
    REPO_ROOT / "agent_eval" / "reliability.py",
    REPO_ROOT / "skills" / "eval-run" / "scripts" / "score.py",
], ids=["reliability", "score"])
def test_scoring_path_purity_static_scan(path):
    """The scoring path stays free of heavyweight stats stacks — ensure_deps
    installs only pyyaml/mlflow/anthropic/jinja2."""
    tree = ast.parse(path.read_text(), filename=str(path))
    hit = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hit.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            hit.add(node.module.split(".")[0])
    assert not (hit & _FORBIDDEN_SCORING_IMPORTS), (
        f"{path}: forbidden scoring-path imports "
        f"{sorted(hit & _FORBIDDEN_SCORING_IMPORTS)}")
