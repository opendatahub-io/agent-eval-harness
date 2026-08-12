"""Tests for Harbor task generation (agent_eval/harbor/tasks.py).

Verifies that dataset cases become self-contained Harbor task packages with a
resolved per-case command, the verifier wiring, and a sanitized bundled config.
"""

import tomllib

import yaml

from agent_eval.config import EvalConfig
from agent_eval.harbor import tasks as gen


def test_resolve_arguments_required_and_optional():
    tmpl = '--headless --dry-run "{prompt}" {priority?}'
    assert gen.resolve_arguments(tmpl, {"prompt": "do X"}) == '--headless --dry-run "do X"'
    assert gen.resolve_arguments(tmpl, {"prompt": "do X", "priority": "--p Critical"}) == \
        '--headless --dry-run "do X" --p Critical'


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
        {"name": "builtin-unknown", "builtin": "quality/example"},
    ]
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    config = EvalConfig.from_yaml(cfg_path)

    gen.generate_tasks(
        config, cfg_path, tmp_path / "tasks", image="img:latest",
        cases=["case-001"], no_llm_judges=True)

    bundled = yaml.safe_load(
        (tmp_path / "tasks" / "case-001" / "tests" / "eval.yaml").read_text())
    assert [judge["name"] for judge in bundled["judges"]] == ["files_exist"]


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
