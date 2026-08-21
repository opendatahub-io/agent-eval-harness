"""Config-load surface of the simulator calibration shadow (PR9).

Covers `inputs.tools[].calibration` parsing, the RESERVED
`thresholds.simulator` mapping key (sub-key whitelist + value validation +
the accepted-but-warned cross-simulator key), the two-stage reservation of
the judge name 'simulator', and `effective_thresholds` passing the reserved
block through untouched.
"""

import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import (  # noqa: E402
    SIMULATOR_THRESHOLD_KEYS, EvalConfig, effective_thresholds,
)


def _write(tmp_path, body, name="eval.yaml"):
    p = tmp_path / name
    p.write_text(body)
    return p


BASE = """
name: t
execution:
  skill: s
"""


# --- inputs.tools[].calibration ---------------------------------------------

def test_calibration_defaults_to_false(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, BASE + """
inputs:
  tools:
    - match: Questions asked via AskUserQuestion.
      prompt: answer from input.yaml
"""))
    assert cfg.inputs.tools[0].calibration is False


def test_calibration_true_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, BASE + """
inputs:
  tools:
    - match: Questions asked via AskUserQuestion.
      prompt: answer from input.yaml
      calibration: true
"""))
    assert cfg.inputs.tools[0].calibration is True


def test_calibration_non_bool_rejected(tmp_path):
    with pytest.raises(ValueError, match=r"inputs\.tools\[0\]\.calibration"):
        EvalConfig.from_yaml(_write(tmp_path, BASE + """
inputs:
  tools:
    - match: Questions asked via AskUserQuestion.
      calibration: "yes"
"""))


def test_calibration_on_non_ask_user_match_warns(tmp_path):
    with pytest.warns(UserWarning, match="AskUserQuestion"):
        EvalConfig.from_yaml(_write(tmp_path, BASE + """
inputs:
  tools:
    - match: Any Jira interaction via Bash scripts.
      calibration: true
"""))


# --- thresholds.simulator (reserved mapping key) -----------------------------

def test_simulator_thresholds_valid_keys_accepted(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg = EvalConfig.from_yaml(_write(tmp_path, BASE + """
thresholds:
  simulator:
    max_fallback_rate: 0.0
    min_gold_agreement: 0.8
"""))
    assert cfg.thresholds["simulator"] == {
        "max_fallback_rate": 0.0, "min_gold_agreement": 0.8}


def test_simulator_thresholds_unknown_sub_key_warns(tmp_path):
    with pytest.warns(UserWarning, match="min_self_consistency"):
        EvalConfig.from_yaml(_write(tmp_path, BASE + """
thresholds:
  simulator:
    min_self_consistency: 0.7
"""))


def test_simulator_thresholds_non_mapping_rejected(tmp_path):
    with pytest.raises(ValueError, match="must be a mapping"):
        EvalConfig.from_yaml(_write(tmp_path, BASE + """
thresholds:
  simulator: 0.7
"""))


@pytest.mark.parametrize("key", sorted(SIMULATOR_THRESHOLD_KEYS))
def test_simulator_thresholds_value_above_one_rejected(tmp_path, key):
    with pytest.raises(ValueError, match=rf"thresholds\.simulator\.{key}"):
        EvalConfig.from_yaml(_write(tmp_path, BASE + f"""
thresholds:
  simulator:
    {key}: 1.5
"""))


def test_simulator_max_fallback_rate_must_be_non_negative(tmp_path):
    with pytest.raises(ValueError, match="max_fallback_rate must be >= 0"):
        EvalConfig.from_yaml(_write(tmp_path, BASE + """
thresholds:
  simulator:
    max_fallback_rate: -0.1
"""))


def test_cross_simulator_key_accepted_but_warned_reserved(tmp_path):
    """min_cross_simulator_agreement activates with cross-family shadow
    simulators (a later commit) — accepted, warned, never evaluated yet."""
    with pytest.warns(UserWarning,
                      match="reserved for cross-family shadow simulators"):
        cfg = EvalConfig.from_yaml(_write(tmp_path, BASE + """
thresholds:
  simulator:
    min_cross_simulator_agreement: 0.7
"""))
    assert cfg.thresholds["simulator"][
        "min_cross_simulator_agreement"] == 0.7


# --- two-stage reservation of the judge name 'simulator' ---------------------

def test_judge_named_simulator_with_the_block_is_a_collision(tmp_path):
    with pytest.raises(ValueError, match="reserved thresholds key"):
        EvalConfig.from_yaml(_write(tmp_path, BASE + """
judges:
  - name: simulator
    check: "return (True, 'ok')"
thresholds:
  simulator:
    max_fallback_rate: 0.0
"""))


def test_judge_named_simulator_alone_only_deprecation_warns(tmp_path):
    with pytest.warns(DeprecationWarning, match="rename the judge"):
        cfg = EvalConfig.from_yaml(_write(tmp_path, BASE + """
judges:
  - name: simulator
    check: "return (True, 'ok')"
"""))
    assert cfg.judges[0].name == "simulator"


# --- effective_thresholds: the reserved block flows through untouched --------

def test_effective_thresholds_passes_simulator_block_through(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, BASE + """
judges:
  - name: q
    llm_rubric: score it
    score_range: [1, 5]
    samples: 3
    consequence: safety
thresholds:
  simulator:
    max_fallback_rate: 0.0
    min_gold_agreement: 0.8
"""))
    eff = cfg.effective_thresholds()
    # untouched pass-through of the reserved key…
    assert eff["simulator"] == {"max_fallback_rate": 0.0,
                                "min_gold_agreement": 0.8}
    # …while the consequence tier still injects for the real judge.
    assert eff["q"]["min_alpha"] == 0.70


def test_effective_thresholds_never_injects_into_the_reserved_key(tmp_path):
    """A (deprecated) judge literally named 'simulator' with a consequence
    tag must not leak min_alpha into the simulator gate block."""
    with pytest.warns(DeprecationWarning):
        cfg = EvalConfig.from_yaml(_write(tmp_path, BASE + """
judges:
  - name: simulator
    llm_rubric: score it
    score_range: [1, 5]
    samples: 3
    consequence: safety
"""))
    eff = effective_thresholds(cfg.thresholds, cfg.judges)
    assert "simulator" not in eff
