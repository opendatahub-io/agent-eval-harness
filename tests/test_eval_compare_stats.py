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
