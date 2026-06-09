"""Tests for the Harbor eval orchestration mapping (agent_eval/harbor/run.py).

Covers build_summary: mapping a parsed Harbor job into the harness summary.yaml
shape (judges aggregated + per_case), which is what makes report.py / regression
/ MLflow consume Harbor runs unchanged.
"""

import yaml

from agent_eval.config import EvalConfig
from agent_eval.harbor import run as run_mod


def _config(tmp_path):
    raw = {
        "name": "t",
        "skill": "rfe.speedrun",
        "dataset": {"path": ""},
        "judges": [
            {"name": "files_exist", "check": "return (True, 'ok')\n"},
            {"name": "rfe_quality", "prompt": "score it"},
        ],
        "thresholds": {"rfe_quality": {"min_mean": 4.0}},
    }
    p = tmp_path / "eval.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return EvalConfig.from_yaml(p)


def _parsed_job():
    return {
        "job_dir": "/x", "mean_reward": 0.75, "n_completed": 2, "n_errored": 0,
        "trials": [
            {"case_id": "case-001", "reward": 1.0, "errored": False, "per_judge": {
                "files_exist": {"value": True, "rationale": "1 file"},
                "rfe_quality": {"value": 5, "rationale": "great"},
            }},
            {"case_id": "case-002", "reward": 0.5, "errored": False, "per_judge": {
                "files_exist": {"value": True, "rationale": "1 file"},
                "rfe_quality": {"value": 3, "rationale": "ok"},
            }},
        ],
    }


def test_judge_types_inference(tmp_path):
    types = run_mod._judge_types(_config(tmp_path))
    assert types == {"files_exist": "check", "rfe_quality": "llm"}


def test_build_summary_aggregates_bool_and_numeric(tmp_path):
    summary = run_mod.build_summary(_parsed_job(), _config(tmp_path))

    # Boolean judge -> pass_rate; numeric judge -> mean.
    assert summary["judges"]["files_exist"]["pass_rate"] == 1.0
    assert summary["judges"]["rfe_quality"]["mean"] == 4.0
    assert summary["judges"]["rfe_quality"]["pass_rate"] is None

    # per_case carries value + rationale + inferred judge_type.
    c1 = summary["per_case"]["case-001"]
    assert c1["files_exist"]["value"] is True
    assert c1["files_exist"]["judge_type"] == "check"
    assert c1["rfe_quality"]["value"] == 5
    assert c1["rfe_quality"]["judge_type"] == "llm"


def test_count_task_packages(tmp_path):
    tasks = tmp_path / "tasks"
    assert run_mod._count_task_packages(tasks) == 0          # missing dir
    (tasks / "case-001").mkdir(parents=True)
    (tasks / "case-001" / "task.toml").write_text("x")
    (tasks / "case-002").mkdir()
    (tasks / "case-002" / "task.toml").write_text("x")
    (tasks / "not-a-task").mkdir()                            # no task.toml
    (tasks / "stray.txt").write_text("x")                    # not a dir
    assert run_mod._count_task_packages(tasks) == 2


def test_build_summary_regression_detectable(tmp_path):
    """The aggregated shape feeds score.detect_regressions correctly."""
    config = _config(tmp_path)
    # Lower rfe_quality below the min_mean=4.0 threshold.
    job = _parsed_job()
    for t in job["trials"]:
        t["per_judge"]["rfe_quality"]["value"] = 2
    summary = run_mod.build_summary(job, config)
    score = run_mod._load_score_module()
    regressions = score.detect_regressions(summary["judges"], config.thresholds)
    assert any(r.judge_name == "rfe_quality" for r in regressions)
