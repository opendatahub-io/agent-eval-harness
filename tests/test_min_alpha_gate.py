"""min_alpha gate semantics in score.detect_regressions.

Three states, documented in-function: (1) alpha present and below the bound
is a breach; (2) the perfect-agreement degenerate (all ratings identical,
coefficient 0/0) PASSES; (3) configured-but-unavailable — samples: 1, all
errored, insufficient data — is a regression. include_irr=False skips
min_alpha keys entirely (Harbor/EvalHub scoping). Consequence tiers resolve
at detection time via effective_thresholds() and never mutate
config.thresholds.
"""

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

from agent_eval.config import (  # noqa: E402
    CONSEQUENCE_TIER_MIN_ALPHA, EvalConfig, effective_thresholds,
)
from score import detect_regressions  # noqa: E402


def _judge_with_irr(**irr):
    return {"q": {"mean": 4.0, "scored_cases": 3,
                  "stability": {"samples": 3, "stable_cases": 1,
                                "total_cases": 3, "irr": irr}}}


THRESHOLDS = {"q": {"min_alpha": 0.7}}


# ---------------------------------------------------------------------------
# The three states
# ---------------------------------------------------------------------------

def test_state_1_breach_when_alpha_below_threshold():
    current = _judge_with_irr(value=0.4, metric="krippendorff_alpha",
                              level="ordinal", n_units=3)
    regs = detect_regressions(current, THRESHOLDS)
    assert len(regs) == 1
    assert regs[0].metric == "alpha"
    assert regs[0].current_value == "0.400"
    assert "upper bound on IRR" in regs[0].detail


def test_no_regression_when_alpha_clears_the_bound():
    current = _judge_with_irr(value=0.85, metric="krippendorff_alpha",
                              level="ordinal", n_units=3)
    assert detect_regressions(current, THRESHOLDS) == []


def test_state_2_perfect_agreement_degenerate_passes():
    current = _judge_with_irr(value=None, reason_code="perfect_agreement",
                              reason="all ratings identical")
    assert detect_regressions(current, THRESHOLDS) == []


@pytest.mark.parametrize("reason_code", [
    "insufficient_data", "below_floor", "undefined"])
def test_state_3_unavailable_reason_codes_regress(reason_code):
    current = _judge_with_irr(value=None, reason_code=reason_code,
                              reason=f"({reason_code})")
    regs = detect_regressions(current, THRESHOLDS)
    assert len(regs) == 1
    assert regs[0].metric == "alpha"
    assert regs[0].current_value == "n/a"


def test_state_3_no_stability_block_at_all_regresses():
    """samples: 1 or a deterministic judge — configured-but-unavailable."""
    current = {"q": {"mean": 4.0, "scored_cases": 3}}
    regs = detect_regressions(current, THRESHOLDS)
    assert len(regs) == 1
    assert regs[0].metric == "alpha"
    assert regs[0].current_value == "n/a"
    assert "samples: 1" in regs[0].detail


# ---------------------------------------------------------------------------
# include_irr=False skips min_alpha entirely (Harbor/EvalHub contract)
# ---------------------------------------------------------------------------

def test_include_irr_false_skips_breaches_and_unavailability():
    breach = _judge_with_irr(value=0.4, metric="krippendorff_alpha",
                             level="ordinal", n_units=3)
    assert detect_regressions(breach, THRESHOLDS, include_irr=False) == []
    missing = {"q": {"mean": 4.0, "scored_cases": 3}}
    assert detect_regressions(missing, THRESHOLDS, include_irr=False) == []


def test_include_irr_false_leaves_other_gates_alone():
    current = {"q": {"mean": 2.0, "scored_cases": 3}}
    regs = detect_regressions(
        current, {"q": {"min_alpha": 0.7, "min_mean": 3.0}}, include_irr=False)
    assert [r.metric for r in regs] == ["mean"]


# ---------------------------------------------------------------------------
# Interplay and edge rows
# ---------------------------------------------------------------------------

def test_degenerate_pass_does_not_mask_a_min_mean_breach():
    current = _judge_with_irr(value=None, reason_code="perfect_agreement")
    current["q"]["mean"] = 2.0
    regs = detect_regressions(
        current, {"q": {"min_alpha": 0.7, "min_mean": 3.0}})
    assert [r.metric for r in regs] == ["mean"]


def test_judge_absent_from_current_results_is_skipped():
    assert detect_regressions({}, THRESHOLDS) == []


def test_reserved_kwargs_are_accepted_and_inert():
    current = _judge_with_irr(value=0.4, metric="krippendorff_alpha",
                              level="ordinal", n_units=3)
    with_reserved = detect_regressions(
        current, THRESHOLDS,
        pairwise={"judge": "q", "wins_a": 1},
        simulator={"max_fallback_rate": 0.1})
    without = detect_regressions(current, THRESHOLDS)
    assert [(r.judge_name, r.metric) for r in with_reserved] == \
           [(r.judge_name, r.metric) for r in without]


# ---------------------------------------------------------------------------
# Consequence tiers resolve at detection time (effective_thresholds)
# ---------------------------------------------------------------------------

def _consequence_config(tmp_path, thresholds_yaml=""):
    body = """
name: t
execution:
  skill: s
judges:
  - name: q
    llm_rubric: score it
    score_range: [1, 5]
    samples: 3
    consequence: safety
"""
    if thresholds_yaml:
        body += thresholds_yaml
    p = tmp_path / "eval.yaml"
    p.write_text(body)
    return EvalConfig.from_yaml(p)


def test_tier_default_injected_into_the_view_only(tmp_path):
    config = _consequence_config(tmp_path)
    snapshot = copy.deepcopy(config.thresholds)

    eff = config.effective_thresholds()
    assert eff == {"q": {"min_alpha": CONSEQUENCE_TIER_MIN_ALPHA["safety"]}}

    # NEVER mutated: harbor/run.py reads config.thresholds as a
    # required-judges set, so the raw dict (and its key set) must not move.
    assert config.thresholds == snapshot
    assert set(config.thresholds or {}) == set(snapshot or {})


def test_explicit_min_alpha_beats_the_tier(tmp_path):
    config = _consequence_config(tmp_path, """
thresholds:
  q:
    min_alpha: 0.75
""")
    eff = config.effective_thresholds()
    assert eff["q"]["min_alpha"] == 0.75
    assert config.thresholds == {"q": {"min_alpha": 0.75}}


def test_other_threshold_keys_survive_the_injection(tmp_path):
    config = _consequence_config(tmp_path, """
thresholds:
  q:
    min_mean: 3.5
""")
    eff = config.effective_thresholds()
    assert eff["q"] == {"min_mean": 3.5, "min_alpha": 0.70}
    assert config.thresholds == {"q": {"min_mean": 3.5}}


def test_consequence_only_config_still_gates(tmp_path):
    """A consequence-tagged judge with NO thresholds block regresses when its
    alpha misses the tier default — detection-time resolution end to end."""
    config = _consequence_config(tmp_path)
    current = _judge_with_irr(value=0.5, metric="krippendorff_alpha",
                              level="ordinal", n_units=3)
    regs = detect_regressions(current, config.effective_thresholds())
    assert [r.metric for r in regs] == ["alpha"]
    # ... and the perfect-agreement carve-out passes the same tier gate.
    degenerate = _judge_with_irr(value=None, reason_code="perfect_agreement")
    assert detect_regressions(degenerate, config.effective_thresholds()) == []


def test_module_level_accessor_takes_raw_dict_judges():
    """report.py path: raw eval.yaml judge dicts, no JudgeConfig objects."""
    eff = effective_thresholds(
        {}, [{"name": "j", "consequence": "gating"}])
    assert eff == {"j": {"min_alpha": 0.80}}
    # Invalid consequence strings in raw dicts are skipped silently (from_yaml
    # already rejects them at load).
    assert effective_thresholds({}, [{"name": "j", "consequence": "sev1"}]) == {}
