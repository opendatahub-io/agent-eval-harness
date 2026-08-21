"""Judge-vs-human calibration (score.py calibration, measurement-validity PR6).

Covers the join semantics (reduced values, null exclusion, malformed /
off-scale / unmatched entries), structural metric selection through the
2-fixed-rater select path (Cohen's kappa on bool judges, Krippendorff's
alpha ordinal/interval), the n<5 raw-table suppression, the exact
single-reviewer label, cmd_calibration end-to-end persistence of BOTH
targets (per-judge human_agreement + run-level human_calibration), the
re-score invalidation, and the report rendering surfaces.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import log_results  # noqa: E402 — conftest puts eval-mlflow/scripts on sys.path
import report  # noqa: E402
import score  # noqa: E402
from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.reliability import (  # noqa: E402
    INTERVAL, NOMINAL, ORDINAL, REASON_INSUFFICIENT_DATA,
    REASON_PERFECT_AGREEMENT, krippendorff_alpha,
)
from score import (  # noqa: E402
    CALIBRATION_FLOOR, HUMAN_AGREEMENT_LABEL, _calibration_join,
    _calibration_scale, _merge_summary, cmd_calibration,
    compute_human_agreement, detect_regressions,
)

BOOL_JC = SimpleNamespace(feedback_type="bool", score_range=None)
SCORE_JC = SimpleNamespace(feedback_type="", score_range=[1.0, 5.0])
FLOAT_JC = SimpleNamespace(feedback_type="float", score_range=[0.0, 1.0])


# ---------------------------------------------------------------------------
# Join semantics
# ---------------------------------------------------------------------------

class TestCalibrationJoin:
    def test_join_uses_the_reduced_value_not_stability_values(self):
        """The comparison target is the _aggregate_samples reduction stored
        in per_case[...]['value'] — never the raw sample spread."""
        per_case = {"c1": {"q": {"value": True,
                                 "stability": {"values": [True, False, True]}}}}
        joined = _calibration_join(per_case, {"c1": {"q": True}}, {"q": BOOL_JC})
        assert joined["q"]["pairs"] == [("c1", True, True)]

    def test_if_skipped_and_errored_nulls_are_excluded_separately(self, capsys):
        per_case = {
            "c1": {"q": {"value": None, "rationale": "Skipped: condition"}},
            "c2": {"q": {"value": None, "error": "boom", "rationale": "x"}},
            "c3": {"q": {"value": True}},
        }
        verdicts = {"c1": {"q": True}, "c2": {"q": False}, "c3": {"q": True}}
        joined = _calibration_join(per_case, verdicts, {"q": BOOL_JC})
        assert joined["q"]["pairs"] == [("c3", True, True)]
        assert joined["q"]["excluded"]["skipped"] == 1
        assert joined["q"]["excluded"]["errored"] == 1
        err = capsys.readouterr().err
        assert "skipped" in err and "errored" in err

    def test_malformed_verdicts_are_excluded_loudly(self, capsys):
        per_case = {"c1": {"q": {"value": True}, "s": {"value": 4}}}
        verdicts = {"c1": {"q": 3, "s": "great"}}  # wrong types both ways
        joined = _calibration_join(per_case, verdicts,
                                   {"q": BOOL_JC, "s": SCORE_JC})
        assert joined["q"]["pairs"] == []
        assert joined["q"]["excluded"]["malformed"] == 1
        assert joined["s"]["pairs"] == []
        assert joined["s"]["excluded"]["malformed"] == 1
        assert "not a bool" in capsys.readouterr().err

    def test_off_scale_human_values_are_excluded_never_clamped(self, capsys):
        per_case = {"c1": {"s": {"value": 4}}}
        joined = _calibration_join(per_case, {"c1": {"s": 7}}, {"s": SCORE_JC})
        assert joined["s"]["pairs"] == []  # 7 was not clamped to 5
        assert joined["s"]["excluded"]["off_scale"] == 1
        assert "never clamped" in capsys.readouterr().err

    def test_verdict_for_a_case_absent_from_per_case_is_unmatched(self, capsys):
        joined = _calibration_join({"c1": {"q": {"value": True}}},
                                   {"c9": {"q": True}}, {"q": BOOL_JC})
        assert joined["q"]["pairs"] == []
        assert joined["q"]["excluded"]["unmatched"] == 1
        assert "not found" in capsys.readouterr().err

    def test_unknown_judge_names_are_skipped_with_a_warning(self, capsys):
        joined = _calibration_join({"c1": {"q": {"value": True}}},
                                   {"c1": {"nope": True}}, {"q": BOOL_JC})
        assert "nope" not in joined
        assert "unknown judge" in capsys.readouterr().err

    def test_non_mapping_case_entries_are_skipped_not_crashed(self, capsys):
        per_case = {"c1": {"q": {"value": True}}}
        verdicts = {"c1": {"q": True}, "c2": "looks fine"}
        joined = _calibration_join(per_case, verdicts, {"q": BOOL_JC})
        assert joined["q"]["pairs"] == [("c1", True, True)]
        assert "not a mapping" in capsys.readouterr().err

    def test_unhashable_verdicts_are_excluded_not_crashed(self, capsys):
        """Agent-written YAML can put a LIST or a DICT where a scalar
        verdict belongs. On a non-bool/non-numeric (string) judge value
        those used to sail through the join and crash Counter() inside
        cohen_kappa — they must be excluded as malformed instead."""
        text_jc = SimpleNamespace(feedback_type="", score_range=None)
        per_case = {"c1": {"t": {"value": "Alpha"}},
                    "c2": {"t": {"value": "Beta"}},
                    "c3": {"t": {"value": "Gamma"}}}
        verdicts = {"c1": {"t": ["Alpha", "Beta"]},   # YAML list
                    "c2": {"t": {"answer": "Beta"}},  # YAML mapping
                    "c3": {"t": "Gamma"}}             # matching scalar
        joined = _calibration_join(per_case, verdicts, {"t": text_jc})
        assert joined["t"]["pairs"] == [("c3", "Gamma", "Gamma")]
        assert joined["t"]["excluded"]["malformed"] == 2
        assert "matching hashable scalars" in capsys.readouterr().err

    def test_scalar_verdict_of_the_wrong_family_is_malformed(self, capsys):
        """A hashable scalar is not enough — it must live on the judge's
        own scale family (a numeric verdict against a string judge value
        would make the coefficient meaningless)."""
        text_jc = SimpleNamespace(feedback_type="", score_range=None)
        joined = _calibration_join({"c1": {"t": {"value": "Alpha"}}},
                                   {"c1": {"t": 3}}, {"t": text_jc})
        assert joined["t"]["pairs"] == []
        assert joined["t"]["excluded"]["malformed"] == 1
        capsys.readouterr()


# ---------------------------------------------------------------------------
# Scale selection + coefficient computation
# ---------------------------------------------------------------------------

class TestCalibrationScale:
    def test_bool_values_are_nominal(self):
        assert _calibration_scale(BOOL_JC, [("c", True, False)]) == NOMINAL

    def test_integer_score_range_is_ordinal(self):
        assert _calibration_scale(SCORE_JC, [("c", 3, 4)]) == ORDINAL

    def test_float_feedback_is_interval(self):
        assert _calibration_scale(FLOAT_JC, [("c", 0.5, 0.7)]) == INTERVAL

    def test_check_judge_with_bool_values_is_nominal(self):
        """Deterministic check judges declare no feedback_type; the joined
        bool values decide (first-class calibration targets)."""
        check_jc = SimpleNamespace(feedback_type="", score_range=None)
        assert _calibration_scale(check_jc, [("c", True, True),
                                             ("d", False, True)]) == NOMINAL


class TestComputeHumanAgreement:
    def test_bool_judge_gets_cohen_kappa_via_the_2_rater_select_path(self):
        h = [True, True, True, True, False, False, False, False]
        j = [True, True, True, False, False, False, False, True]
        pairs = [(f"c{i}", h[i], j[i]) for i in range(8)]
        block = compute_human_agreement(pairs, NOMINAL)
        # Hand-computed: po=0.75, pe=0.5 -> kappa=0.5
        assert block["metric"] == "cohen_kappa"
        assert block["value"] == pytest.approx(0.5)
        assert block["level"] == NOMINAL
        assert block["n_units"] == 8
        assert block["agreement_raw"] == pytest.approx(0.75)
        assert block["rationale"]  # select_irr_metric's P8 sentence
        assert "two fixed raters" in block["rationale"].lower()

    def test_the_label_is_verbatim(self):
        pairs = [(f"c{i}", True, i % 2 == 0) for i in range(8)]
        block = compute_human_agreement(pairs, NOMINAL)
        assert block["label"] == "agreement with a single human reviewer (n=8)"
        assert HUMAN_AGREEMENT_LABEL.format(n=8) == block["label"]

    def test_ordinal_judge_gets_krippendorff_alpha_matching_the_primitive(self):
        h = [1, 2, 3, 4, 5, 3]
        j = [1, 2, 3, 4, 4, 2]
        pairs = [(f"c{i}", h[i], j[i]) for i in range(6)]
        block = compute_human_agreement(pairs, ORDINAL)
        oracle = krippendorff_alpha([[a, b] for a, b in zip(h, j)],
                                    level=ORDINAL)
        assert block["metric"] == "krippendorff_alpha"
        assert block["level"] == ORDINAL
        assert block["value"] == pytest.approx(oracle.value)

    def test_interval_scale_routes_to_alpha_too(self):
        h = [0.1, 0.5, 0.9, 0.4, 0.7]
        j = [0.2, 0.5, 0.8, 0.4, 0.6]
        pairs = [(f"c{i}", h[i], j[i]) for i in range(5)]
        block = compute_human_agreement(pairs, INTERVAL)
        assert block["metric"] == "krippendorff_alpha"
        assert block["level"] == INTERVAL
        assert isinstance(block["value"], float)

    def test_below_the_floor_no_coefficient_only_the_raw_table(self):
        pairs = [("c1", True, True), ("c2", True, False),
                 ("c3", False, False), ("c4", True, True)]
        block = compute_human_agreement(pairs, NOMINAL)
        assert block["value"] is None
        assert block["reason_code"] == REASON_INSUFFICIENT_DATA
        assert "floor (5)" in block["reason"]
        assert "uncorrected" in block["reason"]
        assert block["agreement_raw"] == pytest.approx(0.75)
        assert block["pairs"] == [
            {"case": "c1", "human": True, "judge": True, "match": True},
            {"case": "c2", "human": True, "judge": False, "match": False},
            {"case": "c3", "human": False, "judge": False, "match": True},
            {"case": "c4", "human": True, "judge": True, "match": True},
        ]

    def test_perfect_agreement_is_a_reason_code_never_a_coefficient(self):
        pairs = [(f"c{i}", True, True) for i in range(6)]
        block = compute_human_agreement(pairs, NOMINAL)
        assert block["value"] is None
        assert block["reason_code"] == REASON_PERFECT_AGREEMENT
        assert block["agreement_raw"] == pytest.approx(1.0)

    def test_the_floor_is_overridable(self):
        pairs = [("c1", True, True), ("c2", True, False), ("c3", False, True)]
        block = compute_human_agreement(pairs, NOMINAL, floor=2)
        assert block["reason_code"] != REASON_INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# Gate detail (parity rows live in test_threshold_consumers.py)
# ---------------------------------------------------------------------------

def test_stale_calibration_regression_names_the_fix():
    judges = {"q": {"mean": 4.0, "scored_cases": 5}}
    regs = detect_regressions(judges, {"q": {"min_human_agreement": 0.6}},
                              human_calibration={"judges": ["q"]})
    assert len(regs) == 1
    assert "stale calibration" in regs[0].detail
    assert "re-run score.py calibration" in regs[0].detail


def test_breach_detail_carries_the_single_reviewer_context():
    judges = {"q": {"mean": 4.0, "human_agreement": {
        "metric": "cohen_kappa", "value": 0.2, "n_units": 6}}}
    regs = detect_regressions(judges, {"q": {"min_human_agreement": 0.6}})
    assert len(regs) == 1
    assert "single human reviewer" in regs[0].detail
    assert "n=6" in regs[0].detail


# ---------------------------------------------------------------------------
# cmd_calibration end-to-end
# ---------------------------------------------------------------------------

CONFIG_YAML = """\
name: calib-test
execution:
  skill: fake-skill
judges:
  - name: format_check
    check: "return (True, 'ok')"
  - name: quality
    llm_rubric: score it
    score_range: [1, 5]
{thresholds}"""

# 8 cases: format_check joins all 8 (kappa 0.5); quality joins only 3
# (below the floor -> raw table).
H_BOOL = [True, True, True, True, False, False, False, False]
J_BOOL = [True, True, True, False, False, False, False, True]


def _case_ids():
    return [f"case-{i + 1:03d}" for i in range(8)]


def _summary():
    per_case = {}
    for i, cid in enumerate(_case_ids()):
        per_case[cid] = {
            "format_check": {"value": J_BOOL[i], "rationale": "ok",
                             "judge_type": "check"},
            "quality": {"value": 3, "rationale": "meh", "judge_type": "llm"},
        }
    return {
        "run_id": "r1",
        "judges": {
            "format_check": {"pass_rate": 0.5, "mean": 0.5, "scored_cases": 8},
            "quality": {"mean": 3.0, "pass_rate": None, "scored_cases": 8},
        },
        "per_case": per_case,
    }


def _review():
    verdicts = {}
    for i, cid in enumerate(_case_ids()):
        verdicts[cid] = {"format_check": H_BOOL[i]}
    for cid in _case_ids()[:3]:
        verdicts[cid]["quality"] = 4
    return {
        "run_id": "r1",
        "reviewer": "human",
        "reviewer_id": "antonin",
        "blind": True,
        "selection": "all",
        "feedback": {},
        "verdicts": verdicts,
    }


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    """Config + run dir with summary/review, AGENT_EVAL_RUNS_DIR pointed at it."""
    def _build(summary=None, review=None, thresholds=""):
        cfg_path = tmp_path / "eval.yaml"
        cfg_path.write_text(CONFIG_YAML.format(thresholds=thresholds))
        config = EvalConfig.from_yaml(cfg_path)
        runs_base = tmp_path / "runs"
        runs_dir = runs_base / config.eval_name()
        run_dir = runs_dir / "r1"
        run_dir.mkdir(parents=True)
        if summary is not None:
            (run_dir / "summary.yaml").write_text(
                yaml.dump(summary, default_flow_style=False))
        if review is not None:
            (run_dir / "review.yaml").write_text(
                yaml.dump(review, default_flow_style=False))
        monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs_base))
        return SimpleNamespace(cfg_path=cfg_path, run_dir=run_dir,
                               runs_dir=runs_dir)
    return _build


def _args(env, floor=CALIBRATION_FLOOR):
    return SimpleNamespace(run_id="r1", config=str(env.cfg_path), floor=floor)


class TestCmdCalibration:
    def test_persists_both_targets(self, run_env, capsys):
        env = run_env(summary=_summary(), review=_review())
        cmd_calibration(_args(env))

        summary = yaml.safe_load((env.run_dir / "summary.yaml").read_text())

        # Target 1: per-judge human_agreement merged into summary['judges']
        # — the deterministic check judge is a first-class target.
        ha = summary["judges"]["format_check"]["human_agreement"]
        assert ha["metric"] == "cohen_kappa"
        assert ha["value"] == pytest.approx(0.5)
        assert ha["n_units"] == 8
        assert ha["label"] == "agreement with a single human reviewer (n=8)"

        # quality joined only 3 pairs -> suppressed coefficient + raw table.
        qa = summary["judges"]["quality"]["human_agreement"]
        assert qa["value"] is None
        assert qa["reason_code"] == "insufficient_data"
        assert len(qa["pairs"]) == 3

        # Target 2: the run-level human_calibration block.
        hc = summary["human_calibration"]
        assert hc["reviewer_id"] == "antonin"
        assert hc["blind"] is True
        assert hc["selection"] == "all"
        assert hc["n_reviewed"] == 8
        assert hc["n_total_cases"] == 8
        assert sorted(hc["judges"]) == ["format_check", "quality"]
        assert hc["generated_at"]

        out = capsys.readouterr().out
        assert "cohen_kappa=0.500" in out
        assert "no coefficient" in out  # quality's suppression line
        assert "uncorrected agreement" in out

    def test_reviewer_id_defaults_and_blind_is_conservative(self, run_env):
        review = _review()
        del review["reviewer_id"], review["blind"], review["selection"]
        review["reviewer"] = "human"
        env = run_env(summary=_summary(), review=review)
        cmd_calibration(_args(env))
        hc = yaml.safe_load(
            (env.run_dir / "summary.yaml").read_text())["human_calibration"]
        assert hc["reviewer_id"] == "human"
        assert hc["blind"] is False  # absent -> not blind, conservatively
        assert hc["selection"] == "unspecified"

    def test_exit_1_on_a_breached_min_human_agreement(self, run_env, capsys):
        env = run_env(summary=_summary(), review=_review(), thresholds=(
            "thresholds:\n  format_check:\n    min_human_agreement: 0.6\n"))
        with pytest.raises(SystemExit) as exc:
            cmd_calibration(_args(env))
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "REGRESSIONS: 1" in out
        # Honest-labeling transport: the regression line carries the
        # detail (' — <why>'), not just the bare numbers.
        assert " — " in out
        assert "single human reviewer" in out

    def test_exit_0_when_the_gate_is_satisfied(self, run_env, capsys):
        env = run_env(summary=_summary(), review=_review(), thresholds=(
            "thresholds:\n  format_check:\n    min_human_agreement: 0.4\n"))
        cmd_calibration(_args(env))  # no SystemExit
        assert "REGRESSIONS: 0" in capsys.readouterr().out

    def test_missing_review_yaml_exits_with_a_hint(self, run_env, capsys):
        env = run_env(summary=_summary(), review=None)
        with pytest.raises(SystemExit) as exc:
            cmd_calibration(_args(env))
        assert exc.value.code == 1
        assert "/eval-review" in capsys.readouterr().err

    def test_verdicts_as_a_list_is_a_structural_error(self, run_env, capsys):
        review = _review()
        review["verdicts"] = ["case-001"]
        env = run_env(summary=_summary(), review=review)
        with pytest.raises(SystemExit) as exc:
            cmd_calibration(_args(env))
        assert exc.value.code == 1
        assert "must be a mapping" in capsys.readouterr().err

    def test_malformed_entries_are_skipped_loudly_not_fatally(self, run_env,
                                                              capsys):
        review = _review()
        review["verdicts"]["case-999"] = "looks fine"      # not a mapping
        review["verdicts"]["case-001"]["ghost_judge"] = True  # unknown judge
        env = run_env(summary=_summary(), review=review)
        cmd_calibration(_args(env))
        err = capsys.readouterr().err
        assert "not a mapping" in err
        assert "unknown judge" in err
        summary = yaml.safe_load((env.run_dir / "summary.yaml").read_text())
        assert "human_agreement" in summary["judges"]["format_check"]

    def test_a_re_score_drops_human_agreement_but_not_the_evidence(self,
                                                                   run_env):
        """cmd_judges wholesale-rewrites summary['judges'] (the documented
        invalidation); the surviving human_calibration block is what turns
        the silent skip into a stale-calibration regression."""
        env = run_env(summary=_summary(), review=_review())
        cmd_calibration(_args(env))

        # Simulate the re-score through the real persistence helper.
        fresh_agg = {"format_check": {"pass_rate": 1.0, "scored_cases": 8},
                     "quality": {"mean": 4.0, "scored_cases": 8}}
        _merge_summary("r1", "judges", fresh_agg, env.runs_dir)

        summary = yaml.safe_load((env.run_dir / "summary.yaml").read_text())
        assert "human_agreement" not in summary["judges"]["format_check"]
        assert summary["human_calibration"]["judges"]  # evidence survives

        regs = detect_regressions(
            summary["judges"], {"format_check": {"min_human_agreement": 0.6}},
            human_calibration=summary["human_calibration"])
        assert len(regs) == 1
        assert "stale calibration" in regs[0].detail


# ---------------------------------------------------------------------------
# Validity-block refresh (the producer, in real pipeline order)
# ---------------------------------------------------------------------------

class TestValidityRefresh:
    def test_pipeline_judges_then_calibration_carries_human_agreement(
            self, run_env, monkeypatch, capsys):
        """Real subcommand order: cmd_judges assembles the validity block
        (necessarily without calibration — it drops any prior one), then
        cmd_calibration must refresh the persisted validity rows itself.
        Without that refresh, summary['validity'].judges[*].human_agreement
        can NEVER carry data in any real ordering — so the MLflow routing
        never emits {judge}/human_agreement and the report's validity table
        never grows the column."""
        env = run_env(summary=None, review=_review())
        for cid in _case_ids():
            (env.run_dir / "cases" / cid).mkdir(parents=True)

        values = {cid: J_BOOL[i] for i, cid in enumerate(_case_ids())}

        def fmt(outputs=None, **kwargs):
            return values[Path(outputs["case_dir"]).name], "ok"

        def qual(outputs=None, **kwargs):
            return 3, "meh"

        monkeypatch.setattr(
            score, "load_judges",
            lambda config, root=None: [("format_check", fmt, "", "check", 1),
                                       ("quality", qual, "", "llm", 1)])
        score.cmd_judges(SimpleNamespace(
            run_id="r1", config=str(env.cfg_path), workspace=None,
            model=None, samples=None, no_llm_judges=False))

        summary = yaml.safe_load((env.run_dir / "summary.yaml").read_text())
        rows = {r["judge"]: r for r in summary["validity"]["judges"]}
        assert rows["format_check"]["human_agreement"] is None  # not yet

        cmd_calibration(_args(env))
        capsys.readouterr()

        summary = yaml.safe_load((env.run_dir / "summary.yaml").read_text())
        rows = {r["judge"]: r for r in summary["validity"]["judges"]}
        # Exact row shape build_validity_block emits: {metric, value, n}.
        assert rows["format_check"]["human_agreement"] == {
            "metric": "cohen_kappa", "value": pytest.approx(0.5), "n": 8}
        # Below-floor judge: refreshed too, with the honest null value.
        assert rows["quality"]["human_agreement"] == {
            "metric": "krippendorff_alpha", "value": None, "n": 3}
        # The v3 layer's copy of the rows stays in lockstep.
        v3_rows = {r["judge"]: r
                   for r in summary["validity"]["layers"]["v3"]["judges"]}
        assert (v3_rows["format_check"]["human_agreement"]["value"]
                == pytest.approx(0.5))

        # MLflow routing emits the plottable metric.
        metrics, _tags = log_results._validity_mlflow_fields(summary)
        assert metrics["format_check/human_agreement"] == pytest.approx(0.5)

        # And the report's validity table grows the column.
        html = report._render_validity(summary,
                                       {"thresholds": {}, "judges": []})
        assert "Human agreement" in html
        assert "0.500 (cohen_kappa)" in html


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def _ha_block(**over):
    block = {
        "metric": "cohen_kappa", "level": "nominal", "value": 0.62,
        "reason_code": None, "reason": None, "n_units": 7,
        "label": "agreement with a single human reviewer (n=7)",
        "rationale": "Cohen's kappa selected: two fixed raters ...",
        "agreement_raw": 0.83,
        "pairs": [{"case": "c1", "human": True, "judge": True, "match": True},
                  {"case": "c2", "human": True, "judge": False,
                   "match": False}],
    }
    block.update(over)
    return block


class TestReportRendering:
    def test_scoring_summary_shows_the_human_annotation(self):
        summary = {"judges": {"q": {"mean": 4.0, "scored_cases": 7,
                                    "human_agreement": _ha_block()}},
                   "per_case": {},
                   "human_calibration": {"blind": False, "judges": ["q"]}}
        html = report._render_scoring_summary(
            summary, {"thresholds": {}, "judges": []})
        assert "vs human" in html
        assert "n=7" in html
        assert "reviewer-reported blind: no" in html
        assert "agreement with a single human reviewer (n=7)" in html

    def test_scoring_summary_below_floor_shows_no_coefficient(self):
        ha = _ha_block(value=None, reason_code="insufficient_data", n_units=3,
                       label="agreement with a single human reviewer (n=3)")
        summary = {"judges": {"q": {"mean": 4.0, "scored_cases": 3,
                                    "human_agreement": ha}},
                   "per_case": {}}
        html = report._render_scoring_summary(
            summary, {"thresholds": {}, "judges": []})
        assert "no coefficient" in html

    def test_threshold_column_shows_the_human_bound_once_calibrated(self):
        summary = {"judges": {"q": {"mean": 4.0, "scored_cases": 7,
                                    "human_agreement": _ha_block()}},
                   "per_case": {},
                   "human_calibration": {"blind": True, "judges": ["q"]}}
        html = report._render_scoring_summary(
            summary,
            {"thresholds": {"q": {"min_human_agreement": 0.6}}, "judges": []})
        assert "0.6 vs human" in html

    def test_threshold_column_stays_empty_when_never_calibrated(self):
        summary = {"judges": {"q": {"mean": 4.0, "scored_cases": 7}},
                   "per_case": {}}
        html = report._render_scoring_summary(
            summary,
            {"thresholds": {"q": {"min_human_agreement": 0.6}}, "judges": []})
        assert "vs human" not in html

    def test_calibration_section_renders_caveats_and_the_raw_table(self):
        summary = {
            "judges": {"q": {"mean": 4.0,
                             "human_agreement": _ha_block()}},
            "human_calibration": {"reviewer_id": "antonin", "blind": False,
                                  "selection": "failures", "n_reviewed": 2,
                                  "n_total_cases": 8, "judges": ["q"]},
        }
        html = report._render_calibration(summary)
        assert "Human Calibration" in html
        assert "reviewer-reported blind: no" in html
        assert "collected after judge results were visible" in html
        assert "prevalence-sensitive" in html          # non-random subset
        assert "Single human reviewer" in html          # always
        assert "Raw agreement table" in html
        assert "uncorrected" in html
        assert "c2" in html                             # the mismatch row

    def test_calibration_section_is_empty_without_the_block(self):
        assert report._render_calibration({"judges": {"q": {}}}) == ""

    def test_per_case_shows_the_human_verdict_with_match_tinting(self,
                                                                 tmp_path):
        summary = {"per_case": {
            "case-001": {"q": {"value": True, "rationale": "ok",
                               "judge_type": "check"},
                         "s": {"value": 4, "rationale": "meh",
                               "judge_type": "llm"}}}}
        review = {"reviewer_id": "antonin",
                  "verdicts": {"case-001": {"q": False, "s": 4}}}
        html = report._render_per_case(summary, tmp_path, {}, None, review)
        assert "human: FAIL" in html    # mismatch on q ...
        assert 'class="warn"' in html   # ... warn-tinted
        assert "human: 4" in html       # match on s ...
        assert 'class="pass" title="human verdict' in html  # ... pass-tinted

    def test_per_case_ignores_malformed_verdict_entries(self, tmp_path):
        summary = {"per_case": {
            "case-001": {"q": {"value": True, "rationale": "ok",
                               "judge_type": "check"}}}}
        review = {"verdicts": {"case-001": "looks fine"}}
        html = report._render_per_case(summary, tmp_path, {}, None, review)
        assert "human:" not in html
