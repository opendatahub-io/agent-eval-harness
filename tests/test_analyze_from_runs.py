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


# --- cost provenance in the runs loader (spec 014 PR-4) ------------------------

def _write_run(runs_dir, name, *, model, cost, cost_source=None, routing=None, value=4):
    import json as _json
    import yaml as _yaml

    d = runs_dir / name
    d.mkdir(parents=True)
    rr = {"model": model, "cost_usd": cost}
    if cost_source:
        rr["cost_source"] = cost_source
    if routing:
        rr["routing"] = routing
    (d / "run_result.json").write_text(_json.dumps(rr))
    (d / "summary.yaml").write_text(_yaml.safe_dump({
        "run_id": name, "judges": {"q": {"mean": value}},
        "per_case": {"case-1": {"q": {"value": value, "rationale": "r"}}}}))
    return d


def test_run_cost_info_and_degraded_runs_are_excluded_unless_allowed(tmp_path, caplog):
    import logging
    from types import SimpleNamespace

    from analyze import _run_cost_info

    runs = tmp_path / "runs"
    clean = _write_run(runs, "clean", model="m", cost=0.5, cost_source="openrouter:generation",
                       routing={"degraded": False, "violations": [], "audit_complete": True})
    degraded = _write_run(runs, "degraded", model="m", cost=0.6, cost_source="openrouter:generation",
                          routing={"degraded": True, "degraded_reason": "violations",
                                   "violations": [{"gen_id": "x"}], "audit_complete": True})
    legacy = _write_run(runs, "legacy", model="m", cost=0.4)
    assert _run_cost_info(clean) == {"cost": 0.5, "source": "openrouter:generation",
                                     "degraded": False, "audit_clean": True, "enforcement": "none"}
    assert _run_cost_info(degraded)["degraded"] is True and _run_cost_info(degraded)["audit_clean"] is False
    assert _run_cost_info(legacy)["source"] == "runner:reported"
    assert _run_cost_info(tmp_path / "missing")["cost"] is None

    config = SimpleNamespace(reward=None, judges=[])
    with caplog.at_level(logging.WARNING):
        rows, factors, costs = load_conditions_from_runs(runs, config)
    assert len(rows) == 2                                   # degraded run skipped
    assert any("routing audit degraded" in r.message for r in caplog.records)
    assert any("mixed cost sources" in r.message for r in caplog.records)   # real + legacy
    caplog.clear()
    rows_all, _, _ = load_conditions_from_runs(runs, config, allow_unaudited=True)
    assert len(rows_all) == 3


def test_mixed_enforcement_is_not_pooled_unless_allowed(tmp_path, caplog):
    import logging
    from types import SimpleNamespace

    from analyze import load_conditions_from_runs

    runs = tmp_path / "runs"
    _write_run(runs, "audit-1", model="m", cost=0.5, cost_source="openrouter:generation",
               routing={"enforcement": "audit", "violations": [], "audit_complete": True})
    _write_run(runs, "guardrail-1", model="m", cost=0.4, cost_source="openrouter:generation",
               routing={"enforcement": "key-guardrail", "violations": [], "audit_complete": True})
    cfg = SimpleNamespace(reward=None, judges=[])
    with caplog.at_level(logging.WARNING):
        rows, _, _ = load_conditions_from_runs(runs, cfg)
    assert {r["replication"] for r in rows} == {0}
    assert any("routing enforcement 'key-guardrail' differs" in m for m in caplog.messages)
    rows, _, _ = load_conditions_from_runs(runs, cfg, allow_mixed_enforcement=True)
    assert {r["replication"] for r in rows} == {0, 1}

