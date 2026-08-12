"""Tests for the Harbor eval orchestration mapping (agent_eval/harbor/run.py).

Covers build_summary: mapping a parsed Harbor job into the harness summary.yaml
shape (judges aggregated + per_case), which is what makes report.py / regression
/ MLflow consume Harbor runs unchanged.
"""

import yaml

from agent_eval.config import EvalConfig, OutputConfig
from agent_eval.harbor import run as run_mod


def _config(tmp_path):
    raw = {
        "name": "t",
        "execution": {"skill": "rfe.speedrun"},
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


def test_copy_case_artifacts_uses_configured_output_path(tmp_path):
    trial = tmp_path / "trial"
    source = trial / "verifier" / "output"
    source.mkdir(parents=True)
    (source / "report.html").write_text("<h1>report</h1>")
    config = _config(tmp_path)
    config.outputs = [OutputConfig(path="output")]
    run_dir = tmp_path / "run"

    run_mod._copy_case_artifacts({
        "trials": [{"trial_path": str(trial), "case_id": "case-001"}],
    }, run_dir, config)

    copied = run_dir / "cases" / "case-001" / "artifacts" / "output" / "report.html"
    assert copied.read_text() == "<h1>report</h1>"


def test_parse_bind_mount_defaults_read_only(tmp_path):
    mount = run_mod._parse_bind_mount(f"{tmp_path}:/historical-payload-data")
    assert mount == {
        "type": "bind",
        "source": str(tmp_path.resolve()),
        "target": "/historical-payload-data",
        "read_only": True,
    }


def test_codex_harbor_effort_uses_canonical_field(tmp_path):
    config = _config(tmp_path)
    config.runner.type = "codex"
    config.runner.effort = "xhigh"
    config.runner.settings["model_reasoning_effort"] = "medium"
    assert run_mod._harbor_agent_kwargs(config, "codex") == [
        "reasoning_effort=xhigh"]


def test_resolve_harbor_skill_roots_includes_whole_plugin(tmp_path):
    plugin = tmp_path / "plugin"
    for name in ("parent", "dependency"):
        skill = plugin / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n")
    config = _config(tmp_path)
    config.runner.plugin_dirs = [str(plugin)]

    assert run_mod._resolve_harbor_skill_roots(config) == [
        (plugin / "skills").resolve()]


def test_harbor_agent_env_resolves_references_and_redacts_log(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.execution.env = {
        "DATA_DIR": "/historical-payload-data",
        "TOKEN": "$TEST_ONLY_TOKEN",
        "MISSING": "$TEST_ONLY_MISSING",
    }
    monkeypatch.setenv("TEST_ONLY_TOKEN", "secret-value")
    resolved = run_mod._resolve_harbor_agent_env(config)
    assert resolved == {
        "DATA_DIR": "/historical-payload-data",
        "TOKEN": "secret-value",
    }
    shown = run_mod._display_command([
        "harbor", "run", "--agent-env", "TOKEN=secret-value"])
    assert shown == "harbor run --agent-env TOKEN=<redacted>"


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
