"""Tests for per-case score banding and histograms (issue #182).

A value off the judge's declared scale is invalid, not excellent: it must not
render as a green pass, and it must not vanish from the distribution glyph.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"))

from report import (_ascii_score_hist, _judge_score_ranges,
                    _render_reward_overview, _score_band_class)


def test_bands_span_the_declared_scale():
    assert _score_band_class(2, 0, 2) == "pass"
    assert _score_band_class(1, 0, 2) == "warn"
    assert _score_band_class(0, 0, 2) == "fail"


def test_value_above_the_scale_is_not_a_pass():
    # frac would be 2.0 -> "pass" without the guard.
    assert _score_band_class(4, 0, 2) == "fail"


def test_value_below_the_scale_is_not_a_pass():
    assert _score_band_class(-1, 0, 2) == "fail"


def test_histogram_keeps_an_off_scale_value_visible():
    glyph = _ascii_score_hist(1, [0, 1, 4], smin=0, smax=2)
    # Bins are lo..hi inclusive; the 4 widens the axis instead of being dropped.
    assert glyph.endswith(" 4")


def test_histogram_unchanged_when_every_value_is_on_scale():
    glyph = _ascii_score_hist(1, [0, 1, 2], smin=0, smax=2)
    assert glyph.startswith("0 ") and glyph.endswith(" 2")


def test_a_fractional_upper_bound_keeps_its_fraction():
    """`int()` here read a [0, 2.5] scale as [0, 2] and banded a legitimate
    2.4 as off-scale."""
    ranges = _judge_score_ranges({"judges": [{"name": "j", "score_range": [0, 2.5]}]})
    assert ranges["j"] == (0.0, 2.5)
    assert _score_band_class(2.4, *ranges["j"]) != "fail"


def test_a_range_that_truncates_to_a_point_survives():
    """int() collapsed [1.2, 1.8] to (1, 1), which `lo < hi` then dropped —
    the judge silently lost its scale instead of banding on it."""
    ranges = _judge_score_ranges({"judges": [{"name": "j", "score_range": [1.2, 1.8]}]})
    assert ranges["j"] == (1.2, 1.8)


def test_malformed_ranges_are_still_dropped():
    config = {"judges": [{"name": "rev", "score_range": [5, 1]},
                         {"name": "txt", "score_range": ["a", "b"]},
                         {"name": "one", "score_range": [1]},
                         {"name": "nil"}]}
    assert _judge_score_ranges(config) == {}


def test_histogram_bins_widen_to_cover_a_fractional_bound():
    """Bins are whole numbers; ceil keeps the top of a [0, 2.5] scale visible
    where truncating to 2 would clip it."""
    glyph = _ascii_score_hist(1, [0, 1, 2], smin=0, smax=2.5)
    assert glyph.startswith("0 ") and glyph.endswith(" 3")


def test_reward_overview_normalizes_over_each_judge_declared_range():
    """The report's Reward column must agree with anova.json and reward.json.

    `compose_reward` has three production call sites; wiring `judge_ranges`
    into only two left the report normalizing a 0-2 judge against the 1-5
    default, so a perfect 2/2 rendered 0.2500 while `anova.json` said 1.0000
    for the same summary.yaml.
    """
    import re

    config = {"judges": [{"name": "q", "score_range": [0, 2]},
                         {"name": "r", "score_range": [0, 2]}]}
    summary = {"per_case": {"case-1": {"q": {"value": 2}, "r": {"value": 2}}}}
    html = _render_reward_overview(summary, config)
    assert re.search(r"1\.0000", html), html


def test_a_fractional_off_scale_value_is_not_truncated_into_range():
    """int() ran before the widening, so 2.9 on a [0, 2] judge became a 2 and
    rendered as a top-of-scale reading instead of an off-scale one."""
    glyph = _ascii_score_hist(1, [1, 2, 2.9], smin=0, smax=2)
    assert glyph.endswith(" 3")


def test_a_wild_reading_cannot_explode_the_axis():
    """Widening ran before the bin cap, and the cap's fallback was the same
    span it had just widened to — so the guard could not shrink anything."""
    glyph = _ascii_score_hist(2, [2, 2, 100000], smin=0, smax=2)
    assert len(glyph) < 120
