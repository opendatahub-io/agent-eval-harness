"""Tests for the judge -> Harbor reward.json bridge (agent_eval/harbor/reward.py).

Covers the pure composition logic and an end-to-end run using inline `check`
judges only (no LLM / API key needed), verifying the bridge reuses the
score.py engine and writes Harbor's reward contract.
"""

import json
from pathlib import Path

import pytest
import yaml

from agent_eval.config import EvalConfig
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
