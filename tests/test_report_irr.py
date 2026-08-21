"""Report rendering of the reliability surfaces (measurement-validity PR2).

The IRR badge beside the stability bar, the min_alpha threshold column, the
degenerate "n/a" render (never 1.0), the no-Landis-Koch contract on rendered
HTML, and the pairwise swap-consistency line.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import report  # noqa: E402

UPPER_BOUND_LABEL = ("single-judge self-consistency alpha "
                     "(upper bound on inter-rater reliability)")


def _summary(irr):
    return {"judges": {"q": {"mean": 4.1, "scored_cases": 12,
                             "stability": {"samples": 3, "stable_cases": 9,
                                           "total_cases": 12, "irr": irr}}},
            "per_case": {}}


def _computed_irr(**overrides):
    irr = {"metric": "krippendorff_alpha", "level": "ordinal", "value": 0.712,
           "reason_code": None, "reason": None, "n_units": 12,
           "label": UPPER_BOUND_LABEL,
           "rationale": ("Krippendorff's alpha selected: the ordinal scale "
                         "needs a distance-weighted disagreement function "
                         "(paper Sec 5.3)."),
           "n_ratings": 36}
    irr.update(overrides)
    return irr


def test_badge_renders_value_metric_n_units_and_tooltip():
    html = report._render_scoring_summary(
        _summary(_computed_irr()),
        {"thresholds": {"q": {"min_alpha": 0.7}}, "judges": []})
    assert "self-consistency α = 0.712" in html
    assert "krippendorff_alpha" in html
    assert "n=12" in html
    # Tooltip: verbatim upper-bound label + rationale + tier provenance note.
    assert UPPER_BOUND_LABEL in html
    assert "n_units=12" in html
    assert "author-proposed" in html
    assert "only 0.67 is literature-backed" in html


def test_badge_renders_the_ci_when_present():
    html = report._render_scoring_summary(
        _summary(_computed_irr(ci=[0.55, 0.84])),
        {"thresholds": {}, "judges": []})
    assert "CI [0.550, 0.840]" in html


def test_min_alpha_appears_in_the_threshold_column():
    html = report._render_scoring_summary(
        _summary(_computed_irr(value=0.85)),
        {"thresholds": {"q": {"min_alpha": 0.7}}, "judges": []})
    assert "&ge; &alpha; 0.7" in html
    assert ">PASS<" in html


def test_degenerate_renders_n_a_never_one_point_zero():
    irr = _computed_irr(value=None, reason_code="perfect_agreement",
                        reason="all 36 included ratings are identical")
    html = report._render_scoring_summary(
        _summary(irr), {"thresholds": {"q": {"min_alpha": 0.7}}, "judges": []})
    assert "α n/a (perfect agreement)" in html
    assert "α = 1.0" not in html
    # The degenerate PASSES the gate: Status must not be FAIL.
    assert "FAIL" not in html


def test_unavailable_reason_code_renders_warn_styled():
    irr = _computed_irr(value=None, reason_code="insufficient_data",
                        reason="only 1 pairable unit")
    html = report._render_scoring_summary(
        _summary(irr), {"thresholds": {}, "judges": []})
    assert "α n/a (insufficient_data)" in html
    assert "irr-warn" in html


def test_rendered_section_carries_no_landis_koch_adjectives():
    html = report._render_scoring_summary(
        _summary(_computed_irr()),
        {"thresholds": {"q": {"min_alpha": 0.7}}, "judges": []})
    lowered = html.lower()
    for adjective in ("almost perfect", "substantial", "moderate agreement",
                      "slight", "fair agreement"):
        assert adjective not in lowered, adjective


def test_pairwise_swap_consistency_line():
    html = report._render_pairwise({"pairwise": {
        "run_a": "a", "run_b": "b", "cases_compared": 10,
        "wins_a": 5, "wins_b": 3, "ties": 1, "errors": 1,
        "swap_consistency": {"consistent": 8, "inconsistent": 1,
                             "errors": 1, "rate": 0.889},
        "per_case": [],
    }})
    assert "Swap consistency: 8/9 (89%)" in html
    assert "uncorrected agreement" in html
    assert "errored comparison(s) excluded" in html


def test_pairwise_without_swap_consistency_stays_quiet():
    html = report._render_pairwise({"pairwise": {
        "run_a": "a", "run_b": "b", "cases_compared": 2,
        "wins_a": 1, "wins_b": 1, "ties": 0, "errors": 0,
        "per_case": [],
    }})
    assert "Swap consistency" not in html


def test_two_positional_arg_back_compat():
    """Existing callers pass (summary, config) only — run_result defaults."""
    html = report._render_scoring_summary(
        {"judges": {"q": {"mean": 4.0, "scored_cases": 5}}, "per_case": {}},
        {"thresholds": {"q": {"min_mean": 3.0}}, "judges": []})
    assert ">PASS<" in html
    assert report._render_regressions(
        {"judges": {"q": {"mean": 4.0, "scored_cases": 5}}},
        {"thresholds": {"q": {"min_mean": 3.0}}}) == ""


def test_badge_absent_when_no_irr_block():
    html = report._render_scoring_summary(
        {"judges": {"q": {"mean": 4.0, "scored_cases": 5,
                          "stability": {"samples": 3, "stable_cases": 5,
                                        "total_cases": 5}}},
         "per_case": {}},
        {"thresholds": {}, "judges": []})
    assert "irr-badge" not in html
    assert report._irr_badge(None) == ""
    assert report._irr_badge({}) == ""
