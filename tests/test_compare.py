"""Tests for the eval-compare skill (skills/eval-compare/scripts/compare.py).

Covers the parts with real logic: run discovery + unique slugging, tolerant
file loading, model resolution, aggregation, the authoritative pass-rate check,
best/worst + rank coloring, number formatting, and an end-to-end
generate_report smoke test that guards the report.html collision fix.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "eval-compare" / "scripts"))

import compare  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_run(base, rel, *, model="claude-opus-4-8", judges=None, per_case=None,
              run_metrics=None, run_id=None, with_html=True, with_result=True,
              cost=1.5, html_marker=None):
    """Create a run directory (summary.yaml [+ run_result.json + report.html])."""
    d = base / rel
    d.mkdir(parents=True, exist_ok=True)
    summary = {"run_id": run_id or rel.replace("/", "-"), "judges": judges or {}}
    if per_case is not None:
        summary["per_case"] = per_case
    if run_metrics is not None:
        summary["run_metrics"] = run_metrics
    (d / "summary.yaml").write_text(yaml.dump(summary))
    if with_result:
        (d / "run_result.json").write_text(json.dumps({
            "model": model, "cost_usd": cost, "num_turns": 10,
            "wall_clock_s": 120, "token_usage": {"output": 5000},
        }))
    if with_html:
        (d / "report.html").write_text(html_marker or f"<html>{rel}</html>")
    return d


# ---------------------------------------------------------------------------
# discover_runs + slugging
# ---------------------------------------------------------------------------

def test_discover_runs_basic_fields(tmp_path):
    _make_run(tmp_path, "run-a", model="claude-opus-4-8",
              judges={"quality": {"mean": 4.2}})
    runs = compare.discover_runs(tmp_path)
    assert len(runs) == 1
    r = runs[0]
    assert r["name"] == "run-a"
    assert r["run_result"]["model"] == "claude-opus-4-8"
    assert r["html_report"].endswith("report.html")
    assert r["slug"]  # populated


def test_discover_runs_missing_dir_raises():
    with pytest.raises(NotADirectoryError):
        compare.discover_runs("/no/such/dir/really")


def test_discover_runs_unique_slug_for_shared_basename(tmp_path):
    # Two runs at different paths but identical directory basename.
    _make_run(tmp_path, "evalA/2026-07-30-opus")
    _make_run(tmp_path, "evalB/2026-07-30-opus")
    runs = compare.discover_runs(tmp_path)
    assert len(runs) == 2
    assert runs[0]["name"] == runs[1]["name"] == "2026-07-30-opus"
    slugs = {r["slug"] for r in runs}
    assert len(slugs) == 2, "slugs must be unique across shared basenames"


def test_discover_runs_missing_run_result_is_graceful(tmp_path):
    _make_run(tmp_path, "run-x", with_result=False)
    runs = compare.discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["run_result"] is None


# ---------------------------------------------------------------------------
# tolerant loaders
# ---------------------------------------------------------------------------

def test_load_yaml_malformed_returns_empty(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("key: [unterminated\n  : nope")
    assert compare.load_yaml(p) == {}


def test_load_yaml_non_mapping_returns_empty(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n")
    assert compare.load_yaml(p) == {}


def test_load_json_malformed_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"truncated": ')
    assert compare.load_json(p) is None


def test_discover_runs_survives_one_malformed_summary(tmp_path):
    _make_run(tmp_path, "good", judges={"quality": {"mean": 3.0}})
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "summary.yaml").write_text("::: not : valid : yaml :::\n- [")
    runs = compare.discover_runs(tmp_path)
    # Both are discovered; the bad one just has an empty summary, no crash.
    assert len(runs) == 2


# ---------------------------------------------------------------------------
# get_model
# ---------------------------------------------------------------------------

def test_get_model_from_run_result():
    run = {"run_result": {"model": "claude-sonnet-4-6"}, "summary": {}, "name": "n"}
    assert compare.get_model(run) == "claude-sonnet-4-6"


def test_get_model_fallback_reconstructs_full_claude_id():
    run = {"run_result": None,
           "summary": {"run_id": "20260730-143000-claude-opus-4-8"},
           "name": "20260730-143000-claude-opus-4-8"}
    # Must match the run_result branch key, not a bare "opus-4-8".
    assert compare.get_model(run) == "claude-opus-4-8"


def test_get_model_fallback_unknown_uses_name_not_merged_bucket():
    run = {"run_result": None, "summary": {"run_id": "baseline"}, "name": "baseline"}
    assert compare.get_model(run) == "baseline"


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------

def test_aggregate_values():
    agg = compare.aggregate([1.0, 3.0, None, 2.0])
    assert agg["avg"] == 2.0
    assert agg["min"] == 1.0
    assert agg["max"] == 3.0
    assert agg["count"] == 3


def test_aggregate_empty():
    agg = compare.aggregate([None, None])
    assert agg == {"avg": None, "min": None, "max": None, "count": 0}


# ---------------------------------------------------------------------------
# _is_pass_rate (authoritative via judges[j].pass_rate)
# ---------------------------------------------------------------------------

def test_is_pass_rate_true_for_boolean_judge():
    runs = [{"summary": {"judges": {"passes": {"mean": 0.5, "pass_rate": 0.5}}}}]
    assert compare._is_pass_rate("passes", runs) is True


def test_is_pass_rate_false_for_numeric_judge_even_if_all_01():
    # Numeric judge whose values happen to be 0/1 must NOT be treated as pct.
    runs = [{"summary": {"judges": {"errors": {"mean": 0.5, "pass_rate": None}}}}]
    assert compare._is_pass_rate("errors", runs) is False


# ---------------------------------------------------------------------------
# best_worst_indices + _rank_color
# ---------------------------------------------------------------------------

def test_best_worst_indices_normal():
    best, worst = compare.best_worst_indices([1.0, 3.0, 2.0], higher_is_better=True)
    assert best == 1 and worst == 0


def test_best_worst_indices_all_equal():
    assert compare.best_worst_indices([2.0, 2.0, 2.0]) == (None, None)


def test_rank_color_all_equal_is_uncolored():
    assert compare._rank_color(1.0, [1.0, 1.0, 1.0], True) == ""


def test_rank_color_best_and_worst():
    assert compare._rank_color(3.0, [1.0, 2.0, 3.0], True) == "green"
    assert compare._rank_color(1.0, [1.0, 2.0, 3.0], True) == "red"


# ---------------------------------------------------------------------------
# fmt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("v,expected", [(45, "45s"), (130, "2 min"), (0, "0s")])
def test_fmt_time_sub_minute(v, expected):
    assert compare.fmt(v, "time") == expected


@pytest.mark.parametrize("v,expected", [(800, "800"), (1500, "1.5K"), (2_000_000, "2.0M")])
def test_fmt_tokens(v, expected):
    assert compare.fmt(v, "tokens") == expected


def test_fmt_range_single_vs_multi():
    assert compare.fmt_range({"avg": 2.0, "min": 2.0, "max": 2.0, "count": 1}, "usd") == "$2.00"
    ranged = compare.fmt_range({"avg": 2.0, "min": 1.0, "max": 3.0, "count": 2}, "usd")
    assert ranged == "$2.00 ($1.00-$3.00)"


# ---------------------------------------------------------------------------
# short_name derivation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("claude-opus-4-8", "Opus 4.8"),
    ("claude-opus-4-8[1m]", "Opus 4.8 [1M]"),
    ("claude-opus-5", "Opus 5"),          # not in map -> derived
    ("gpt-4o", "gpt-4o"),                  # non-claude -> unchanged
])
def test_short_name(model, expected):
    assert compare.short_name(model) == expected


# ---------------------------------------------------------------------------
# generate_report smoke tests
# ---------------------------------------------------------------------------

def test_generate_report_two_models(tmp_path):
    _make_run(tmp_path, "opus", model="claude-opus-4-8",
              judges={"quality": {"mean": 4.5}}, cost=2.0)
    _make_run(tmp_path, "sonnet", model="claude-sonnet-4-6",
              judges={"quality": {"mean": 3.5}}, cost=0.5)
    runs = compare.discover_runs(tmp_path)
    out = tmp_path / "report"
    index = compare.generate_report(runs, "T", None, out)
    html = Path(index).read_text()

    assert Path(index).exists()
    assert 'data-tab="claude-opus-4-8"' in html
    assert 'data-tab="claude-sonnet-4-6"' in html
    # single-run models -> "Total Cost" label (no multi-run averaging)
    assert "Total Cost" in html
    # iframes reference the per-run slug copies, which exist on disk
    for r in runs:
        assert (out / r["slug"] / "report.html").exists()
        assert f'src="{r["slug"]}/report.html"' in html


def test_generate_report_shared_basename_no_overwrite(tmp_path):
    # Two runs of the same model, same directory basename, distinct reports.
    _make_run(tmp_path, "evalA/2026-07-30-opus", model="claude-opus-4-8",
              cost=1.0, html_marker="<html>REPORT-A</html>")
    _make_run(tmp_path, "evalB/2026-07-30-opus", model="claude-opus-4-8",
              cost=3.0, html_marker="<html>REPORT-B</html>")
    runs = compare.discover_runs(tmp_path)
    out = tmp_path / "report"
    index = compare.generate_report(runs, "T", None, out)
    html = Path(index).read_text()

    # Both reports survive (no clobber) with their distinct content.
    copies = sorted(out.glob("*/report.html"))
    assert len(copies) == 2
    contents = {c.read_text() for c in copies}
    assert contents == {"<html>REPORT-A</html>", "<html>REPORT-B</html>"}

    # Grouped as one model with two runs -> multi-run averaging + range.
    assert "Avg Run Cost" in html
    assert "$1.00-$3.00" in html


def test_generate_report_missing_html_shows_placeholder(tmp_path):
    _make_run(tmp_path, "opus", model="claude-opus-4-8", with_html=False)
    runs = compare.discover_runs(tmp_path)
    out = tmp_path / "report"
    index = compare.generate_report(runs, "T", None, out)
    html = Path(index).read_text()
    assert "No HTML report available" in html
