"""summary['simulator'] aggregation + the reserved thresholds.simulator gates.

Covers `aggregate_simulator` (tier distribution, fallback rate, by-source
gold-agreement stratification, deadline skips, ledger scope, the
`cross_simulator` block from models.hook_shadow shadow records),
`_detect_simulator_regressions` (human-stratum-only gold gate, fail-loud on
zero human pairs, explicit-missing rule, the active
min_cross_simulator_agreement gate), the `score.py simulator`
re-aggregation subcommand, the report card's P1 banner + cross-simulator
rows, and the Harbor/EvalHub scoping (strip + include_irr=False skip +
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
    # Question-scoped: 1 fallback / 8 answered questions. Disabled records
    # are per-hook-invocation (no question) and never enter the rate —
    # they are counted separately.
    assert block["fallback_rate"] == round(1 / 8, 3)
    assert block["disabled_events"] == 1
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
    # MIXED_RECORDS carries 1 disabled record: the gate regresses on the
    # question-scoped rate breach AND on the disabled events.
    assert [(r.judge_name, r.metric) for r in regs] == [
        ("simulator", "fallback_rate"), ("simulator", "disabled_events")]
    assert "fallback answer(s) over" in regs[0].detail
    # A satisfied rate does NOT absolve the disabled events — the gate
    # stays protective against interception-off runs.
    regs = _detect_simulator_regressions(block, {"max_fallback_rate": 0.9})
    assert [(r.judge_name, r.metric) for r in regs] == [
        ("simulator", "disabled_events")]
    assert regs[0].detail == ("interception was disabled during the run "
                              "(1 events)")

    # With no disabled records, a satisfied rate is clean.
    no_disabled = [r for r in MIXED_RECORDS if r.get("tier") != "disabled"]
    clean_block = _sim_block(tmp_path / "nd", no_disabled)
    assert _detect_simulator_regressions(
        clean_block, {"max_fallback_rate": 0.9}) == []


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
        {"simulator": {"max_fallback_rate": 0.0, "min_gold_agreement": 0.8,
                       "min_cross_simulator_agreement": 0.7}},
        simulator=None)
    assert sorted(r.metric for r in regs) == ["cross_simulator_agreement",
                                              "fallback_rate",
                                              "gold_agreement"]
    assert all(r.judge_name == "simulator" for r in regs)
    assert all("no simulator block in summary" in r.detail for r in regs)


def test_the_reserved_key_never_hits_the_judge_loop(tmp_path):
    """A judge aggregate containing real judges plus thresholds.simulator
    must not produce phantom judge-loop regressions ('n/a pass_rate' for a
    judge named simulator). Uses a ledger without disabled records so the
    disabled_events rule cannot mask a phantom regression."""
    no_disabled = [r for r in MIXED_RECORDS if r.get("tier") != "disabled"]
    block = _sim_block(tmp_path, no_disabled)
    regs = detect_regressions(
        {"q": {"mean": 4.0, "pass_rate": 1.0, "scored_cases": 3}},
        {"q": {"min_pass_rate": 0.5},
         "simulator": {"max_fallback_rate": 0.9,
                       "min_gold_agreement": 0.6}},
        simulator=block)
    assert regs == []


# --- cross_simulator (models.hook_shadow shadow records) ---------------------

def _shadow(model, answer=None, **extra):
    entry = {"model": model, "answer": answer, "held_out": False}
    entry.update(extra)
    return entry


SHADOW_RECORDS = [
    {"tier": "llm", "question": "Q1", "answer": "A",
     "hook_model": "claude-haiku-4-5",
     "shadows": [_shadow("gemini-2.5-flash", "A"), _shadow("gpt-4o", "A")]},
    {"tier": "override", "question": "Q2", "answer": "B", "source": "human",
     "shadows": [_shadow("gemini-2.5-flash", "B"), _shadow("gpt-4o", "X")]},
    # partial coverage: gpt-4o deadline-skipped — excluded from n_questions
    {"tier": "fallback", "question": "Q3", "answer": "C",
     "shadows": [_shadow("gemini-2.5-flash", "C"),
                 {"model": "gpt-4o", "skipped": "deadline"}]},
    # partial coverage: gemini errored
    {"tier": "llm", "question": "Q4", "answer": "D",
     "hook_model": "claude-haiku-4-5",
     "shadows": [_shadow("gemini-2.5-flash", None, error="boom"),
                 _shadow("gpt-4o", "D")]},
    # no shadows on this record — never enters cross_simulator
    {"tier": "llm", "question": "Q5", "answer": "E",
     "hook_model": "claude-haiku-4-5"},
]


def test_cross_simulator_block_shape_and_rates(tmp_path):
    block = _sim_block(tmp_path, SHADOW_RECORDS)
    cross = block["cross_simulator"]
    # primary = modal recorded hook_model, then each shadow in seen order
    assert cross["models"] == ["claude-haiku-4-5", "gemini-2.5-flash",
                               "gpt-4o"]
    assert cross["families"] == {"anthropic": 1, "google": 1, "openai": 1}
    assert cross["single_family"] is False
    # full shadow coverage = Q1, Q2 only (Q3 skipped shadow, Q4 errored one)
    assert cross["n_questions"] == 2
    assert cross["n_shadowed_questions"] == 4
    assert cross["all_agree_rate"] == 0.5
    assert "uncorrected" in cross["all_agree_label"]
    # per-model rates run over each model's own answered questions
    assert cross["per_model_agreement"] == {"gemini-2.5-flash": 1.0,
                                            "gpt-4o": round(2 / 3, 3)}
    assert cross["shadow_deadline_skips"] == 1
    assert cross["shadow_errors"] == 1
    # 2 covered questions < 10 -> alpha suppressed, never fabricated
    alpha = cross["alpha"]
    assert alpha["value"] is None
    assert alpha["reason_code"] == "insufficient_data"
    assert alpha["n_units"] == 2
    # the single disagreement names every answering model
    assert cross["disagreements"] == [{
        "question": "Q2",
        "answers": {"claude-haiku-4-5": "B", "gemini-2.5-flash": "B",
                    "gpt-4o": "X"}}]


def test_cross_simulator_absent_without_shadow_records(tmp_path):
    assert "cross_simulator" not in _sim_block(tmp_path)  # MIXED_RECORDS


def _bulk_shadow_records(n, disagree_on=(), models=("gemini-2.5-flash",)):
    records = []
    for i in range(n):
        primary = "Yes" if i % 2 else "No"
        shadows = []
        for m in models:
            ans = "X" if i in disagree_on else primary
            shadows.append(_shadow(m, ans))
        records.append({"tier": "llm", "question": f"Q{i}",
                        "answer": primary,
                        "hook_model": "claude-haiku-4-5",
                        "shadows": shadows})
    return records


def test_cross_simulator_alpha_computed_at_ten_questions(tmp_path):
    block = _sim_block(tmp_path, _bulk_shadow_records(12, disagree_on=(3, 7)))
    cross = block["cross_simulator"]
    assert cross["n_questions"] == 12
    alpha = cross["alpha"]
    assert isinstance(alpha["value"], float)
    assert alpha["metric"] == "krippendorff_alpha"
    assert alpha["level"] == "nominal"
    assert alpha["n_units"] == 12
    assert "reason_code" not in alpha
    assert cross["all_agree_rate"] == round(10 / 12, 3)


def test_cross_simulator_disagreements_capped_at_twenty(tmp_path):
    records = _bulk_shadow_records(30, disagree_on=range(25))
    cross = _sim_block(tmp_path, records)["cross_simulator"]
    assert len(cross["disagreements"]) == 20


def test_cross_simulator_single_family_flag(tmp_path):
    """PR8 panel rule: single_family is True ONLY with zero unknown-family
    models AND exactly one known family. An unclassifiable gateway alias
    SILENCES the claim (silence contract) — it must never let within-family
    agreement be reported as a single-family finding it cannot verify."""
    cross = _sim_block(
        tmp_path,
        _bulk_shadow_records(2, models=("claude-opus-4-8",)))[
            "cross_simulator"]
    assert cross["single_family"] is True

    cross = _sim_block(
        tmp_path / "alias",
        _bulk_shadow_records(2, models=("claude-opus-4-8", "my-alias")))[
            "cross_simulator"]
    assert cross["single_family"] is False
    assert cross["families"] == {"anthropic": 2, "unknown": 1}


def test_cross_simulator_gate_three_states(tmp_path):
    """Active min_cross_simulator_agreement: breach / clean /
    configured-but-unavailable (no shadows at all, and shadows without a
    single fully covered question)."""
    block = _sim_block(tmp_path, SHADOW_RECORDS)  # all_agree_rate 0.5
    regs = _detect_simulator_regressions(
        block, {"min_cross_simulator_agreement": 0.8})
    assert [r.metric for r in regs] == ["cross_simulator_agreement"]
    assert regs[0].current_value == "0.500"
    assert "families:" in regs[0].detail
    assert "single_family" not in regs[0].detail  # cross-family panel

    assert _detect_simulator_regressions(
        block, {"min_cross_simulator_agreement": 0.5}) == []

    # no shadow records ever -> loud pointer at models.hook_shadow
    no_shadows = _sim_block(tmp_path / "ns")
    regs = _detect_simulator_regressions(
        no_shadows, {"min_cross_simulator_agreement": 0.5})
    assert len(regs) == 1
    assert "configure models.hook_shadow" in regs[0].detail

    # shadows exist but zero fully covered questions
    partial = [{"tier": "llm", "question": "Q1", "answer": "A",
                "hook_model": "claude-haiku-4-5",
                "shadows": [{"model": "gemini-2.5-flash",
                             "skipped": "deadline"}]}]
    block = _sim_block(tmp_path / "partial", partial)
    regs = _detect_simulator_regressions(
        block, {"min_cross_simulator_agreement": 0.5})
    assert len(regs) == 1
    assert "no question has full shadow coverage" in regs[0].detail


def test_cross_simulator_breach_notes_single_family(tmp_path):
    block = _sim_block(
        tmp_path, _bulk_shadow_records(4, disagree_on=(0, 1, 2),
                                       models=("claude-opus-4-8",)))
    regs = _detect_simulator_regressions(
        block, {"min_cross_simulator_agreement": 0.9})
    assert len(regs) == 1
    assert "single_family: true" in regs[0].detail
    assert "not cross-family robustness" in regs[0].detail


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
    # Unit-honest card: the rate row names its units; the disabled record
    # renders as its own fail-styled row, never inside the rate.
    assert "fallback answers over answered questions" in html
    assert "Interception disabled" in html
    assert "1 event(s)" in html


def test_report_renders_nothing_without_a_block():
    assert report._render_simulator({}, {}) == ""
    assert report._render_simulator({"simulator": {}}, {}) == ""


def test_report_cross_simulator_rows_and_disagreements(tmp_path):
    block = _sim_block(tmp_path, SHADOW_RECORDS)
    html = report._render_simulator(
        {"simulator": block},
        {"thresholds": {"simulator": {
            "min_cross_simulator_agreement": 0.8}}})
    assert "Cross-simulator agreement" in html
    # agreement renders NEXT TO the family composition (the sensitivity
    # observable), with the gate colored as a breach (0.5 < 0.8)
    assert "anthropic x1, google x1, openai x1" in html
    assert "gate: &ge; 0.8" in html
    assert '<span class="fail">50.0%</span>' in html
    assert "Per-shadow vs primary" in html
    assert "alpha suppressed" in html  # the suppressed alpha says why
    # compact, collapsible disagreement list
    assert "<details>" in html
    assert "1 cross-simulator disagreement(s)" in html
    assert "Q2" in html
    # cross-family panel: no single-family caveat
    assert "Single-family shadow panel" not in html


def test_report_cross_simulator_single_family_caveat(tmp_path):
    block = _sim_block(
        tmp_path, _bulk_shadow_records(4, models=("claude-opus-4-8",)))
    html = report._render_simulator({"simulator": block}, {})
    assert "Single-family shadow panel" in html
    assert "not cross-family robustness" in html


def test_report_without_cross_simulator_block_has_no_rows(tmp_path):
    html = report._render_simulator(
        {"simulator": _sim_block(tmp_path)}, {})
    assert "Cross-simulator agreement" not in html


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
