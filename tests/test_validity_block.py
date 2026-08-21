"""summary['validity'] assembly (score.build_validity_block) and rendering.

Covers the P8 rows (nested stability.irr read through the ONE _judge_irr
accessor — never flat keys), the three layer stanzas (v1 audit detection +
null-probe passthrough, v2 wildcard interception + defensive simulator
read, v3 min_gated_alpha), the never-numeric v_total frame with unmeasured
layers NAMED, the conservative same-family caveat (silent on unknown ids),
and the report section (order, em-dash + samples hint, precision title
attr, no Landis-Koch adjectives). The block is NON-GATING by design.
"""

import copy
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

from agent_eval.config import (  # noqa: E402
    DatasetConfig, EvalConfig, GenerationConfig, InputsConfig, JudgeConfig,
    ModelsConfig, ToolInputConfig,
)
from score import _judge_irr, build_validity_block  # noqa: E402
import report  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**kw):
    kw.setdefault("judges", [JudgeConfig(name="quality", prompt="grade it")])
    return EvalConfig(name="t", skill="s", **kw)


def _irr(**overrides):
    irr = {"metric": "krippendorff_alpha", "level": "ordinal", "value": 0.72,
           "reason_code": None, "reason": None, "n_units": 3,
           "label": ("single-judge self-consistency alpha "
                     "(upper bound on inter-rater reliability)"),
           "rationale": "alpha selected: resampled single judge",
           "n_ratings": 9}
    irr.update(overrides)
    return irr


def _agg(irr=None, human=None, **kw):
    agg = {"mean": 4.0, "pass_rate": None, "scored_cases": 3, **kw}
    if irr is not None:
        agg["stability"] = {"samples": 3, "stable_cases": 2,
                            "total_cases": 3, "irr": irr}
    if human is not None:
        agg["human_agreement"] = human
    return agg


def _rows(block):
    return block["judges"]


# ---------------------------------------------------------------------------
# _judge_irr — the ONE accessor, nested shape only
# ---------------------------------------------------------------------------

class TestJudgeIrrAccessor:
    def test_reads_the_nested_stability_irr_dict(self):
        agg = _agg(irr=_irr())
        assert _judge_irr(agg)["value"] == 0.72

    def test_flat_keys_are_never_read(self):
        """The pre-canonical flat shape (stability.irr_value) is NOT the
        contract — the accessor returns None for it."""
        agg = {"stability": {"samples": 3, "irr_value": 0.9,
                             "irr_metric": "krippendorff_alpha"}}
        assert _judge_irr(agg) is None

    def test_none_safe(self):
        assert _judge_irr(None) is None
        assert _judge_irr({}) is None
        assert _judge_irr({"stability": None}) is None
        assert _judge_irr({"stability": {"samples": 3}}) is None
        assert _judge_irr({"stability": {"irr": {}}}) is None


# ---------------------------------------------------------------------------
# Per-judge P8 rows
# ---------------------------------------------------------------------------

class TestJudgeRows:
    def test_row_with_irr_and_explicit_threshold(self):
        cfg = _cfg(thresholds={"quality": {"min_alpha": 0.75}})
        block = build_validity_block(cfg, {"quality": _agg(irr=_irr())})
        (row,) = _rows(block)
        assert row["judge"] == "quality"
        assert row["irr"]["metric"] == "krippendorff_alpha"
        assert row["irr"]["value"] == 0.72
        assert row["irr"]["threshold"] == 0.75
        assert row["irr"]["rationale"]
        assert row["human_agreement"] is None

    def test_row_without_irr_is_null(self):
        block = build_validity_block(_cfg(), {"quality": _agg()})
        (row,) = _rows(block)
        assert row["irr"] is None

    def test_degenerate_irr_keeps_reason_code_never_a_value(self):
        irr = _irr(value=None, reason_code="perfect_agreement",
                   reason="all ratings identical")
        block = build_validity_block(_cfg(), {"quality": _agg(irr=irr)})
        (row,) = _rows(block)
        assert row["irr"]["value"] is None
        assert row["irr"]["reason_code"] == "perfect_agreement"

    def test_consequence_tier_threshold_without_mutation(self):
        """A consequence-tagged judge gets its tier bound through
        effective_thresholds(); config.thresholds is NEVER mutated."""
        cfg = _cfg(judges=[JudgeConfig(name="quality", prompt="x",
                                       consequence="safety")],
                   thresholds={})
        before = copy.deepcopy(cfg.thresholds)
        block = build_validity_block(cfg, {"quality": _agg(irr=_irr())})
        (row,) = _rows(block)
        assert row["irr"]["threshold"] == 0.70
        assert cfg.thresholds == before

    def test_no_threshold_is_null(self):
        block = build_validity_block(_cfg(), {"quality": _agg(irr=_irr())})
        assert _rows(block)[0]["irr"]["threshold"] is None

    def test_human_agreement_passthrough(self):
        human = {"metric": "cohen_kappa", "level": "nominal", "value": 0.61,
                 "reason_code": None, "reason": None, "n_units": 8,
                 "label": "agreement with a single human reviewer (n=8)",
                 "rationale": "2 fixed raters"}
        block = build_validity_block(
            _cfg(), {"quality": _agg(irr=_irr(), human=human)})
        (row,) = _rows(block)
        assert row["human_agreement"] == {"metric": "cohen_kappa",
                                          "value": 0.61, "n": 8}

    def test_empty_aggregation_yields_no_rows(self):
        block = build_validity_block(_cfg(), {})
        assert _rows(block) == []
        assert block["layers"]["v3"]["status"] == "unmeasured"


# ---------------------------------------------------------------------------
# V1 — task generation
# ---------------------------------------------------------------------------

class TestV1Layer:
    def test_no_audit_is_unmeasured(self, tmp_path):
        cfg = _cfg(dataset=DatasetConfig(path=str(tmp_path)))
        v1 = build_validity_block(cfg, {})["layers"]["v1"]
        assert v1["status"] == "unmeasured"
        assert v1["dataset_audit"] == "absent"
        assert v1["manifest"] == "absent"
        assert "null_probe" not in v1

    def test_audit_and_manifest_detected(self, tmp_path):
        (tmp_path / "dataset_audit.yaml").write_text("audit_version: 1\n")
        (tmp_path / "manifest.yaml").write_text("generator_model: m\n")
        cfg = _cfg(dataset=DatasetConfig(path=str(tmp_path)))
        v1 = build_validity_block(cfg, {})["layers"]["v1"]
        assert v1["status"] == "partially-measured"
        assert v1["dataset_audit"] == "present"
        assert v1["manifest"] == "present"

    def test_null_probe_passthrough(self, tmp_path):
        (tmp_path / "dataset_audit.yaml").write_text(yaml.safe_dump(
            {"audit_version": 1,
             "null_probe": {"null_pass_rate": 0.25, "flagged": ["c1"]}}))
        cfg = _cfg(dataset=DatasetConfig(path=str(tmp_path)))
        v1 = build_validity_block(cfg, {})["layers"]["v1"]
        assert v1["null_probe"] == {"null_pass_rate": 0.25}

    def test_unparseable_audit_still_counts_as_present(self, tmp_path):
        (tmp_path / "dataset_audit.yaml").write_text("{ not: [ yaml")
        cfg = _cfg(dataset=DatasetConfig(path=str(tmp_path)))
        v1 = build_validity_block(cfg, {})["layers"]["v1"]
        assert v1["dataset_audit"] == "present"
        assert "null_probe" not in v1

    def test_generation_strategy_recorded(self):
        cfg = _cfg(generation=GenerationConfig(strategy="synthetic"))
        v1 = build_validity_block(cfg, {})["layers"]["v1"]
        assert v1["generation_strategy"] == "synthetic"

    def test_default_strategy_is_skill(self):
        v1 = build_validity_block(_cfg(), {})["layers"]["v1"]
        assert v1["generation_strategy"] == "skill"


# ---------------------------------------------------------------------------
# V2 — simulator
# ---------------------------------------------------------------------------

class TestV2Layer:
    def test_no_interception_is_not_applicable(self):
        v2 = build_validity_block(_cfg(), {})["layers"]["v2"]
        assert v2["status"] == "not-applicable"
        assert v2["intercepts_ask_user"] is False

    def test_explicit_askuserquestion_handler(self):
        cfg = _cfg(inputs=InputsConfig(tools=[
            ToolInputConfig(match="Intercept AskUserQuestion prompts",
                            prompt="answer sensibly")]),
                   models=ModelsConfig(hook="claude-haiku-4-5"))
        v2 = build_validity_block(cfg, {})["layers"]["v2"]
        assert v2["intercepts_ask_user"] is True
        assert v2["status"] == "uncalibrated simulator"
        assert v2["hook_model"] == "claude-haiku-4-5"

    def test_wildcard_handler_counts_as_intercepting(self):
        """extract_tool_patterns falls back to ['*'], and the runtime '*'
        prefix-matches EVERY tool — AskUserQuestion included."""
        cfg = _cfg(inputs=InputsConfig(tools=[
            ToolInputConfig(match="everything else",
                            prompt="deny")]))
        v2 = build_validity_block(cfg, {})["layers"]["v2"]
        assert v2["intercepts_ask_user"] is True
        assert v2["status"] == "uncalibrated simulator"

    def test_bash_only_handler_does_not_count(self):
        cfg = _cfg(inputs=InputsConfig(tools=[
            ToolInputConfig(match="block scripts calling the API",
                            prompt="deny")]))
        v2 = build_validity_block(cfg, {})["layers"]["v2"]
        assert v2["intercepts_ask_user"] is False
        assert v2["status"] == "not-applicable"

    def test_defensive_simulator_block_read(self):
        """A future summary['simulator'] block supplies its own status."""
        cfg = _cfg(inputs=InputsConfig(tools=[
            ToolInputConfig(match="Intercept AskUserQuestion")]))
        summary = {"simulator": {"status": "measured",
                                 "gold_agreement": 0.9}}
        v2 = build_validity_block(cfg, {}, summary=summary)["layers"]["v2"]
        assert v2["status"] == "measured"

    def test_malformed_simulator_block_falls_back(self):
        cfg = _cfg(inputs=InputsConfig(tools=[
            ToolInputConfig(match="Intercept AskUserQuestion")]))
        v2 = build_validity_block(
            cfg, {}, summary={"simulator": "oops"})["layers"]["v2"]
        assert v2["status"] == "uncalibrated simulator"


# ---------------------------------------------------------------------------
# V3 + v_total
# ---------------------------------------------------------------------------

class TestV3AndVTotal:
    def test_min_gated_alpha_over_gated_judges(self):
        cfg = _cfg(judges=[JudgeConfig(name="a", prompt="x"),
                           JudgeConfig(name="b", prompt="y")],
                   thresholds={"a": {"min_alpha": 0.7},
                               "b": {"min_alpha": 0.7}})
        agg = {"a": _agg(irr=_irr(value=0.81)),
               "b": _agg(irr=_irr(value=0.72))}
        v3 = build_validity_block(cfg, agg)["layers"]["v3"]
        assert v3["min_gated_alpha"] == 0.72

    def test_min_gated_alpha_null_when_any_gated_judge_lacks_alpha(self):
        cfg = _cfg(judges=[JudgeConfig(name="a", prompt="x"),
                           JudgeConfig(name="b", prompt="y")],
                   thresholds={"a": {"min_alpha": 0.7},
                               "b": {"min_alpha": 0.7}})
        agg = {"a": _agg(irr=_irr(value=0.81)), "b": _agg()}
        v3 = build_validity_block(cfg, agg)["layers"]["v3"]
        assert v3["min_gated_alpha"] is None

    def test_v3_embeds_the_rows(self):
        block = build_validity_block(_cfg(), {"quality": _agg(irr=_irr())})
        assert block["layers"]["v3"]["judges"] == block["judges"]

    def test_v_total_is_never_a_number(self):
        block = build_validity_block(_cfg(), {"quality": _agg(irr=_irr())})
        assert block["v_total"]["value"] is None
        assert "V_total <= V1 x V2 x V3" in block["v_total"]["frame"]
        assert "2608.00794" in block["v_total"]["frame"]
        # No computed product anywhere in the serialized block.
        dumped = yaml.safe_dump(block, allow_unicode=True)
        assert re.search(r"V_total\s*[=≈:]\s*0?\.\d", dumped) is None

    def test_unmeasured_layers_are_named(self):
        block = build_validity_block(_cfg(), {"quality": _agg()})
        unmeasured = block["v_total"]["unmeasured_layers"]
        assert "V1 (task generation)" in unmeasured
        assert "V3 (judgment)" in unmeasured
        # v2 is not-applicable (no interception) — excluded, not unmeasured.
        assert not any("V2" in name for name in unmeasured)

    def test_intercepting_v2_is_named_unmeasured(self):
        cfg = _cfg(inputs=InputsConfig(tools=[
            ToolInputConfig(match="Intercept AskUserQuestion")]))
        block = build_validity_block(cfg, {})
        assert any("V2" in name
                   for name in block["v_total"]["unmeasured_layers"])

    def test_note_is_guidance_only(self):
        note = build_validity_block(_cfg(), {})["v_total"]["note"]
        assert "0.50" in note and "0.30" in note
        assert "No numeric V_total" in note


# ---------------------------------------------------------------------------
# same_family — conservative, silent on unknowns
# ---------------------------------------------------------------------------

class TestSameFamily:
    def test_positive_all_anthropic(self):
        cfg = _cfg(models=ModelsConfig(skill="claude-opus-4-8",
                                       judge="claude-haiku-4-5"))
        sf = build_validity_block(cfg, {})["same_family"]
        assert sf["family"] == "anthropic"
        assert set(sf["models"]) == {"claude-opus-4-8", "claude-haiku-4-5"}
        assert "B.4" in sf["caveat"]

    def test_negative_mixed_families(self):
        cfg = _cfg(models=ModelsConfig(skill="claude-opus-4-8",
                                       judge="gpt-4o"))
        assert build_validity_block(cfg, {})["same_family"] is None

    def test_unknown_id_anywhere_stays_silent(self):
        cfg = _cfg(models=ModelsConfig(skill="claude-opus-4-8",
                                       judge="my-gateway-alias"))
        assert build_validity_block(cfg, {})["same_family"] is None

    def test_single_role_makes_no_claim(self):
        cfg = _cfg(models=ModelsConfig(judge="claude-haiku-4-5"))
        assert build_validity_block(cfg, {})["same_family"] is None

    def test_run_result_model_wins_over_config_skill(self):
        cfg = _cfg(models=ModelsConfig(skill="claude-opus-4-8",
                                       judge="claude-haiku-4-5"))
        block = build_validity_block(cfg, {}, run_result={"model": "gpt-4o"})
        assert block["same_family"] is None

    def test_per_judge_override_counts(self):
        cfg = _cfg(judges=[JudgeConfig(name="q", prompt="x",
                                       model="claude-haiku-4-5")],
                   models=ModelsConfig(skill="claude-opus-4-8"))
        sf = build_validity_block(cfg, {})["same_family"]
        assert sf["family"] == "anthropic"

    def test_hook_model_counts_only_when_intercepting(self):
        # Hook is gpt-4o but no interception -> the hook plays no role.
        cfg = _cfg(models=ModelsConfig(skill="claude-opus-4-8",
                                       judge="claude-haiku-4-5",
                                       hook="gpt-4o"))
        assert build_validity_block(cfg, {})["same_family"]["family"] == \
            "anthropic"
        cfg.inputs = InputsConfig(tools=[
            ToolInputConfig(match="Intercept AskUserQuestion")])
        assert build_validity_block(cfg, {})["same_family"] is None


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def _block(**kw):
    cfg = kw.pop("cfg", None) or _cfg()
    agg = kw.pop("agg", None)
    if agg is None:
        agg = {"quality": _agg(irr=_irr())}
    return build_validity_block(cfg, agg, **kw)


class TestRenderValidity:
    def test_dash_and_samples_hint_when_irr_absent(self):
        block = _block(agg={"quality": _agg()})
        html = report._render_validity({"validity": block}, {})
        assert "—" in html
        assert "samples" in html  # the title-attr hint names the fix

    def test_section_renders_when_block_absent(self):
        html = report._render_validity({}, {})
        assert "Validity &amp; Reliability" in html
        assert "not computed" in html

    def test_layer_badges_and_upper_bound_caption(self):
        html = report._render_validity({"validity": _block()}, {})
        assert "V1 — task generation" in html
        assert "not-applicable" in html  # v2 skip badge
        assert ("single-judge self-consistency alpha "
                "(upper bound on inter-rater reliability)") in html

    def test_value_renders_with_ci_and_threshold(self):
        block = _block(
            cfg=_cfg(thresholds={"quality": {"min_alpha": 0.75}}),
            agg={"quality": _agg(irr=_irr(ci=[0.51, 0.88]))})
        html = report._render_validity({"validity": block}, {})
        assert "0.720 [0.510, 0.880] (n=3)" in html
        assert "&ge; 0.75" in html

    def test_no_numeric_vtotal_ever(self):
        html = report._render_validity({"validity": _block()}, {})
        assert re.search(r"V_total\s*[=≈:]\s*0?\.\d", html) is None
        assert "V_total &lt;= V1 x V2 x V3" in html

    def test_unmeasured_layers_listed(self):
        html = report._render_validity({"validity": _block()}, {})
        assert "Unmeasured layers:" in html
        assert "V1 (task generation)" in html

    def test_warn_box_only_when_same_family_present(self):
        cfg = _cfg(models=ModelsConfig(skill="claude-opus-4-8",
                                       judge="claude-haiku-4-5"))
        html = report._render_validity({"validity": _block(cfg=cfg)}, {})
        assert "same-family models" in html
        assert "anthropic" in html
        html2 = report._render_validity({"validity": _block()}, {})
        assert "same-family models" not in html2

    def test_human_agreement_column_only_when_present(self):
        human = {"metric": "cohen_kappa", "value": 0.61, "n_units": 8}
        block = _block(agg={"quality": _agg(irr=_irr(), human=human)})
        html = report._render_validity({"validity": block}, {})
        assert "Human agreement" in html
        assert "single human reviewer" in html
        html2 = report._render_validity({"validity": _block()}, {})
        assert "Human agreement" not in html2

    def test_no_landis_koch_adjectives(self):
        block = _block()
        html = report._render_validity({"validity": block}, {}).lower()
        for adjective in ("almost perfect", "substantial agreement",
                          "moderate agreement"):
            assert adjective not in html, adjective


class TestSectionOrderAndPrecision:
    def test_validity_between_scoring_summary_and_regressions(self, tmp_path):
        config = {"name": "t", "judges": [],
                  "thresholds": {"q": {"min_mean": 4.5}}}
        summary = {"run_id": "r1",
                   "judges": {"q": {"mean": 4.0, "scored_cases": 3}},
                   "per_case": {},
                   "validity": _block(agg={"q": _agg()})}
        html = report.generate_report(config, summary, {}, tmp_path)
        i_summary = html.index("Scoring Summary")
        i_validity = html.index("Validity &amp; Reliability")
        i_regressions = html.index("<h2>Regressions</h2>")
        assert i_summary < i_validity < i_regressions

    def test_mean_carries_precision_title_attr(self):
        html = report._render_scoring_summary(
            {"judges": {"q": {"mean": 4.0, "scored_cases": 3}},
             "per_case": {}},
            {"thresholds": {}, "judges": []})
        assert "precision limited by measurement reliability" in html
        assert ">4.00</span>" in html

    def test_pass_rate_column_is_unchanged(self):
        html = report._render_scoring_summary(
            {"judges": {"q": {"pass_rate": 0.8, "scored_cases": 5}},
             "per_case": {}},
            {"thresholds": {}, "judges": []})
        assert "80%" in html
        assert "precision limited" not in html
