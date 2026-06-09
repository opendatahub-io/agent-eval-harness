"""Tests for parsing a Harbor job directory (agent_eval/harbor/results.py)."""

import json
from pathlib import Path

from agent_eval.harbor import results as R


def _make_trial(job: Path, name: str, reward: float, metrics: dict,
                per_judge: dict | None = None, errored: bool = False):
    tdir = job / name
    (tdir / "verifier").mkdir(parents=True)
    (tdir / "verifier" / "reward.json").write_text(
        json.dumps({"reward": reward, **metrics}))
    if per_judge is not None:
        (tdir / "verifier" / "judges.json").write_text(
            json.dumps({"reward": reward, "per_judge": per_judge}))
    if errored:
        (tdir / "exception.txt").write_text("boom")
    return tdir


def test_parse_trial_strips_id_and_reads_metrics(tmp_path):
    _make_trial(tmp_path, "case-001-foo__abc123", 0.75,
                {"files_exist": 1.0, "rfe_quality": 4.0},
                per_judge={"files_exist": {"value": True, "rationale": "ok"}})
    trial = R.parse_trial(tmp_path / "case-001-foo__abc123")
    assert trial["case_id"] == "case-001-foo"
    assert trial["reward"] == 0.75
    assert trial["metrics"] == {"files_exist": 1.0, "rfe_quality": 4.0}
    assert trial["per_judge"]["files_exist"]["value"] is True
    assert trial["errored"] is False


def test_parse_trial_none_without_reward(tmp_path):
    (tmp_path / "empty").mkdir()
    assert R.parse_trial(tmp_path / "empty") is None


def test_parse_job_aggregates(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    _make_trial(job, "case-001__a", 1.0, {"files_exist": 1.0, "rfe_quality": 5.0})
    _make_trial(job, "case-002__b", 0.0, {"files_exist": 1.0, "rfe_quality": 3.0},
                errored=True)
    # Non-trial dirs/files are ignored.
    (job / "logs").mkdir()
    (job / "result.json").write_text("{}")

    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 2
    assert parsed["n_errored"] == 1
    assert parsed["mean_reward"] == 0.5
    assert parsed["aggregated"]["files_exist"]["mean"] == 1.0
    assert parsed["aggregated"]["rfe_quality"]["mean"] == 4.0
    case_ids = sorted(t["case_id"] for t in parsed["trials"])
    assert case_ids == ["case-001", "case-002"]
