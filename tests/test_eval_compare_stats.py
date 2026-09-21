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


def _contrasts_block(**overrides):
    block = {
        "correction": "holm",
        "family": "pairwise level contrasts within factor 'model'",
        "family_size": 3,
        "contrast_type": "reference-cell",
        "omnibus_p_adjusted": 0.003,
        "note": "Estimates are reference-cell contrasts from the fitted model "
                "— level differences at the other factors' reference levels, "
                "NOT marginal means (the model includes interactions).",
        "pairs": [
            {"a": "claude-opus-4-8", "b": "claude-sonnet-4-6",
             "estimate": 0.06, "se": 0.02, "p_raw": 0.004,
             "p_adjusted": 0.012, "significant": True},
            {"a": "claude-opus-4-8", "b": "claude-haiku-4-5",
             "estimate": 0.01, "se": None, "p_raw": None, "p_adjusted": None,
             "significant": False, "reason": "no finite p"},
        ],
    }
    block.update(overrides)
    return block


def test_pairwise_contrasts_table_named_method_and_raw_p(tmp_path):
    """The A-vs-B table renders per factor, names the correction and its
    within-factor family, keeps raw p beside adjusted, and never fabricates a
    value for a degenerate pair."""
    _mk_run(tmp_path, "r-a", "model-a", {"c1": 5})
    _artifact(tmp_path, anova=_corrected_anova(),
              contrasts={"model": _contrasts_block()})
    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "Pairwise level contrasts (post-hoc)" in html
    assert "Holm-corrected across the 3 contrast(s) within this factor" in html
    assert "omnibus adjusted p: 0.0030" in html
    assert "<td>0.0040</td>" in html and "<td>0.0120</td>" in html  # raw + adjusted
    assert "no test" in html  # degenerate pair, no fabricated p
    assert "NOT marginal means" in html  # reference-cell contrasts labelled


def test_no_contrasts_key_renders_no_posthoc_section(tmp_path):
    _mk_run(tmp_path, "r-a", "model-a", {"c1": 5})
    _artifact(tmp_path, anova=_corrected_anova())
    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "Pairwise level contrasts" not in html


def test_contrasts_escape_user_controlled_level_names(tmp_path):
    evil = "m<script>alert(1)</script>"
    block = _contrasts_block(pairs=[
        {"a": evil, "b": "safe", "estimate": 0.1, "se": 0.1,
         "p_raw": 0.5, "p_adjusted": 0.5, "significant": False}])
    _mk_run(tmp_path, "r-a", "model-a", {"c1": 5})
    _artifact(tmp_path, anova=_corrected_anova(), contrasts={"model": block})
    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_degenerate_contrast_factor_shows_reason(tmp_path):
    _mk_run(tmp_path, "r-a", "model-a", {"c1": 5})
    _artifact(tmp_path, anova=_corrected_anova(), contrasts={"model": _contrasts_block(
        pairs=[], family_size=0,
        reason="No variance in response — no pairwise tests computed.")})
    stats = compare.load_stats_artifact(tmp_path)
    out = tmp_path / "rep"
    compare.generate_report(compare.discover_runs(tmp_path), "T", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert "No pairwise tests: No variance in response" in html


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
