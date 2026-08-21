"""summary['simulator'] aggregation + the reserved thresholds.simulator gates.

Covers `aggregate_simulator` (tier distribution, fallback rate, by-source
gold-agreement stratification, deadline skips, ledger scope),
`_detect_simulator_regressions` (human-stratum-only gold gate, fail-loud on
zero human pairs, explicit-missing rule), the `score.py simulator`
re-aggregation subcommand, the report card's P1 banner, and the
Harbor/EvalHub scoping (strip + include_irr=False skip +
config_translator's pass_criteria exclusion).
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import report  # noqa: E402
from score import (  # noqa: E402
    SIM_GOLD_AGENT_LABEL, SIM_GOLD_HUMAN_LABEL,
    _detect_simulator_regressions, aggregate_simulator, cmd_simulator,
    detect_regressions,
)

from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.evalhub.config_translator import (  # noqa: E402
    eval_config_to_provider,
)
from agent_eval.harbor.run import _strip_simulator_thresholds  # noqa: E402


P1_BANNER = "Simulator calibration not validated against human answers"


def _config(tmp_path, *, tools=True, thresholds=None):
    raw = {
        "name": "sim-t",
        "execution": {"skill": "s"},
        "dataset": {"path": ""},
        "judges": [{"name": "q", "check": "return (True, 'ok')\n"}],
    }
    if tools:
        raw["inputs"] = {"tools": [{
            "match": "Questions asked via AskUserQuestion.",
            "prompt": "answer from input.yaml",
            "calibration": True,
        }]}
    if thresholds:
        raw["thresholds"] = thresholds
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "eval.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return EvalConfig.from_yaml(p)


def _cal(gold, shadow, *, agree=None, **extra):
    cal = {"gold": gold, "shadow": shadow,
           "agree": (shadow == gold) if agree is None and shadow is not None
           else agree,
           "held_out": True}
    cal.update(extra)
    return cal


def _write_ledger(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


MIXED_RECORDS = [
    # human pairs: 2 agree, 1 disagree
    {"tier": "override", "question": "Q1", "answer": "A", "source": "human",
     "calibration": _cal("A", "A")},
    {"tier": "override", "question": "Q2", "answer": "B", "source": "human",
     "calibration": _cal("B", "B")},
    {"tier": "override", "question": "Q3", "answer": "C", "source": "human",
     "calibration": _cal("C", "X")},
    # agent pair: 0/1 agree
    {"tier": "override", "question": "Q4", "answer": "D", "source": "agent",
     "calibration": _cal("D", "Y")},
    # deadline-skipped shadow (human) — no pair
    {"tier": "override", "question": "Q5", "answer": "E", "source": "human",
     "calibration": {"gold": "E", "shadow": None, "agree": None,
                     "held_out": True, "skipped": "deadline"}},
    # errored shadow — no pair
    {"tier": "override", "question": "Q6", "answer": "F", "source": "agent",
     "calibration": {"gold": "F", "shadow": None, "agree": None,
                     "held_out": True, "error": "boom"}},
    {"tier": "llm", "question": "Q7", "answer": "G",
     "hook_model": "claude-haiku-4-5"},
    {"tier": "fallback", "question": "Q8", "answer": "H"},
    {"tier": "disabled", "reason": "tool-handlers-missing"},
]


def _run_with_ledger(tmp_path, records=MIXED_RECORDS, *, scope="case"):
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "r1"
    case_dir = run_dir / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    if scope == "case":
        _write_ledger(case_dir / "hook_answers.jsonl", records)
    elif scope == "run":
        _write_ledger(run_dir / "hook_answers.jsonl", records)
    return runs_dir, [case_dir]


# --- aggregation --------------------------------------------------------------

def test_mixed_tiers_and_fallback_rate(tmp_path):
    config = _config(tmp_path)
    runs_dir, case_dirs = _run_with_ledger(tmp_path)
    block = aggregate_simulator(config, "r1", runs_dir, case_dirs)
    assert block["tiers"] == {"override": 6, "llm": 1, "fallback": 1,
                              "disabled": 1}
    assert block["n_questions"] == 8
    # (1 fallback + 1 disabled) / 9 recorded events, rounded to 3 decimals
    assert block["fallback_rate"] == round(2 / 9, 3)
    assert block["ledger_scope"] == "case"
    assert block["hook_model"] == "claude-haiku-4-5"
    assert block["deadline_skips"] == 1
    assert block["generated_at"]


def test_gold_agreement_stratified_by_source(tmp_path):
    config = _config(tmp_path)
    runs_dir, case_dirs = _run_with_ledger(tmp_path)
    cal = aggregate_simulator(config, "r1", runs_dir, case_dirs)["calibration"]
    human = cal["by_source"]["human"]
    agent = cal["by_source"]["agent"]
    assert (human["n"], human["agree"], human["rate"]) == (3, 2, 0.667)
    assert (agent["n"], agent["agree"], agent["rate"]) == (1, 0, 0.0)
    assert human["label"] == SIM_GOLD_HUMAN_LABEL
    assert agent["label"] == SIM_GOLD_AGENT_LABEL
    assert "uncorrected" in human["label"]
    assert "not human calibration" in agent["label"]
    assert cal["n_pairs"] == 4
    assert cal["gold_agreement"] == 0.667  # human stratum, 3 decimals
    assert cal["errors"] == 1
    assert cal["validated"] is True
    assert len(human["pairs"]) == 3
    assert "pairs" not in agent


def test_status_calibrated_iff_human_pairs_exist(tmp_path):
    config = _config(tmp_path)
    runs_dir, case_dirs = _run_with_ledger(tmp_path)
    assert aggregate_simulator(config, "r1", runs_dir,
                               case_dirs)["status"] == "calibrated"

    agent_only = [r for r in MIXED_RECORDS if r.get("source") != "human"]
    runs_dir2, case_dirs2 = _run_with_ledger(tmp_path / "b", agent_only)
    block = aggregate_simulator(config, "r1", runs_dir2, case_dirs2)
    assert block["status"] == "uncalibrated simulator"
    assert block["calibration"]["validated"] is False
    assert block["calibration"]["gold_agreement"] is None


def test_batch_run_root_ledger_is_run_scope(tmp_path):
    config = _config(tmp_path)
    runs_dir, case_dirs = _run_with_ledger(tmp_path, scope="run")
    block = aggregate_simulator(config, "r1", runs_dir, case_dirs)
    assert block["ledger_scope"] == "run"
    assert "not attributed to cases" in block["note"]


def test_missing_ledger_and_no_interception(tmp_path):
    config = _config(tmp_path)
    runs_dir, case_dirs = _run_with_ledger(tmp_path, records=[],
                                           scope="none")
    block = aggregate_simulator(config, "r1", runs_dir, case_dirs)
    assert block["ledger_scope"] == "missing"
    assert block["n_questions"] == 0
    assert block["fallback_rate"] is None

    no_tools = _config(tmp_path / "nt", tools=False)
    assert aggregate_simulator(no_tools, "r1", runs_dir, case_dirs) is None


# --- the reserved gates --------------------------------------------------------

def _sim_block(tmp_path, records=MIXED_RECORDS):
    config = _config(tmp_path)
    runs_dir, case_dirs = _run_with_ledger(tmp_path, records)
    return aggregate_simulator(config, "r1", runs_dir, case_dirs)


def test_max_fallback_rate_breach_regresses(tmp_path):
    block = _sim_block(tmp_path)
    regs = _detect_simulator_regressions(block, {"max_fallback_rate": 0.0})
    assert [(r.judge_name, r.metric) for r in regs] == [
        ("simulator", "fallback_rate")]
    assert _detect_simulator_regressions(
        block, {"max_fallback_rate": 0.9}) == []


def test_min_gold_agreement_gates_the_human_stratum_only(tmp_path):
    """Human stratum 0.667 passes a 0.6 gate even though the agent stratum
    (0.0) is worse — agent pairs are LLM-vs-LLM consistency, never the
    calibration evidence."""
    block = _sim_block(tmp_path)
    assert _detect_simulator_regressions(
        block, {"min_gold_agreement": 0.6}) == []
    regs = _detect_simulator_regressions(block, {"min_gold_agreement": 0.9})
    assert [r.metric for r in regs] == ["gold_agreement"]
    assert regs[0].current_value == "0.667"


def test_zero_human_pairs_regresses_loudly(tmp_path):
    agent_only = [r for r in MIXED_RECORDS if r.get("source") != "human"]
    block = _sim_block(tmp_path, agent_only)
    regs = _detect_simulator_regressions(block, {"min_gold_agreement": 0.1})
    assert len(regs) == 1
    assert "no human-provenance calibration pairs" in regs[0].detail
    assert "case_overrides_source: human" in regs[0].detail


def test_configured_but_no_simulator_block_regresses_per_key():
    regs = detect_regressions(
        {"q": {"mean": 4.0, "scored_cases": 3}},
        {"simulator": {"max_fallback_rate": 0.0, "min_gold_agreement": 0.8}},
        simulator=None)
    assert sorted(r.metric for r in regs) == ["fallback_rate",
                                              "gold_agreement"]
    assert all(r.judge_name == "simulator" for r in regs)
    assert all("no simulator block in summary" in r.detail for r in regs)


def test_the_reserved_key_never_hits_the_judge_loop(tmp_path):
    """A judge aggregate containing real judges plus thresholds.simulator
    must not produce phantom judge-loop regressions ('n/a pass_rate' for a
    judge named simulator)."""
    block = _sim_block(tmp_path)
    regs = detect_regressions(
        {"q": {"mean": 4.0, "pass_rate": 1.0, "scored_cases": 3}},
        {"q": {"min_pass_rate": 0.5},
         "simulator": {"max_fallback_rate": 0.9,
                       "min_gold_agreement": 0.6}},
        simulator=block)
    assert regs == []


def test_cross_simulator_key_is_not_evaluated_yet(tmp_path):
    """min_cross_simulator_agreement is reserved for cross-family shadow
    simulators (a later commit): configured today, it neither regresses nor
    errors — config load already warned about it."""
    block = _sim_block(tmp_path)
    assert _detect_simulator_regressions(
        block, {"min_cross_simulator_agreement": 0.7}) == []
    assert detect_regressions(
        {}, {"simulator": {"min_cross_simulator_agreement": 0.7}},
        simulator=None) == []


# --- subcommand re-aggregation ---------------------------------------------

def test_cmd_simulator_reaggregates_into_summary(tmp_path, monkeypatch,
                                                 capsys):
    config_path = tmp_path / "eval.yaml"
    _config(tmp_path)  # writes eval.yaml
    runs_dir, _ = _run_with_ledger(tmp_path)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs_dir.parent / "byname"))
    # _get_runs_dir scopes by eval name (the skill field, "s") — place the
    # run there.
    scoped = runs_dir.parent / "byname" / "s"
    scoped.mkdir(parents=True)
    (scoped / "r1").symlink_to(runs_dir / "r1")

    cmd_simulator(SimpleNamespace(run_id="r1", config=str(config_path)))
    out = capsys.readouterr().out
    assert "simulator:" in out
    summary = yaml.safe_load((scoped / "r1" / "summary.yaml").read_text())
    sim = summary["simulator"]
    assert sim["status"] == "calibrated"
    assert sim["calibration"]["gold_agreement"] == 0.667


# --- report card ---------------------------------------------------------------

def test_report_banner_fires_without_human_pairs(tmp_path):
    agent_only = [r for r in MIXED_RECORDS if r.get("source") != "human"]
    block = _sim_block(tmp_path, agent_only)
    html = report._render_simulator({"simulator": block}, {})
    assert P1_BANNER in html
    assert "LLM-vs-LLM consistency" in html


def test_report_banner_absent_with_human_pairs(tmp_path):
    block = _sim_block(tmp_path)
    html = report._render_simulator({"simulator": block}, {})
    assert P1_BANNER not in html
    assert "Gold agreement (human)" in html
    assert SIM_GOLD_HUMAN_LABEL in html
    assert "Deadline skips" in html  # 1 deadline-skipped shadow in fixture


def test_report_batch_note_and_gates(tmp_path):
    config = _config(tmp_path)
    runs_dir, case_dirs = _run_with_ledger(tmp_path, scope="run")
    block = aggregate_simulator(config, "r1", runs_dir, case_dirs)
    html = report._render_simulator(
        {"simulator": block},
        {"thresholds": {"simulator": {"max_fallback_rate": 0.0,
                                      "min_gold_agreement": 0.6}}})
    assert "not attributed to cases" in html
    assert "gate: &le; 0.0" in html
    assert "human stratum only" in html


def test_report_renders_nothing_without_a_block():
    assert report._render_simulator({}, {}) == ""
    assert report._render_simulator({"simulator": {}}, {}) == ""


def test_validity_v2_picks_up_the_simulator_status(tmp_path):
    """build_validity_block's V2 stanza reads summary['simulator'].status —
    calibrated vs uncalibrated now flows from human-pair existence."""
    from score import build_validity_block
    config = _config(tmp_path)
    block = _sim_block(tmp_path)
    validity = build_validity_block(config, {}, summary={"simulator": block})
    assert validity["layers"]["v2"]["status"] == "calibrated"

    agent_only = [r for r in MIXED_RECORDS if r.get("source") != "human"]
    block = _sim_block(tmp_path / "b2", agent_only)
    validity = build_validity_block(config, {}, summary={"simulator": block})
    assert validity["layers"]["v2"]["status"] == "uncalibrated simulator"


# --- Harbor / EvalHub scoping ---------------------------------------------------

def test_include_irr_false_skips_the_simulator_gates():
    """Harbor/EvalHub call detect_regressions with include_irr=False (and
    additionally strip the key): the simulator gates never regress those
    paths, keeping the report/MLflow consumers in lockstep with the CLIs."""
    thresholds = {"simulator": {"max_fallback_rate": 0.0,
                                "min_gold_agreement": 0.8}}
    assert detect_regressions({}, thresholds, include_irr=False,
                              simulator=None) == []


def test_harbor_strip_removes_the_key_with_a_notice(capsys):
    thresholds = {"q": {"min_mean": 1.0},
                  "simulator": {"max_fallback_rate": 0.0}}
    stripped = _strip_simulator_thresholds(thresholds)
    assert stripped == {"q": {"min_mean": 1.0}}
    err = capsys.readouterr().err
    assert "thresholds.simulator is not evaluated on the Harbor path" in err

    assert _strip_simulator_thresholds({"q": {"min_mean": 1.0}}) == {
        "q": {"min_mean": 1.0}}
    assert capsys.readouterr().err == ""


def test_config_translator_excludes_the_reserved_key(tmp_path):
    config = _config(tmp_path, thresholds={
        "q": {"min_pass_rate": 1.0},
        "simulator": {"max_fallback_rate": 0.0}})
    provider = eval_config_to_provider(config)
    criteria = provider["benchmarks"][0]["pass_criteria"]["threshold"]
    assert "simulator" not in criteria
    assert criteria == {"q": {"min_pass_rate": 1.0}}


def test_config_translator_omits_pass_criteria_when_only_simulator(tmp_path):
    config = _config(tmp_path,
                     thresholds={"simulator": {"max_fallback_rate": 0.0}})
    provider = eval_config_to_provider(config)
    assert "pass_criteria" not in provider["benchmarks"][0]
