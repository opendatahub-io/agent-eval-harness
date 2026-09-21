"""Tests for eval-compare's stats-awareness (the eval-anova anova.json bridge).

eval-compare must render an ANOVA/Pareto section when the artifact is present,
stay descriptive when it is absent, and escape user-controlled values.
"""

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "eval-compare" / "scripts"))

import compare  # noqa: E402


def _mk_run(base, run_id, model, scores):
    rd = base / run_id
    rd.mkdir(parents=True)
    per_case = {c: {"quality": {"value": s}} for c, s in scores.items()}
    mean = sum(scores.values()) / len(scores)
    (rd / "summary.yaml").write_text(yaml.dump(
        {"run_id": run_id, "per_case": per_case, "judges": {"quality": {"mean": mean}}}))
    (rd / "run_result.json").write_text(json.dumps(
        {"model": model, "cost_usd": 0.2, "num_turns": 3}))


def _artifact(base, **overrides):
    data = {
        "anova": {"factor": "model", "f_statistic": 9.0, "p_value": 0.03,
                  "significant": True, "method": "rm_anova", "alpha": 0.05},
        "design": {"n_cases": 2, "replications": 1},
        "condition_summaries": [
            {"model": "claude-opus-4-8", "mean": 0.9, "cost": 0.5},
            {"model": "claude-sonnet-4-6", "mean": 0.5, "cost": 0.2},
        ],
        "pareto_frontier": [
            {"model": "claude-opus-4-8", "mean": 0.9, "cost": 0.5},
            {"model": "claude-sonnet-4-6", "mean": 0.5, "cost": 0.2},
        ],
    }
    data.update(overrides)
    (base / "anova.json").write_text(json.dumps(data))


def test_stats_section_rendered_when_artifact_present(tmp_path):
    _mk_run(tmp_path, "r-opus", "claude-opus-4-8", {"c1": 5, "c2": 4})
    _mk_run(tmp_path, "r-sonnet", "claude-sonnet-4-6", {"c1": 3, "c2": 2})
    _artifact(tmp_path)

    stats = compare.load_stats_artifact(tmp_path)
    assert stats is not None
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert 'id="statistics"' in html
    assert "Statistical Significance" in html
    assert "SIGNIFICANT" in html
    assert "Pareto" in html


def test_descriptive_only_without_artifact(tmp_path):
    _mk_run(tmp_path, "r-opus", "claude-opus-4-8", {"c1": 5})
    _mk_run(tmp_path, "r-sonnet", "claude-sonnet-4-6", {"c1": 3})
    assert compare.load_stats_artifact(tmp_path) is None

    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=None)
    html = (out / "index.html").read_text()
    assert 'id="statistics"' not in html
    assert "Comparison" in html  # the normal report still renders


def test_stats_section_escapes_user_controlled_values(tmp_path):
    evil = "m<script>alert(1)</script>"
    _mk_run(tmp_path, "r-evil", evil, {"c1": 5})
    _artifact(tmp_path,
              condition_summaries=[{"model": evil, "mean": 1.0, "cost": 0.1}],
              pareto_frontier=[{"model": evil, "mean": 1.0, "cost": 0.1}])

    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def _corrected_anova(**overrides):
    an = {
        "p_values": {"model": 0.001, "effort": 0.2, "model:effort": 0.7},
        "p_adjusted": {"model": 0.003, "effort": 0.4, "model:effort": 0.7},
        "significant": {"model": True, "effort": False, "model:effort": False},
        "correction": "holm", "family_size": 3,
        "method": "Mixed-effects model (statsmodels mixedlm, per-term Wald tests)",
        "alpha": 0.05, "factors": ["model", "effort"],
    }
    an.update(overrides)
    return an


def test_corrected_artifact_shows_raw_and_adjusted_with_method_named(tmp_path):
    """An adjusted p is never rendered without naming the correction method,
    and interaction terms get their own rows."""
    _mk_run(tmp_path, "r-a", "model-a", {"c1": 5})
    _artifact(tmp_path, anova=_corrected_anova())
    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "p (raw)" in html and "p (adjusted)" in html
    assert "Holm" in html and "family of 3" in html
    assert "model:effort" in html  # interaction row


def test_correction_none_is_labelled_not_silent(tmp_path):
    _mk_run(tmp_path, "r-a", "model-a", {"c1": 5})
    _artifact(tmp_path, anova=_corrected_anova(
        correction="none",
        p_adjusted={"model": 0.001, "effort": 0.2, "model:effort": 0.7}))
    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "No multiple-comparison correction" in html


def test_legacy_artifact_without_correction_keeps_single_p_column(tmp_path):
    """Pre-correction anova.json artifacts (no correction/p_adjusted fields)
    must not grow an unlabelled adjusted column."""
    _mk_run(tmp_path, "r-a", "model-a", {"c1": 5})
    _artifact(tmp_path)  # the default single-factor artifact has no correction
    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "p (adjusted)" not in html
    assert "p-value" in html


def _per_judge_block():
    return {
        "correction": "bh", "family_size": 2, "alpha": 0.05,
        "judges": {
            "quality": {"method": "Repeated-measures ANOVA (pingouin rm_anova)",
                        "terms": {"model": {"p_raw": 0.004, "p_adjusted": 0.008,
                                            "significant": True}},
                        "n_cases": 4, "n_conditions": 2},
            "tests_pass": {"method": "Repeated-measures ANOVA (pingouin rm_anova)",
                           "terms": {"model": {"p_raw": 0.2, "p_adjusted": 0.2,
                                               "significant": False}},
                           "n_cases": 4, "n_conditions": 2},
        },
        "excluded": [{"judge": "always_five",
                      "reason": "constant value — no variance to analyse"}],
    }


def test_per_judge_table_rendered_when_block_present(tmp_path):
    """The judge-by-term screening table names its family and correction and
    lists exclusions with their reasons; without the block the section stays
    exactly as before (no per-judge table)."""
    _mk_run(tmp_path, "r-a", "model-a", {"c1": 5})
    _artifact(tmp_path, per_judge=_per_judge_block())
    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "Per-judge effects (screening)" in html
    assert "quality" in html and "tests_pass" in html
    assert "Benjamini-Hochberg" in html and "family of 2" in html
    # raw p stays visible alongside the adjusted value
    assert "0.0040" in html and "0.0080" in html
    assert "always_five" in html and "constant value" in html

    # no block -> no table (opt-out is the default shape)
    _artifact(tmp_path)
    stats = compare.load_stats_artifact(tmp_path)
    out2 = tmp_path / "rep2"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out2, stats=stats)
    assert "Per-judge effects" not in (out2 / "index.html").read_text()


def test_per_judge_table_escapes_user_controlled_values(tmp_path):
    evil = "j<script>alert(1)</script>"
    _mk_run(tmp_path, "r-a", "model-a", {"c1": 5})
    pj = _per_judge_block()
    pj["judges"] = {evil: pj["judges"]["quality"]}
    pj["excluded"] = [{"judge": evil, "reason": "<img src=x onerror=alert(1)>"}]
    _artifact(tmp_path, per_judge=pj)
    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror" not in html


def test_no_variance_artifact_renders_gracefully(tmp_path):
    _mk_run(tmp_path, "r-a", "model-a", {"c1": 5})
    _artifact(tmp_path,
              anova={"factor": "model", "f_statistic": None, "p_value": None,
                     "significant": False, "method": "rm_anova", "alpha": 0.05,
                     "note": "No variance in response."},
              pareto_frontier=[])
    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "not significant" in html
    assert "No variance in response." in html
