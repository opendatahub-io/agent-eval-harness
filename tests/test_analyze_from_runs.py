"""Tests for analyze.analyze_runs — statistics over a directory of standard
eval-run runs (summary.yaml), the migration-critical path that lets any set of
runs be analysed without eval-anova having executed them."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

_scripts_dir = str(Path(__file__).parent.parent / "skills" / "eval-anova" / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from analyze import analyze_runs, load_conditions_from_runs  # noqa: E402


def _mk_run(runs_dir, run_id, model, scores, cost, *, judge="quality", jtype="numeric"):
    rd = runs_dir / run_id
    rd.mkdir(parents=True)
    per_case = {c: {judge: {"value": s, "judge_type": jtype}} for c, s in scores.items()}
    (rd / "summary.yaml").write_text(yaml.dump({"run_id": run_id, "per_case": per_case}))
    (rd / "run_result.json").write_text(json.dumps({"model": model, "cost_usd": cost}))
    return rd


def test_analyze_runs_writes_artifact_with_stats_and_pareto(tmp_path):
    runs = tmp_path / "eval"
    _mk_run(runs, "2026-07-30-opus", "claude-opus-4-8", {"c1": 5, "c2": 4, "c3": 5}, 0.9)
    _mk_run(runs, "2026-07-30-sonnet", "claude-sonnet-4-6", {"c1": 3, "c2": 2, "c3": 4}, 0.3)

    analysis, artifact = analyze_runs(runs, NS(reward=None))

    assert artifact == runs / "anova.json" and artifact.exists()
    assert set(analysis["design"]["factors"]["model"]) == {
        "claude-opus-4-8", "claude-sonnet-4-6"}
    an = analysis["anova"]
    assert an["f_statistic"] is not None and 0.0 <= an["p_value"] <= 1.0
    # 1..5 numeric judge normalised via the default score range → opus > sonnet
    means = {c["model"]: c["mean"] for c in analysis["condition_summaries"]}
    assert means["claude-opus-4-8"] > means["claude-sonnet-4-6"]
    # cost threaded so the Pareto frontier is real (each condition carries cost)
    assert all("cost" in c for c in analysis["condition_summaries"])
    assert analysis["pareto_frontier"], "pareto frontier should not be empty"


def test_boolean_gate_composite_from_summary(tmp_path):
    runs = tmp_path / "eval"
    # a failing boolean gate zeros the case; a passing one leaves the numeric
    _mk_run(runs, "r-a", "model-a",
            {"c1": True, "c2": True}, 0.1, judge="passed", jtype="boolean")
    _mk_run(runs, "r-b", "model-b",
            {"c1": False, "c2": True}, 0.1, judge="passed", jtype="boolean")
    analysis, _ = analyze_runs(runs, NS(reward=None))
    means = {c["model"]: c["mean"] for c in analysis["condition_summaries"]}
    assert means["model-a"] == 1.0   # both pass
    assert means["model-b"] == 0.5   # one fails (gated to 0), one passes


def test_condition_json_levels_override_model(tmp_path):
    runs = tmp_path / "eval"
    rd = _mk_run(runs, "cellA", "ignored", {"c1": 1.0}, 0.1)
    (rd / "condition.json").write_text(
        json.dumps({"levels": {"model": "opus", "effort": "high"}}))
    rows, factors, _ = load_conditions_from_runs(runs, NS(reward=None))
    assert set(factors) == {"model", "effort"}
    assert rows[0]["model"] == "opus" and rows[0]["effort"] == "high"


def test_multiple_runs_same_condition_are_replications(tmp_path):
    runs = tmp_path / "eval"
    _mk_run(runs, "r1", "claude-opus-4-8", {"c1": 5}, 0.1)
    _mk_run(runs, "r2", "claude-opus-4-8", {"c1": 4}, 0.1)
    rows, factors, _ = load_conditions_from_runs(runs, NS(reward=None))
    reps = sorted(r["replication"] for r in rows)
    assert reps == [0, 1]  # two runs of one model → two replications
