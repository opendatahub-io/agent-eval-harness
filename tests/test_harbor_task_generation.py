"""Tests for Harbor task generation (agent_eval/harbor/tasks.py).

Verifies that dataset cases become self-contained Harbor task packages with a
resolved per-case command, the verifier wiring, and a sanitized bundled config.
"""

import yaml

from agent_eval.config import EvalConfig
from agent_eval.harbor import tasks as gen


def test_resolve_arguments_required_and_optional():
    tmpl = '--headless --dry-run "{prompt}" {priority?}'
    assert gen.resolve_arguments(tmpl, {"prompt": "do X"}) == '--headless --dry-run "do X"'
    assert gen.resolve_arguments(tmpl, {"prompt": "do X", "priority": "--p Critical"}) == \
        '--headless --dry-run "do X" --p Critical'


def _make_eval(tmp_path):
    cases = tmp_path / "cases"
    (cases / "case-001").mkdir(parents=True)
    (cases / "case-001" / "input.yaml").write_text(
        yaml.safe_dump({"prompt": "Verify model signatures at serving", "priority": "Critical"}))
    (cases / "case-002").mkdir(parents=True)
    (cases / "case-002" / "input.yaml").write_text(
        yaml.safe_dump({"prompt": "Autoscaling for Ray clusters", "priority": "Major"}))

    raw = {
        "name": "rfe-speedrun",
        "skill": "rfe.speedrun",
        "execution": {"mode": "batch", "arguments": "--headless --dry-run --input batch.yaml"},
        "dataset": {"path": "cases", "schema": "x"},
        "outputs": [{"path": "artifacts/rfe-tasks", "schema": "rfe files"}],
        "judges": [{"name": "files_exist", "check": "return (True, 'ok')\n"}],
        "models": {"judge": "claude-opus-4-6"},
    }
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

    # instruction.md carries the resolved per-case command + input context
    instr = (t1 / "instruction.md").read_text()
    assert '/rfe.speedrun --headless --dry-run "Verify model signatures at serving"' in instr
    assert "Critical" in instr  # input context embedded

    # test.sh wires the reward bridge against the workdir
    test_sh = (t1 / "tests" / "test.sh").read_text()
    assert "agent_eval.harbor.reward" in test_sh
    assert "/workspace" in test_sh
    assert "/logs/verifier" in test_sh

    # bundled eval.yaml has dataset.path blanked, judges retained
    bundled = yaml.safe_load((t1 / "tests" / "eval.yaml").read_text())
    assert bundled["dataset"]["path"] == ""
    assert bundled["judges"][0]["name"] == "files_exist"


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
