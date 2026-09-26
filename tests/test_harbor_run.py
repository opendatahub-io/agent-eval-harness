"""Tests for the Harbor eval orchestration mapping (agent_eval/harbor/run.py).

Covers build_summary: mapping a parsed Harbor job into the harness summary.yaml
shape (judges aggregated + per_case), which is what makes report.py / regression
/ MLflow consume Harbor runs unchanged.
"""

import json

import pytest
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


def _write_pregenerated_task(tasks_dir, case_id="case-1", *,
                             judge_mode="full", judges=("rfe_quality",)):
    task = tasks_dir / case_id
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        '[metadata]\njudge_mode = ' + json.dumps(judge_mode) + '\n')
    (task / "tests" / "eval.yaml").write_text(yaml.safe_dump({
        "judges": [{"name": name, "check": "return True"}
                   for name in judges],
    }))
    return task


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

    copied = run_dir / "cases" / "case-001" / "output" / "report.html"
    assert copied.read_text() == "<h1>report</h1>"


def test_copy_case_artifacts_matches_local_collect_layout(tmp_path):
    trial = tmp_path / "trial"
    source = trial / "verifier" / "artifacts"
    source.mkdir(parents=True)
    (source / "report.html").write_text("report")
    config = _config(tmp_path)
    config.outputs = [OutputConfig(path="artifacts")]
    run_dir = tmp_path / "run"

    run_mod._copy_case_artifacts({
        "trials": [{"trial_path": str(trial), "case_id": "case-001"}],
    }, run_dir, config)

    assert (run_dir / "cases" / "case-001" / "artifacts" /
            "report.html").read_text() == "report"
    assert not (run_dir / "cases" / "case-001" / "artifacts" /
                "artifacts").exists()


def test_copy_case_artifacts_uses_pipeline_order_not_lexical_order(tmp_path):
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text(yaml.safe_dump({
        "name": "t",
        "execution": {"steps": [
            {"id": "zeta", "skill": "a"},
            {"id": "alpha", "skill": "b"},
        ]},
        "dataset": {"path": ""},
        "outputs": [{"path": "output"}],
    }))
    config = EvalConfig.from_yaml(config_path)
    trial = tmp_path / "trial"
    for step, text in (("zeta", "first"), ("alpha", "second")):
        source = trial / "steps" / step / "verifier" / "output"
        source.mkdir(parents=True)
        (source / "report.txt").write_text(text)

    out = tmp_path / "run"
    run_mod._copy_case_artifacts({
        "trials": [{"trial_path": str(trial), "case_id": "case-1"}]},
        out, config)
    assert (out / "cases" / "case-1" / "output" /
            "report.txt").read_text() == "second"


def test_copy_case_artifacts_replaces_stale_destination_type(tmp_path):
    config = _config(tmp_path)
    config.outputs = [OutputConfig(path="report")]
    trial = tmp_path / "trial"
    source = trial / "verifier" / "report"
    source.parent.mkdir(parents=True)
    source.write_text("fresh")
    out = tmp_path / "run"
    destination = out / "cases" / "case-1" / "report"
    destination.mkdir(parents=True)
    (destination / "stale").write_text("old")

    run_mod._copy_case_artifacts({
        "trials": [{"trial_path": str(trial), "case_id": "case-1"}]},
        out, config)
    assert destination.is_file()
    assert destination.read_text() == "fresh"


def test_parse_bind_mount_defaults_read_only(tmp_path):
    mount = run_mod._parse_bind_mount(f"{tmp_path}:/historical-payload-data")
    assert mount == {
        "type": "bind",
        "source": str(tmp_path.resolve()),
        "target": "/historical-payload-data",
        "read_only": True,
    }


def test_parse_bind_mount_explicit_writable(tmp_path):
    mount = run_mod._parse_bind_mount(f"{tmp_path}:/data:rw")
    assert mount == {
        "type": "bind", "source": str(tmp_path.resolve()), "target": "/data"}


@pytest.mark.parametrize("spec,error", [
    ("", "expected SOURCE"),
    (":/data", "source must not be empty"),
    ("/tmp:", "target must not be empty"),
    ("/tmp:relative", "target must be absolute"),
    ("/tmp:/data:rx", "expected SOURCE"),
    ("/:/data", "host filesystem root"),
    ("/tmp:/", "container filesystem root"),
])
def test_parse_bind_mount_rejects_invalid_specs(spec, error):
    with pytest.raises(ValueError, match=error):
        run_mod._parse_bind_mount(spec)


def test_parse_bind_mount_rejects_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        run_mod._parse_bind_mount(f"{tmp_path / 'missing'}:/data")


def test_codex_harbor_effort_uses_canonical_field(tmp_path):
    config = _config(tmp_path)
    config.runner.type = "codex"
    config.runner.effort = "xhigh"
    config.runner.settings["model_reasoning_effort"] = "medium"
    assert run_mod._harbor_agent_kwargs(config, "codex") == [
        "reasoning_effort=xhigh"]


def test_codex_harbor_effort_accepts_minimal_and_rejects_max(tmp_path):
    config = _config(tmp_path)
    config.runner.effort = "minimal"
    assert run_mod._harbor_agent_kwargs(config, "codex") == [
        "reasoning_effort=minimal"]
    config.runner.effort = "max"
    with pytest.raises(ValueError, match="Invalid Codex effort"):
        run_mod._harbor_agent_kwargs(config, "codex")


def test_claude_code_harbor_effort_is_forwarded(tmp_path):
    # Harbor's stock claude-code agent exposes the reasoning_effort kwarg;
    # a configured effort must reach it rather than being recorded in run
    # metadata while the agent runs at its default.
    config = _config(tmp_path)
    config.runner.effort = "high"
    assert run_mod._harbor_agent_kwargs(config, "claude-code") == [
        "reasoning_effort=high"]
    assert run_mod._harbor_agent_effort(config, "claude-code") == "high"


def test_claude_code_harbor_effort_validates_vocabulary(tmp_path):
    config = _config(tmp_path)
    config.runner.effort = "minimal"  # codex-only value
    with pytest.raises(ValueError, match="Invalid claude-code effort"):
        run_mod._harbor_agent_kwargs(config, "claude-code")
    config.runner.effort = "max"  # claude-only value is accepted here
    assert run_mod._harbor_agent_kwargs(config, "claude-code") == [
        "reasoning_effort=max"]


def test_unknown_harbor_agent_records_no_effort(tmp_path):
    config = _config(tmp_path)
    config.runner.effort = "high"
    assert run_mod._harbor_agent_kwargs(config, "opencode") == []
    assert run_mod._harbor_agent_effort(config, "opencode") is None


def test_resolve_harbor_skill_roots_includes_whole_plugin(tmp_path):
    plugin = tmp_path / "plugin"
    for name in ("parent", "dependency"):
        skill = plugin / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n")
    config = _config(tmp_path)
    config.runner.plugin_dirs = [str(plugin)]

    assert run_mod._resolve_harbor_skill_roots(config, "codex") == [
        (plugin / "skills").resolve()]


def test_resolve_harbor_skill_roots_honors_plugin_manifest(tmp_path):
    plugin = tmp_path / "plugin"
    skill = plugin / "custom-skills" / "parent"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# parent\n")
    manifest = plugin / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"skills": ["custom-skills"]}))
    config = _config(tmp_path)
    config.runner.plugin_dirs = [str(plugin)]

    assert run_mod._resolve_harbor_skill_roots(config, "codex") == [
        (plugin / "custom-skills").resolve()]


def test_codex_records_the_effort_harbor_defaults_to(tmp_path):
    # Harbor's Codex agent declares CliFlag("reasoning_effort", default="high").
    # Forwarding nothing still runs the trial at high, so run metadata must not
    # claim the cell had no effort — /eval-anova would otherwise treat it as a
    # different condition from an explicit effort: high.
    config = _config(tmp_path)
    config.runner.effort = None
    assert run_mod._harbor_agent_kwargs(config, "codex") == []
    assert run_mod._harbor_agent_effort(config, "codex") is None
    assert run_mod._harbor_effective_effort(config, "codex") == "high"

    # claude-code's flag has no default, so there is nothing to record.
    assert run_mod._harbor_effective_effort(config, "claude-code") is None
    assert run_mod._harbor_effective_effort(config, "opencode") is None

    # An explicit effort still wins and is forwarded.
    config.runner.effort = "minimal"
    assert run_mod._harbor_effective_effort(config, "codex") == "minimal"


def test_skill_roots_resolve_for_claude_code_too(tmp_path):
    # skills_dir is a BaseAgent argument in Harbor, not a Codex extra: the
    # stock claude-code agent copies it into $CLAUDE_CONFIG_DIR/skills. A
    # claude-code trial must not run without the skills under test.
    plugin = tmp_path / "plugin"
    skill = plugin / "skills" / "parent"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# parent\n")
    config = _config(tmp_path)
    config.runner.plugin_dirs = [str(plugin)]

    assert run_mod._resolve_harbor_skill_roots(config, "claude-code") == [
        (plugin / "skills").resolve()]


def test_skill_less_plugin_is_fatal_for_codex_only(tmp_path):
    # A Claude plugin may ship only commands/agents/hooks; Codex can consume a
    # plugin *only* through skills, so an empty one is a misconfiguration.
    plugin = tmp_path / "commands-only"
    (plugin / "commands").mkdir(parents=True)
    config = _config(tmp_path)
    config.runner.plugin_dirs = [str(plugin)]

    assert run_mod._resolve_harbor_skill_roots(config, "claude-code") == []
    with pytest.raises((ValueError, FileNotFoundError)):
        run_mod._resolve_harbor_skill_roots(config, "codex")


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

    args, child_env = run_mod._harbor_agent_env_args(config)
    assert "secret-value" not in args
    assert args == [
        "--agent-env", "DATA_DIR=${AGENT_EVAL_HARBOR_AGENT_ENV_0}",
        "--agent-env", "TOKEN=${AGENT_EVAL_HARBOR_AGENT_ENV_1}",
    ]
    assert child_env == {
        "AGENT_EVAL_HARBOR_AGENT_ENV_0": "/historical-payload-data",
        "AGENT_EVAL_HARBOR_AGENT_ENV_1": "secret-value",
    }


def test_harbor_command_keeps_secret_out_of_argv(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.runner.type = "codex"
    config.execution.env = {"JIRA_TOKEN": "$TEST_ONLY_TOKEN"}
    plugin = tmp_path / "plugin"
    (plugin / "skills" / "parent").mkdir(parents=True)
    (plugin / "skills" / "parent" / "SKILL.md").write_text("# parent\n")
    config.runner.plugin_dirs = [str(plugin)]
    config_path = tmp_path / "run-eval.yaml"
    raw = yaml.safe_load((tmp_path / "eval.yaml").read_text())
    raw["runner"] = {
        "type": "codex", "plugin_dirs": [str(plugin)], "effort": "xhigh"}
    raw["execution"]["env"] = {"JIRA_TOKEN": "$TEST_ONLY_TOKEN"}
    config_path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("TEST_ONLY_TOKEN", "argv-redaction-sentinel")
    tasks_dir = tmp_path / "tasks"
    _write_pregenerated_task(tasks_dir)
    captured = {}

    class FakeProcess:
        returncode = 17

        def wait(self):
            return 17

        def send_signal(self, signum):
            pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(run_mod.subprocess, "Popen", fake_popen)
    result = run_mod.run_eval_on_harbor(
        config_path, image=None, model="gpt-5.6-luna",
        output_dir=tmp_path / "out", tasks_dir=tasks_dir,
        jobs_dir=tmp_path / "jobs", harbor_bin="harbor",
        env_import_path=run_mod._ENV_IMPORT_PATHS["podman"],
        mounts=[{
            "type": "bind", "source": str(tmp_path), "target": "/history",
            "read_only": True,
        }],
        cpus=2, memory_mb=1024)

    assert result == 17
    assert "argv-redaction-sentinel" not in " ".join(captured["command"])
    joined = " ".join(captured["command"])
    assert "JIRA_TOKEN=${AGENT_EVAL_HARBOR_AGENT_ENV_0}" in joined
    assert captured["env"]["AGENT_EVAL_HARBOR_AGENT_ENV_0"] \
        == "argv-redaction-sentinel"
    command = captured["command"]
    assert command[command.index("--skill") + 1] == str(
        (plugin / "skills").resolve())
    assert json.loads(command[command.index("--mounts") + 1]) == [{
        "type": "bind", "source": str(tmp_path), "target": "/history",
        "read_only": True,
    }]
    assert command[command.index("--override-cpus") + 1] == "2"
    assert command[command.index("--override-memory-mb") + 1] == "1024"


def test_claude_harbor_does_not_resolve_codex_skill_roots(tmp_path, monkeypatch):
    config_path = tmp_path / "eval.yaml"
    plugin = tmp_path / "commands-only"
    plugin.mkdir()
    config_path.write_text(yaml.safe_dump({
        "name": "t",
        "execution": {"skill": "x"},
        "runner": {"type": "claude-code", "plugin_dirs": [str(plugin)]},
        "dataset": {"path": ""},
    }))
    tasks_dir = tmp_path / "tasks"
    _write_pregenerated_task(tasks_dir, judges=())
    captured = {}

    class FakeProcess:
        returncode = 9

        def wait(self):
            return 9

        def send_signal(self, signum):
            return None

    monkeypatch.setattr(
        run_mod.subprocess, "Popen",
        lambda command, **kwargs: captured.setdefault("command", command)
        and FakeProcess())
    assert run_mod.run_eval_on_harbor(
        config_path, image=None, model="m", output_dir=tmp_path / "out",
        tasks_dir=tasks_dir, jobs_dir=tmp_path / "jobs") == 9
    assert "--skill" not in captured["command"]


def test_mounts_fail_fast_outside_podman(tmp_path):
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(yaml.safe_dump({
        "name": "t", "execution": {"skill": "x"},
        "runner": {"type": "claude-code"}, "dataset": {"path": ""}}))
    tasks_dir = tmp_path / "tasks"
    _write_pregenerated_task(tasks_dir, judges=())
    with pytest.raises(ValueError, match="supported only by the Podman"):
        run_mod.run_eval_on_harbor(
            config_path, image=None, model="m", output_dir=tmp_path / "out",
            tasks_dir=tasks_dir, jobs_dir=tmp_path / "jobs",
            mounts=[{"type": "bind", "source": str(tmp_path),
                     "target": "/data", "read_only": True}],
            env_import_path=run_mod._ENV_IMPORT_PATHS["kubernetes"])


def test_pregenerated_tasks_reject_generation_only_flags(tmp_path):
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(yaml.safe_dump({
        "name": "t", "execution": {"skill": "x"},
        "runner": {"type": "claude-code"}, "dataset": {"path": ""}}))
    tasks_dir = tmp_path / "tasks"
    (tasks_dir / "case-1").mkdir(parents=True)
    (tasks_dir / "case-1" / "task.toml").write_text("x")
    with pytest.raises(ValueError, match="--cases"):
        run_mod.run_eval_on_harbor(
            config_path, image=None, model="m", output_dir=tmp_path / "out",
            tasks_dir=tasks_dir, jobs_dir=tmp_path / "jobs", cases=["case-1"])


def test_pregenerated_deterministic_tasks_rejected_for_full_run(tmp_path):
    config = _config(tmp_path)
    tasks = tmp_path / "tasks"
    _write_pregenerated_task(tasks, judge_mode="deterministic-only", judges=())
    with pytest.raises(ValueError, match="built with --no-llm-judges"):
        run_mod._validate_task_package_reuse(tasks, config)


def _config_det_thresholds(tmp_path):
    raw = {
        "name": "t",
        "execution": {"skill": "rfe.speedrun"},
        "dataset": {"path": ""},
        "judges": [{"name": "files_exist", "check": "return (True, 'ok')\n"}],
        "thresholds": {"files_exist": {"min_pass_rate": 1.0}},
    }
    p = tmp_path / "det-eval.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return EvalConfig.from_yaml(p)


def test_deterministic_only_packages_reusable_for_no_llm_run(tmp_path):
    config = _config_det_thresholds(tmp_path)
    tasks = tmp_path / "tasks"
    _write_pregenerated_task(tasks, judge_mode="deterministic-only",
                             judges=("files_exist",))
    run_mod._validate_task_package_reuse(tasks, config, no_llm_judges=True)


def test_full_packages_rejected_for_no_llm_run(tmp_path):
    config = _config(tmp_path)
    tasks = tmp_path / "tasks"
    _write_pregenerated_task(tasks)
    with pytest.raises(ValueError, match="bundles model judges"):
        run_mod._validate_task_package_reuse(tasks, config, no_llm_judges=True)


def test_pregenerated_task_from_other_eval_rejected(tmp_path):
    config = _config(tmp_path)
    tasks = tmp_path / "tasks"
    task = _write_pregenerated_task(tasks)
    (task / "task.toml").write_text(
        '[metadata]\neval_name = "other"\njudge_mode = "full"\n')
    with pytest.raises(ValueError, match="generated for eval 'other'"):
        run_mod._validate_task_package_reuse(tasks, config)


def test_purge_task_packages_spares_non_package_entries(tmp_path):
    tasks = tmp_path / "tasks"
    _write_pregenerated_task(tasks, case_id="case-old")
    (tasks / "notes.md").write_text("keep me")
    (tasks / "not-a-package").mkdir()

    run_mod._purge_task_packages(tasks)

    assert not (tasks / "case-old").exists()
    assert (tasks / "notes.md").is_file()
    assert (tasks / "not-a-package").is_dir()


def test_legacy_pregenerated_task_missing_thresholded_judge_is_rejected(tmp_path):
    config = _config(tmp_path)
    tasks = tmp_path / "tasks"
    task = _write_pregenerated_task(tasks, judges=())
    (task / "task.toml").write_text('[metadata]\neval_name = "t"\n')
    with pytest.raises(ValueError, match="missing thresholded judge.*rfe_quality"):
        run_mod._validate_task_package_reuse(tasks, config)


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


def test_build_summary_carries_judge_usage(tmp_path):
    """The in-container judge engine records usage per judge call (spec 014);
    build_summary keeps it on the per-case record and aggregates
    summary.judge_usage exactly like the local scorer."""
    job = _parsed_job()
    usage = {"model": "z-ai/glm-5.2", "provider": "Z.AI", "id": "gen-1",
             "prompt_tokens": 100, "completion_tokens": 10, "reasoning_tokens": None,
             "cost_usd": 0.01, "cost_source": "provider-inline"}
    job["trials"][0]["per_judge"]["rfe_quality"]["usage"] = usage
    job["trials"][0]["per_judge"]["rfe_quality"]["tool_choice_mode"] = "required"
    job["trials"][1]["per_judge"]["rfe_quality"]["usage"] = {**usage, "id": "gen-2",
                                                             "cost_usd": 0.02}

    summary = run_mod.build_summary(job, _config(tmp_path))

    assert summary["per_case"]["case-001"]["rfe_quality"]["usage"] == usage
    assert summary["per_case"]["case-001"]["rfe_quality"]["tool_choice_mode"] == "required"
    assert "usage" not in summary["per_case"]["case-001"]["files_exist"]
    assert summary["judge_usage"]["judge_cost_usd"] == pytest.approx(0.03)
    assert summary["judge_usage"]["requests"] == 2
    assert summary["judge_usage"]["by_judge"]["rfe_quality"]["prompt_tokens"] == 200
    assert summary["judge_usage"]["by_model"]["z-ai/glm-5.2"]["requests"] == 2


def test_build_summary_without_usage_has_no_judge_usage(tmp_path):
    summary = run_mod.build_summary(_parsed_job(), _config(tmp_path))
    assert "judge_usage" not in summary


# --- config_chain reuse refusal (spec 014 PR-3a) ------------------------------

def _write_task_with_chain(tasks_dir, chain, case_id="case-1"):
    task = tasks_dir / case_id
    (task / "tests").mkdir(parents=True)
    chain_toml = ("" if chain is None
                  else "config_chain = [" + ", ".join(json.dumps(c) for c in chain) + "]\n")
    (task / "task.toml").write_text(
        '[metadata]\njudge_mode = "full"\n' + chain_toml)
    (task / "tests" / "eval.yaml").write_text(yaml.safe_dump({
        "judges": [{"name": "rfe_quality", "check": "return True"}]}))
    return task


def _overlay_config(tmp_path):
    base = tmp_path / "eval.yaml"
    base.write_text(yaml.safe_dump({
        "name": "t", "execution": {"skill": "rfe.speedrun"}, "dataset": {"path": ""},
        "judges": [{"name": "rfe_quality", "prompt": "score it"}],
        "thresholds": {"rfe_quality": {"min_mean": 4.0}}}, sort_keys=False))
    (tmp_path / "eval-profiles").mkdir()
    profile = tmp_path / "eval-profiles" / "glm.yaml"
    profile.write_text("extends: ../eval.yaml\nmodels:\n  skill: openrouter:/z-ai/glm-5.2\n")
    return EvalConfig.from_yaml(profile)


def test_reuse_accepts_a_matching_chain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _overlay_config(tmp_path)
    tasks = tmp_path / "tasks"
    _write_task_with_chain(tasks, ["eval.yaml", "eval-profiles/glm.yaml"])
    run_mod._validate_task_package_reuse(tasks, config)


def test_reuse_refuses_a_package_from_another_chain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _overlay_config(tmp_path)
    tasks = tmp_path / "tasks"
    _write_task_with_chain(tasks, ["eval.yaml"])
    with pytest.raises(ValueError, match=r"was built from \['eval.yaml'\]"):
        run_mod._validate_task_package_reuse(tasks, config)


def test_reuse_refuses_a_pre_chain_package_for_an_overlay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _overlay_config(tmp_path)
    tasks = tmp_path / "tasks"
    _write_task_with_chain(tasks, None)
    with pytest.raises(ValueError, match="predates config chains"):
        run_mod._validate_task_package_reuse(tasks, config)


def test_reuse_accepts_a_pre_chain_package_for_a_plain_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _config(tmp_path)
    tasks = tmp_path / "tasks"
    _write_task_with_chain(tasks, None)
    run_mod._validate_task_package_reuse(tasks, config)
    # ... but not one built from a profile
    _write_task_with_chain(tasks, ["eval.yaml", "eval-profiles/glm.yaml"], case_id="case-2")
    with pytest.raises(ValueError, match="was built from"):
        run_mod._validate_task_package_reuse(tasks, config)


# --- spec 014: the same plan on Harbor podman -----------------------------------------

def _plan_config(tmp_path, base_url, *, judge=None, api_key_env=None, guardrail=False):
    raw = yaml.safe_load((tmp_path / "eval.yaml").read_text())
    raw["runner"] = {"type": "claude-code"}
    raw["models"] = {"skill": "openrouter:/z-ai/glm-5.2:exacto",
                     "providers": {"openrouter": {"base_url": base_url, "routing": {
                         "models": {"z-ai/glm-5.2": {"order": ["novita"], "allow_fallbacks": False}}}}}}
    if api_key_env:
        raw["models"]["providers"]["openrouter"]["api_key_env"] = api_key_env
    if guardrail:
        raw["models"]["providers"]["openrouter"]["routing"]["enforcement"] = "key-guardrail"
        raw["models"]["providers"]["openrouter"]["budget"] = {"run_usd": 0.5}
    if judge:
        raw["models"]["judge"] = judge
        raw["judges"] = [{"name": "rfe_quality", "prompt": "score it", "model": judge}]
    else:
        raw["judges"] = [{"name": "files_exist", "check": "return (True, 'ok')\n"}]
    raw["thresholds"] = {}
    p = tmp_path / "plan-eval.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return p


class _FastSession:
    """Installed as run_mod.SESSION_FACTORY: the real session, no waiting."""

    def __new__(cls, *a, **kw):
        from agent_eval.providers.openrouter.session import ProviderSession
        kw.update(poll_s=0.01, give_up_s=0.3, settle_s=0.0, key_poll_s=0.0, key_max_s=2.0)
        return ProviderSession(*a, **kw)


def _harbor_plan_run(tmp_path, monkeypatch, *, judge=None, env="podman", returncode=17, fake_env=None,
                     api_key_env=None, guardrail=False):
    from openrouter_fakes import FAKE_KEY, FAKE_MGMT_KEY, FakeOpenRouter, quiet_atexit

    _config(tmp_path)
    fake = FakeOpenRouter()
    base = fake.start()
    config_path = _plan_config(tmp_path, base, judge=judge, api_key_env=api_key_env, guardrail=guardrail)
    quiet_atexit(monkeypatch)
    tasks_dir = tmp_path / "tasks"
    _write_pregenerated_task(tasks_dir, judge_mode="deterministic-only" if judge is None else "full",
                             judges=("files_exist",) if judge is None else ("rfe_quality",))
    captured = {}

    class FakeProcess:
        def __init__(self):
            self.returncode = returncode

        def wait(self):
            return self.returncode

        def send_signal(self, signum):
            pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(run_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(run_mod, "SESSION_FACTORY", _FastSession)
    for k, v in {"OPENROUTER_API_KEY": FAKE_KEY, "OPENROUTER_MANAGEMENT_KEY": FAKE_MGMT_KEY,
                 **({api_key_env: FAKE_KEY} if api_key_env else {}),
                 "CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "p", "CLOUD_ML_REGION": "r",
                 "ANTHROPIC_API_KEY": "sk-ant-host", "ANTHROPIC_AUTH_TOKEN": "host-tok",
                 "ANTHROPIC_BASE_URL": "https://host.example", **(fake_env or {})}.items():
        monkeypatch.setenv(k, v)
    try:
        result = run_mod.run_eval_on_harbor(
            config_path, image=None, model="openrouter:/z-ai/glm-5.2:exacto",
            output_dir=tmp_path / "out", tasks_dir=tasks_dir, jobs_dir=tmp_path / "jobs",
            harbor_bin="harbor", env_import_path=run_mod._ENV_IMPORT_PATHS[env],
            no_llm_judges=judge is None)
    finally:
        fake.stop()
    return result, captured, fake, base


def test_harbor_podman_run_under_a_plan(tmp_path, monkeypatch, capsys):
    from agent_eval.providers.env import MANAGED_ENV_KEYS
    from openrouter_fakes import FAKE_KEY

    result, captured, fake, base = _harbor_plan_run(tmp_path, monkeypatch)
    assert result == 17
    command, env = captured["command"], captured["env"]
    assert command[command.index("-m") + 1] == "z-ai/glm-5.2:exacto"          # bare id
    carriers = {command[i + 1].split("=")[0]: command[i + 1] for i, a in enumerate(command) if a == "--agent-env"}
    assert set(carriers) >= MANAGED_ENV_KEYS - {"ANTHROPIC_CUSTOM_HEADERS"}      # no attribution configured
    assert all(v.endswith("}") and "${AGENT_EVAL_HARBOR_AGENT_ENV_" in v for v in carriers.values())
    assert FAKE_KEY not in " ".join(command)
    values = {command[i + 1].split("=")[0]: env[command[i + 1].split("${")[1].rstrip("}")]
              for i, a in enumerate(command) if a == "--agent-env"}
    assert values["ANTHROPIC_AUTH_TOKEN"] == FAKE_KEY and values["ANTHROPIC_BASE_URL"] == base
    assert values["CLAUDE_CODE_USE_VERTEX"] == "" and values["ANTHROPIC_MODEL"] == "z-ai/glm-5.2:exacto"
    # the child env is scrubbed of every managed key and of both provider key variables
    assert not (set(env) & MANAGED_ENV_KEYS)
    assert "OPENROUTER_API_KEY" not in env and "OPENROUTER_MANAGEMENT_KEY" not in env
    assert "ANTHROPIC_BASE_URL" in env["AGENT_EVAL_PODMAN_PLAN_EXCLUDE"]
    assert (tmp_path / "out" / "provider" / "routing_snapshot.json").exists()      # preflight ran first
    assert "Provider: openrouter direct | model: z-ai/glm-5.2:exacto" in capsys.readouterr().err


def test_harbor_keeps_the_verifier_key_only_for_an_openrouter_judge(tmp_path, monkeypatch):
    from openrouter_fakes import FAKE_KEY

    _, captured, _, _ = _harbor_plan_run(tmp_path, monkeypatch, judge="openrouter:/z-ai/glm-5.2")
    env = captured["env"]
    assert env["OPENROUTER_API_KEY"] == FAKE_KEY and "OPENROUTER_MANAGEMENT_KEY" not in env
    assert "OPENROUTER_API_KEY" not in env["AGENT_EVAL_PODMAN_PLAN_EXCLUDE"]


def test_harbor_custom_key_name_keeps_the_default_out_and_forwards_by_name(tmp_path, monkeypatch):
    from openrouter_fakes import FAKE_KEY

    _, captured, _, _ = _harbor_plan_run(tmp_path, monkeypatch, judge="openrouter:/z-ai/glm-5.2",
                                         api_key_env="MY_OR_KEY", fake_env={"OPENROUTER_API_KEY": "sk-or-unrelated"})
    env = captured["env"]
    assert env["MY_OR_KEY"] == FAKE_KEY and "OPENROUTER_API_KEY" not in env
    assert env["AGENT_EVAL_PODMAN_PLAN_FORWARD"] == "MY_OR_KEY"
    assert "OPENROUTER_API_KEY" in env["AGENT_EVAL_PODMAN_PLAN_EXCLUDE"]


def test_harbor_key_guardrail_carries_the_per_run_key(tmp_path, monkeypatch):
    from openrouter_fakes import FAKE_KEY, FAKE_MGMT_KEY

    result, captured, fake, _ = _harbor_plan_run(tmp_path, monkeypatch, guardrail=True)
    assert result == 17
    command, env = captured["command"], captured["env"]
    per_run_key = next(iter(fake.issued.values()))["key"]
    carrier = next(command[i + 1] for i, a in enumerate(command) if a == "--agent-env"
                   and command[i + 1].startswith("ANTHROPIC_AUTH_TOKEN="))
    assert env[carrier.split("${")[1].rstrip("}")] == per_run_key       # the literal per-run key, not $OPENROUTER_API_KEY
    assert FAKE_KEY not in env.values() and FAKE_MGMT_KEY not in env.values()
    assert "OPENROUTER_MANAGEMENT_KEY" not in env
    assert fake.deletes == list(fake.issued)                             # revoked in the finally
    record = json.loads((tmp_path / "out" / "provider" / "key.json").read_text())
    assert record["revoked_at"] and per_run_key not in json.dumps(record)


def test_harbor_plan_refuses_kubernetes_for_now(tmp_path, monkeypatch):
    from agent_eval.providers.base import ConfigError

    with pytest.raises(ConfigError, match="podman"):
        _harbor_plan_run(tmp_path, monkeypatch, env="kubernetes")


def test_harbor_without_a_plan_forwards_the_host_env_as_before(tmp_path, monkeypatch):
    _config_det_thresholds(tmp_path)
    config_path = tmp_path / "det-eval.yaml"
    tasks_dir = tmp_path / "tasks"
    _write_pregenerated_task(tasks_dir, judge_mode="deterministic-only", judges=("files_exist",))
    captured = {}

    class FakeProcess:
        returncode = 17

        def wait(self):
            return 17

        def send_signal(self, signum):
            pass

    def fake_popen(command, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(run_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    run_mod.run_eval_on_harbor(config_path, image=None, model="claude-sonnet-4-5",
                               output_dir=tmp_path / "out", tasks_dir=tasks_dir, jobs_dir=tmp_path / "jobs",
                               harbor_bin="harbor", env_import_path=run_mod._ENV_IMPORT_PATHS["podman"],
                               no_llm_judges=True)
    assert captured["env"]["CLAUDE_CODE_USE_VERTEX"] == "1" and "AGENT_EVAL_PODMAN_PLAN_EXCLUDE" not in captured["env"]

