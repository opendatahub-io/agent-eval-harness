"""Tests for Harbor task generation (agent_eval/harbor/tasks.py).

Verifies that dataset cases become self-contained Harbor task packages with a
resolved per-case command, the verifier wiring, and a sanitized bundled config.
"""

import tomllib

import pytest
import yaml

from agent_eval.config import EvalConfig
from agent_eval.harbor import tasks as gen


def test_resolve_arguments_required_and_optional():
    tmpl = '--headless --dry-run "{prompt}" {priority?}'
    assert gen.resolve_arguments(tmpl, {"prompt": "do X"}) == '--headless --dry-run "do X"'
    assert gen.resolve_arguments(tmpl, {"prompt": "do X", "priority": "--p Critical"}) == \
        '--headless --dry-run "do X" --p Critical'


def test_copy_outputs_snippet_quotes_hostile_paths(tmp_path):
    import shlex
    cfg_path, config = _make_eval(tmp_path)
    hostile = 'out dir/$(touch pwned)'
    config.outputs[0].path = hostile

    snippet = gen._copy_outputs_snippet(config, "/workspace")

    src = shlex.quote(f"/workspace/{hostile}")
    dst = shlex.quote(f"/logs/verifier/{hostile}")
    assert snippet == (f'mkdir -p "$(dirname {dst})" && '
                       f'cp -r {src} {dst} 2>/dev/null || true')


def _make_eval(tmp_path, *, name=None, description=None):
    cases = tmp_path / "cases"
    (cases / "case-001").mkdir(parents=True)
    (cases / "case-001" / "input.yaml").write_text(
        yaml.safe_dump({"prompt": "Verify model signatures at serving", "priority": "Critical"}))
    (cases / "case-002").mkdir(parents=True)
    (cases / "case-002" / "input.yaml").write_text(
        yaml.safe_dump({"prompt": "Autoscaling for Ray clusters", "priority": "Major"}))

    raw = {
        "name": "rfe-speedrun",
        "execution": {"skill": "rfe.speedrun", "mode": "batch", "arguments": "--headless --dry-run --input batch.yaml"},
        "dataset": {"path": "cases", "schema": "x"},
        "outputs": [{"path": "artifacts/rfe-tasks", "schema": "rfe files"}],
        "judges": [{"name": "files_exist", "check": "return (True, 'ok')\n"}],
        "models": {"judge": "claude-opus-4-6"},
    }
    if name is not None:
        raw["name"] = name
    if description is not None:
        raw["description"] = description
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return cfg_path, EvalConfig.from_yaml(cfg_path)


def test_generate_tasks_structure_and_command(tmp_path):
    cfg_path, config = _make_eval(tmp_path)
    out = tmp_path / "harbor-tasks"

    tasks = gen.generate_tasks(
        config, cfg_path, out, image="quay.io/test/rfe-task:latest",
        arguments='--headless --dry-run "{prompt}"', skill="rfe.speedrun",
        workdir="/workspace",
    )
    assert len(tasks) == 2
    t1 = out / "case-001"

    # Files present
    for rel in ("task.toml", "instruction.md", "tests/test.sh",
                "tests/eval.yaml", "environment/input.yaml"):
        assert (t1 / rel).is_file(), rel

    # task.toml references the image and is valid-ish
    toml_text = (t1 / "task.toml").read_text()
    assert 'docker_image = "quay.io/test/rfe-task:latest"' in toml_text
    assert 'case_id = "case-001"' in toml_text
    assert tomllib.loads(toml_text)["metadata"]["judge_mode"] == "full"

    # instruction.md carries the resolved per-case command
    instr = (t1 / "instruction.md").read_text()
    assert '/rfe.speedrun --headless --dry-run "Verify model signatures at serving"' in instr

    # test.sh wires the reward bridge against the workdir
    test_sh = (t1 / "tests" / "test.sh").read_text()
    assert "agent_eval.harbor.reward" in test_sh
    assert "/workspace" in test_sh
    assert "/logs/verifier" in test_sh

    # bundled eval.yaml has dataset.path blanked, judges retained
    bundled = yaml.safe_load((t1 / "tests" / "eval.yaml").read_text())
    assert bundled["dataset"]["path"] == ""
    assert bundled["judges"][0]["name"] == "files_exist"


def test_generate_tasks_uses_codex_skill_instruction(tmp_path):
    cfg_path, config = _make_eval(tmp_path)
    config.runner.type = "codex"
    out = tmp_path / "harbor-tasks"

    gen.generate_tasks(
        config, cfg_path, out, image="img:latest",
        arguments='--snapshot-dir /history/{prompt}', skill="ci:payload-analysis",
        cases=["case-001"],
    )

    instruction = (out / "case-001" / "instruction.md").read_text()
    assert "Use the payload-analysis skill with arguments:" in instruction
    assert "/history/Verify model signatures at serving" in instruction
    assert "/ci:payload-analysis" not in instruction

@pytest.mark.parametrize("workdir", [
    "relative/path", "/work$(touch pwned)", "/work;rm -rf /", '/work"dir',
])
def test_generate_tasks_rejects_unsafe_workdir(tmp_path, workdir):
    cfg_path, config = _make_eval(tmp_path)
    with pytest.raises(ValueError, match="workdir must be an absolute path"):
        gen.generate_tasks(config, cfg_path, tmp_path / "tasks",
                           image="img:latest", workdir=workdir)


def test_instruction_includes_runner_system_prompt(tmp_path):
    cfg_path, _ = _make_eval(tmp_path)
    raw = yaml.safe_load(cfg_path.read_text())
    raw["runner"] = {"type": "codex",
                     "system_prompt": "You are a careful engineer.\n"}
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    config = EvalConfig.from_yaml(cfg_path)

    gen.generate_tasks(
        config, cfg_path, tmp_path / "tasks", image="img:latest",
        arguments='"{prompt}"', skill="ci:payload-analysis", cases=["case-001"])

    instruction = (
        tmp_path / "tasks" / "case-001" / "instruction.md").read_text()
    assert instruction.startswith("You are a careful engineer.\n\n")
    assert "Use the payload-analysis skill with arguments:" in instruction


def test_generate_tasks_case_subset(tmp_path):
    cfg_path, config = _make_eval(tmp_path)
    out = tmp_path / "harbor-tasks"
    tasks = gen.generate_tasks(
        config, cfg_path, out, image="img:latest",
        arguments='"{prompt}"', skill="rfe.speedrun", cases=["case-002"],
    )
    assert len(tasks) == 1
    assert tasks[0].name == "case-002"
    assert not (out / "case-001").exists()


def test_generate_tasks_can_drop_model_calling_judges(tmp_path):
    cfg_path, config = _make_eval(tmp_path)
    raw = yaml.safe_load(cfg_path.read_text())
    raw["judges"] += [
        {"name": "quality", "prompt": "score it"},
        # A builtin whose registry kind is "llm" must be dropped with the
        # prompt judges. (An unknown builtin name can no longer reach the
        # bundler: EvalConfig.from_yaml rejects it at load.)
        {"name": "builtin-llm", "builtin": "quality/output_completeness"},
    ]
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    config = EvalConfig.from_yaml(cfg_path)

    gen.generate_tasks(
        config, cfg_path, tmp_path / "tasks", image="img:latest",
        cases=["case-001"], no_llm_judges=True)

    bundled = yaml.safe_load(
        (tmp_path / "tasks" / "case-001" / "tests" / "eval.yaml").read_text())
    assert [judge["name"] for judge in bundled["judges"]] == ["files_exist"]
    task = tomllib.loads(
        (tmp_path / "tasks" / "case-001" / "task.toml").read_text())
    assert task["metadata"]["judge_mode"] == "deterministic-only"


def test_single_step_with_all_llm_judges_is_marked_unjudged(tmp_path):
    cfg_path, _ = _make_eval(tmp_path)
    raw = yaml.safe_load(cfg_path.read_text())
    raw["judges"] = [{"name": "quality", "prompt": "score it"}]
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    config = EvalConfig.from_yaml(cfg_path)

    out = tmp_path / "tasks"
    gen.generate_tasks(
        config, cfg_path, out, image="img:latest", cases=["case-001"],
        no_llm_judges=True)

    test_sh = (out / "case-001" / "tests" / "test.sh").read_text()
    assert '"agent_eval_unjudged": 1' in test_sh
    assert '"reward": 1.0' not in test_sh
    assert not (out / "case-001" / "tests" / "eval.yaml").exists()


def test_no_llm_bundle_rewrites_reward_thresholds_and_keeps_python_builtin(
        tmp_path):
    cfg_path, _ = _make_eval(tmp_path)
    raw = yaml.safe_load(cfg_path.read_text())
    raw["judges"] = [
        {"name": "quality", "prompt": "Rate it"},
        {"name": "det", "check": "true"},
        {"name": "budget", "builtin": "cost_budget"},
    ]
    raw["reward"] = {"judge": "quality"}
    raw["thresholds"] = {
        "quality": {"min_mean": 4}, "det": {"min_pass_rate": 1}}
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    bundled = gen._bundle_eval_config(cfg_path, no_llm_judges=True)
    assert [judge["name"] for judge in bundled["judges"]] == ["det", "budget"]
    assert "reward" not in bundled
    assert bundled["thresholds"] == {"det": {"min_pass_rate": 1}}

    bundled_path = tmp_path / "bundled.yaml"
    bundled_path.write_text(yaml.safe_dump(bundled, sort_keys=False))
    EvalConfig.from_yaml(bundled_path)  # no dangling reward reference


def test_no_llm_bundle_prunes_weighted_reward_references(tmp_path):
    cfg_path, _ = _make_eval(tmp_path)
    raw = yaml.safe_load(cfg_path.read_text())
    raw["judges"] = [
        {"name": "quality", "prompt": "Rate it"},
        {"name": "det", "check": "true"},
    ]
    raw["reward"] = {
        "formula": "weighted", "weights": {"quality": 0.7, "det": 0.3},
        "raw": ["quality", "det"],
    }
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    bundled = gen._bundle_eval_config(cfg_path, no_llm_judges=True)
    assert bundled["reward"]["weights"] == {"det": 0.3}
    assert bundled["reward"]["raw"] == ["det"]


def test_no_llm_bundle_drops_expression_reward_using_removed_judge(tmp_path):
    cfg_path, _ = _make_eval(tmp_path)
    raw = yaml.safe_load(cfg_path.read_text())
    raw["judges"] = [
        {"name": "quality", "prompt": "Rate it"},
        {"name": "det", "check": "true"},
    ]
    raw["reward"] = {"formula": "0.8 * quality + 0.2 * det"}
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    assert "reward" not in gen._bundle_eval_config(
        cfg_path, no_llm_judges=True)


def test_generate_tasks_escapes_multiline_description_for_toml(tmp_path):
    description = 'Line one\nLine "two" uses \\ a path'
    cfg_path, config = _make_eval(tmp_path, description=description)
    out = tmp_path / "harbor-tasks"

    gen.generate_tasks(config, cfg_path, out, image="img:latest")

    task = tomllib.loads((out / "case-001" / "task.toml").read_text())
    assert task["task"]["description"] == description


def test_generate_tasks_preserves_placeholder_text_in_description(tmp_path):
    description = "Use @@IMAGE@@ when documenting the Harbor image"
    cfg_path, config = _make_eval(tmp_path, description=description)
    out = tmp_path / "harbor-tasks"

    gen.generate_tasks(config, cfg_path, out, image="img:latest")

    task = tomllib.loads((out / "case-001" / "task.toml").read_text())
    assert task["task"]["description"] == description
    assert task["environment"]["docker_image"] == "img:latest"


def test_generate_tasks_escapes_special_chars_in_name(tmp_path):
    # The name/eval_name fields share the description's quoting hazard: a name
    # with quotes or newlines must not break the generated TOML either.
    name = 'Acme "Pro"\nrelease'
    cfg_path, config = _make_eval(tmp_path, name=name)
    out = tmp_path / "harbor-tasks"

    gen.generate_tasks(config, cfg_path, out, image="img:latest")

    task = tomllib.loads((out / "case-001" / "task.toml").read_text())
    assert task["task"]["name"] == f"{name}/case-001"
    assert task["metadata"]["eval_name"] == name
    assert task["metadata"]["case_id"] == "case-001"
    # Numeric fields must stay bare (not quoted) so they parse as numbers.
    assert isinstance(task["verifier"]["timeout_sec"], (int, float))
    assert isinstance(task["agent"]["timeout_sec"], (int, float))


def test_generate_tasks_escapes_del_control_char_in_description(tmp_path):
    # U+007F (DEL) is emitted raw by json.dumps but rejected by TOML parsers;
    # _toml_string must escape it so the description still round-trips.
    description = "before\x7fafter"
    cfg_path, config = _make_eval(tmp_path, description=description)
    out = tmp_path / "harbor-tasks"

    gen.generate_tasks(config, cfg_path, out, image="img:latest")

    task = tomllib.loads((out / "case-001" / "task.toml").read_text())
    assert task["task"]["description"] == description


def test_generate_tasks_truncates_long_description_with_ellipsis(tmp_path):
    # task.description is a one-line label: long text is capped to 120 chars and
    # marked with an ellipsis rather than silently dropped. The full text is
    # still preserved in the bundled eval.yaml for judges.
    description = "x" * 200
    cfg_path, config = _make_eval(tmp_path, description=description)
    out = tmp_path / "harbor-tasks"

    gen.generate_tasks(config, cfg_path, out, image="img:latest")

    task = tomllib.loads((out / "case-001" / "task.toml").read_text())
    label = task["task"]["description"]
    assert len(label) == 120
    assert label == "x" * 119 + "…"

    bundled = yaml.safe_load(
        (out / "case-001" / "tests" / "eval.yaml").read_text())
    assert bundled["description"] == description


def test_generate_tasks_writes_non_ascii_description_as_utf8(tmp_path):
    # _toml_string keeps non-ASCII text raw (ensure_ascii=False), so the file
    # must be written as UTF-8 to survive an ASCII/C-locale environment.
    description = "deploy 🚀 café résumé"
    cfg_path, config = _make_eval(tmp_path, description=description)
    out = tmp_path / "harbor-tasks"

    gen.generate_tasks(config, cfg_path, out, image="img:latest")

    text = (out / "case-001" / "task.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["task"]["description"] == description
