"""Tests for the judge -> Harbor reward.json bridge (agent_eval/harbor/reward.py).

Covers the pure composition logic and an end-to-end run using inline `check`
judges only (no LLM / API key needed), verifying the bridge reuses the
score.py engine and writes Harbor's reward contract.
"""

import json
from pathlib import Path

import pytest
import yaml

from agent_eval.config import EvalConfig, RewardConfig
from agent_eval.harbor import reward as reward_mod


# --- compose_reward (pure) ---------------------------------------------------

def test_compose_reward_gate_fail():
    per_judge = {
        "files_exist": {"value": True, "judge_type": "check"},
        "frontmatter_valid": {"value": False, "judge_type": "check"},
        "rfe_quality": {"value": 5, "judge_type": "llm"},
    }
    reward, metrics = reward_mod.compose_reward(per_judge)
    assert reward == 0.0  # a failing boolean gate zeroes the reward
    assert metrics["files_exist"] == 1.0
    assert metrics["frontmatter_valid"] == 0.0
    assert metrics["rfe_quality"] == 5.0


def test_compose_reward_numeric_average():
    per_judge = {
        "ok": {"value": True, "judge_type": "check"},
        "rfe_quality": {"value": 5, "judge_type": "llm"},      # -> 1.0
        "revision_quality": {"value": 3, "judge_type": "llm"},  # -> 0.5
    }
    reward, metrics = reward_mod.compose_reward(per_judge)
    assert reward == pytest.approx(0.75)  # mean(1.0, 0.5)


def test_compose_reward_all_pass_no_numeric():
    per_judge = {
        "a": {"value": True, "judge_type": "check"},
        "b": {"value": True, "judge_type": "check"},
    }
    reward, metrics = reward_mod.compose_reward(per_judge)
    assert reward == 1.0


def test_compose_reward_ignores_skipped_and_errored():
    per_judge = {
        "skipped": {"value": None, "rationale": "Skipped", "judge_type": "check"},
        "errored": {"value": None, "error": "boom", "judge_type": "llm"},
        "ok": {"value": True, "judge_type": "check"},
    }
    reward, metrics = reward_mod.compose_reward(per_judge)
    assert reward == 1.0           # None values neither gate nor average
    assert "skipped" not in metrics
    assert "errored" not in metrics


# --- end-to-end with inline check judges -------------------------------------

def _write_config(tmp_path: Path, judges: list) -> EvalConfig:
    raw = {
        "name": "t",
        "skill": "t",
        "dataset": {"path": ""},
        "outputs": [{"path": "artifacts/out", "schema": "any"}],
        "judges": judges,
    }
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return EvalConfig.from_yaml(cfg_path)


def test_build_and_write_reward_inline_checks(tmp_path):
    # A case workspace with one produced artifact.
    case_dir = tmp_path / "case-001"
    (case_dir / "artifacts" / "out").mkdir(parents=True)
    (case_dir / "artifacts" / "out" / "foo.md").write_text("hello foo")

    config = _write_config(tmp_path, [
        {
            "name": "has_files",
            "check": (
                'files = outputs.get("files", {})\n'
                'return (len(files) > 0, f"{len(files)} files")\n'
            ),
        },
        {
            "name": "mentions_foo",
            "check": (
                'files = outputs.get("files", {})\n'
                'return (any("foo" in k for k in files), "looked for foo")\n'
            ),
        },
    ])

    payload = reward_mod.build_reward(config, case_dir)
    assert payload["reward"] == 1.0
    assert payload["metrics"]["has_files"] == 1.0
    assert payload["metrics"]["mentions_foo"] == 1.0

    out_dir = tmp_path / "logs" / "verifier"
    reward_mod.write_reward(payload, out_dir, case_dir=case_dir)

    reward_json = json.loads((out_dir / "reward.json").read_text())
    assert reward_json["reward"] == 1.0
    assert reward_json["has_files"] == 1.0
    assert (out_dir / "reward.txt").read_text() == "1.0"
    # sidecar carries rationale, written both in out_dir and next to artifacts
    sidecar = json.loads((case_dir / "judges.json").read_text())
    assert sidecar["per_judge"]["has_files"]["value"] is True
    assert "files" in sidecar["per_judge"]["has_files"]["rationale"]


def test_build_reward_failing_check_zeroes_reward(tmp_path):
    case_dir = tmp_path / "case-002"
    (case_dir / "artifacts" / "out").mkdir(parents=True)
    (case_dir / "artifacts" / "out" / "foo.md").write_text("hello")

    config = _write_config(tmp_path, [
        {
            "name": "requires_missing",
            "check": (
                'files = outputs.get("files", {})\n'
                'return (any("MISSING" in k for k in files), "needs MISSING file")\n'
            ),
        },
    ])

    payload = reward_mod.build_reward(config, case_dir)
    assert payload["reward"] == 0.0
    assert payload["metrics"]["requires_missing"] == 0.0


# --- RewardConfig: weighted mode ---------------------------------------------

def test_reward_weighted_basic():
    per_judge = {
        "a": {"value": 5},
        "b": {"value": 3},
    }
    cfg = RewardConfig(
        formula="weighted",
        weights={"a": 0.7, "b": 0.3},
        gate=False,
        score_range=[1, 5],
    )
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    expected = (0.7 * 1.0 + 0.3 * 0.5) / 1.0
    assert r == pytest.approx(expected)


def test_reward_weighted_normalizes_by_weight_sum():
    """Weights that don't sum to 1.0 are still normalized."""
    per_judge = {"a": {"value": 5}, "b": {"value": 5}}
    cfg = RewardConfig(
        formula="weighted",
        weights={"a": 1.0, "b": 1.0},
        gate=False,
        score_range=[1, 5],
    )
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    assert r == pytest.approx(1.0)


def test_reward_weighted_gate_zeros():
    per_judge = {
        "gate": {"value": False},
        "score": {"value": 5},
    }
    cfg = RewardConfig(
        formula="weighted",
        weights={"score": 1.0},
        gate=True,
        score_range=[1, 5],
    )
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    assert r == 0.0


# --- RewardConfig: expression mode -------------------------------------------

def test_reward_expression_simple():
    per_judge = {"x": {"value": 3}, "y": {"value": 5}}
    cfg = RewardConfig(
        formula="0.5 * x + 0.5 * y",
        gate=False,
        score_range=[1, 5],
    )
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    assert r == pytest.approx(0.5 * 0.5 + 0.5 * 1.0)


def test_reward_expression_multiline():
    per_judge = {
        "a": {"value": True},
        "b": {"value": False},
        "score": {"value": 4},
    }
    formula = "gate = mean([a, b])\ngate * score"
    cfg = RewardConfig(formula=formula, gate=False, score_range=[1, 5])
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    expected = 0.5 * 0.75  # mean([1.0, 0.0]) * (4-1)/(5-1)
    assert r == pytest.approx(expected)


def test_reward_expression_raw_judges():
    """Judges in the raw list skip score_range normalization."""
    per_judge = {"eff": {"value": 0.5}, "score": {"value": 4}}
    cfg = RewardConfig(
        formula="0.5 * score + 0.5 * eff",
        gate=False,
        score_range=[1, 5],
        raw=["eff"],
    )
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    expected = 0.5 * 0.75 + 0.5 * 0.5  # score normalized, eff raw
    assert r == pytest.approx(expected)


def test_reward_expression_malformed_returns_zero():
    per_judge = {"x": {"value": 3}}
    cfg = RewardConfig(formula="this is not valid python !!!", gate=False)
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    assert r == 0.0


def test_reward_expression_rejects_dangerous_code():
    """AST validation blocks import/attribute access attempts."""
    per_judge = {"x": {"value": 3}}
    cfg = RewardConfig(
        formula='__import__("os").system("id")',
        gate=False,
        score_range=[1, 5],
    )
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    assert r == 0.0


# --- RewardConfig: single-judge (judge:) mode --------------------------------

def test_reward_judge_clamped_by_default():
    """judge mode uses the value as-is, clamped to [0, 1]."""
    per_judge = {"my_score": {"value": 0.73}}
    cfg = RewardConfig(judge="my_score", gate=False)
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    assert r == pytest.approx(0.73)


def test_reward_judge_clamps_out_of_range():
    """A judge value above 1.0 is clamped, not normalized."""
    per_judge = {"my_score": {"value": 1.5}}
    cfg = RewardConfig(judge="my_score", gate=False)
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    assert r == 1.0


def test_reward_judge_normalize_via_score_range():
    """normalize=true maps the value from score_range to [0, 1]."""
    per_judge = {"rfe_quality": {"value": 4}}
    cfg = RewardConfig(judge="rfe_quality", gate=False,
                       score_range=[1, 5], normalize=True)
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    assert r == pytest.approx(0.75)  # (4-1)/(5-1)


def test_reward_judge_missing_scores_zero():
    """A skipped/errored judge (value None or absent) scores 0.0."""
    cfg = RewardConfig(judge="my_score", gate=False)
    assert reward_mod.compute_reward_from_config(
        {"my_score": {"value": None}}, cfg) == 0.0
    assert reward_mod.compute_reward_from_config({}, cfg) == 0.0


def test_reward_judge_gate_still_applies():
    """gate=true zeros the reward when a boolean judge fails, even in judge mode."""
    per_judge = {"my_score": {"value": 0.9}, "passed": {"value": False}}
    cfg = RewardConfig(judge="my_score", gate=True)
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    assert r == 0.0


# --- RewardConfig: compose_reward integration --------------------------------

def test_compose_reward_uses_reward_cfg():
    per_judge = {"a": {"value": 5}, "b": {"value": 3}}
    cfg = RewardConfig(
        formula="weighted",
        weights={"a": 0.6, "b": 0.4},
        gate=False,
        score_range=[1, 5],
    )
    reward, metrics = reward_mod.compose_reward(per_judge, reward_cfg=cfg)
    expected = (0.6 * 1.0 + 0.4 * 0.5) / 1.0
    assert reward == pytest.approx(expected)
    assert metrics["a"] == 5.0
    assert metrics["b"] == 3.0


def test_compose_reward_falls_back_without_cfg():
    """No reward_cfg = legacy gate+average behavior."""
    per_judge = {
        "gate": {"value": True},
        "s": {"value": 3},
    }
    reward, _ = reward_mod.compose_reward(per_judge)
    assert reward == pytest.approx(0.5)  # (3-1)/(5-1)


# --- EvalConfig parsing ------------------------------------------------------

def test_reward_config_parsed_from_yaml(tmp_path):
    raw = {
        "name": "t", "skill": "t",
        "reward": {
            "formula": "weighted",
            "weights": {"a": 0.5, "b": 0.5},
            "gate": False,
            "score_range": [1, 10],
            "raw": ["b"],
        },
    }
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    config = EvalConfig.from_yaml(cfg_path)

    assert config.reward is not None
    assert config.reward.formula == "weighted"
    assert config.reward.weights == {"a": 0.5, "b": 0.5}
    assert config.reward.gate is False
    assert config.reward.score_range == [1.0, 10.0]
    assert config.reward.raw == ["b"]


def _judge_cfg(reward, judges=None):
    """Build a raw eval.yaml dict with a reward block and defined judges."""
    return {
        "name": "t", "skill": "t",
        "judges": judges or [{"name": "my_reward", "check": "x"}],
        "reward": reward,
    }


def test_reward_config_judge_parsed(tmp_path):
    raw = _judge_cfg({"judge": "my_reward"})
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    config = EvalConfig.from_yaml(cfg_path)
    assert config.reward.judge == "my_reward"
    assert config.reward.normalize is False
    assert config.reward.gate is False  # defaults to False in judge mode


def test_reward_config_judge_normalize(tmp_path):
    raw = _judge_cfg({"judge": "my_reward", "normalize": True, "gate": True})
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    config = EvalConfig.from_yaml(cfg_path)
    assert config.reward.normalize is True
    assert config.reward.gate is True  # explicit override respected


def test_reward_config_judge_rejects_unknown(tmp_path):
    raw = _judge_cfg({"judge": "nope"})
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="does not match any defined judge"):
        EvalConfig.from_yaml(cfg_path)


def test_reward_config_judge_conflicts_with_formula(tmp_path):
    raw = _judge_cfg({"judge": "my_reward", "formula": "weighted"})
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="cannot be combined with"):
        EvalConfig.from_yaml(cfg_path)


def test_reward_config_judge_rejects_bad_normalize(tmp_path):
    raw = _judge_cfg({"judge": "my_reward", "normalize": "yes"})
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="normalize must be a boolean"):
        EvalConfig.from_yaml(cfg_path)


def test_reward_config_rejects_bad_gate(tmp_path):
    raw = {
        "name": "t", "skill": "t",
        "reward": {"gate": "false"},
    }
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="gate must be a boolean"):
        EvalConfig.from_yaml(cfg_path)


def test_reward_config_rejects_inverted_range(tmp_path):
    raw = {
        "name": "t", "skill": "t",
        "reward": {"score_range": [5, 1]},
    }
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="increasing"):
        EvalConfig.from_yaml(cfg_path)


def test_reward_config_rejects_negative_weight(tmp_path):
    raw = {
        "name": "t", "skill": "t",
        "reward": {"formula": "weighted", "weights": {"a": -0.5, "b": 1.0}},
    }
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="non-negative"):
        EvalConfig.from_yaml(cfg_path)


def test_reward_config_rejects_malformed_formula(tmp_path):
    """A syntactically invalid expression fails at config load, not silently."""
    raw = {
        "name": "t", "skill": "t",
        "reward": {"formula": "0.5 * quality + "},
    }
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="formula is invalid"):
        EvalConfig.from_yaml(cfg_path)


def test_reward_config_rejects_unsafe_formula(tmp_path):
    """An unsafe construct is rejected at config load."""
    raw = {
        "name": "t", "skill": "t",
        "reward": {"formula": '__import__("os").system("id")'},
    }
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="formula is invalid"):
        EvalConfig.from_yaml(cfg_path)


def test_reward_config_accepts_valid_expression(tmp_path):
    """A valid expression formula parses and is stored verbatim."""
    raw = {
        "name": "t", "skill": "t",
        "reward": {"formula": "0.6 * quality + 0.4 * efficiency",
                   "raw": ["efficiency"]},
    }
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    config = EvalConfig.from_yaml(cfg_path)
    assert config.reward.formula == "0.6 * quality + 0.4 * efficiency"


# --- formula sandbox hardening (bounded numeric arithmetic) ------------------

def _expect_invalid_formula(tmp_path, formula):
    raw = {"name": "t", "skill": "t", "reward": {"formula": formula}}
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="formula is invalid"):
        EvalConfig.from_yaml(cfg_path)


def test_reward_formula_rejects_power_operator(tmp_path):
    """** is excluded — integer exponentiation is a cheap CPU/memory blow-up."""
    _expect_invalid_formula(tmp_path, "2 ** quality")


def test_reward_formula_rejects_huge_constant(tmp_path):
    _expect_invalid_formula(tmp_path, "9999999999 * quality")


def test_reward_formula_rejects_string_constant(tmp_path):
    """String literals (and thus "x" * N repetition blow-ups) are rejected."""
    _expect_invalid_formula(tmp_path, '"x" * 5 + quality')


def test_reward_formula_rejects_oversized_ast(tmp_path):
    """A formula past the node-count cap is rejected at config load."""
    _expect_invalid_formula(tmp_path, " + ".join(["quality"] * 300))


def test_reward_formula_allows_list_for_mean():
    """List literals stay allowed so mean([...]) keeps working."""
    reward_mod.validate_formula("mean([a, b, c])")  # must not raise
    per_judge = {"a": {"value": 5}, "b": {"value": 3}, "c": {"value": 1}}
    cfg = RewardConfig(formula="mean([a, b, c])", gate=False,
                       score_range=[1, 5])
    r = reward_mod.compute_reward_from_config(per_judge, cfg)
    assert r == pytest.approx((1.0 + 0.5 + 0.0) / 3)
