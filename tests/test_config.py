"""Config schema parsing tests."""

import sys
from pathlib import Path

import pytest

# Ensure agent_eval is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import DatasetConfig, EvalConfig, JudgeConfig, ModelsConfig
from score import _resolve_judge_model


def _write(tmp_path, body, name="eval.yaml"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def test_execution_block_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
skill: s
execution:
  mode: batch
  arguments: "--in batch.yaml"
  timeout: 1800
  max_budget_usd: 25.5
"""))
    assert cfg.execution.mode == "batch"
    assert cfg.execution.arguments == "--in batch.yaml"
    assert cfg.execution.timeout == 1800
    assert cfg.execution.max_budget_usd == 25.5


def test_runner_block_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
skill: s
runner:
  type: claude-code
  plugin_dirs:
    - /tmp/p
  env:
    FOO: "$FOO"
  settings:
    a: 1
  system_prompt: "be careful"
"""))
    assert cfg.runner.type == "claude-code"
    assert cfg.runner.plugin_dirs == ["/tmp/p"]
    assert cfg.runner.env == {"FOO": "$FOO"}
    assert cfg.runner.settings == {"a": 1}
    assert cfg.runner.system_prompt == "be careful"


def test_runner_type_default(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: t\nskill: s\n"))
    assert cfg.runner.type == "claude-code"


def test_models_block_defaults(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
skill: s
models:
  skill: claude-opus-4-7
  judge: claude-opus-4-7
"""))
    assert cfg.models.skill == "claude-opus-4-7"
    assert cfg.models.subagent is None
    assert cfg.models.judge == "claude-opus-4-7"


def test_mlflow_block_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
skill: s
mlflow:
  experiment: e1
  tracking_uri: sqlite:///x.db
  tags:
    team: ml
"""))
    assert cfg.mlflow.experiment == "e1"
    assert cfg.mlflow.tracking_uri == "sqlite:///x.db"
    assert cfg.mlflow.tags == {"team": "ml"}


def test_mlflow_experiment_defaults_to_name_when_block_present(tmp_path):
    """`mlflow:` block present but no `experiment:` → fall back to eval name."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: my-eval
skill: s
mlflow:
  tracking_uri: sqlite:///x.db
"""))
    assert cfg.mlflow.experiment == "my-eval"


def test_mlflow_disabled_when_block_absent(tmp_path):
    """No `mlflow:` block → experiment empty, MLflow logging off."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: my-eval\nskill: s\n"))
    assert cfg.mlflow.experiment == ""


def test_judge_model_resolution_precedence(tmp_path, monkeypatch):
    """Per-judge `model:` > config.models.judge > EVAL_JUDGE_MODEL > error."""
    cfg = EvalConfig(name="t", skill="s")

    # 1. Per-judge model wins
    jc = JudgeConfig(name="j", model="per-judge-model")
    cfg.models = ModelsConfig(judge="config-judge")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "env-model")
    assert _resolve_judge_model(jc, cfg) == "per-judge-model"

    # 2. config.models.judge used when per-judge unset
    jc = JudgeConfig(name="j")
    assert _resolve_judge_model(jc, cfg) == "config-judge"

    # 3. env var used when both unset
    cfg.models = ModelsConfig()
    assert _resolve_judge_model(jc, cfg) == "env-model"

    # 4. error when nothing set
    monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="No model configured"):
        _resolve_judge_model(jc, cfg)


# --- Path resolution tests (T009) ---

def test_config_dir_set_from_yaml(tmp_path):
    """config_dir is set to the parent of the loaded eval.yaml."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: t\nskill: s\n"))
    assert cfg.config_dir == tmp_path.resolve()


def test_config_dir_subdirectory(tmp_path):
    """config_dir follows the eval.yaml location in subdirectories."""
    sub = tmp_path / "eval" / "my-eval"
    p = _write(tmp_path, "name: t\nskill: s\n",
               name="eval/my-eval/eval.yaml")
    cfg = EvalConfig.from_yaml(p)
    assert cfg.config_dir == sub.resolve()


def test_config_dir_none_fallback():
    """resolve_path falls back to cwd when config_dir is None."""
    cfg = EvalConfig(name="t", skill="s")
    assert cfg.config_dir is None
    resolved = cfg.resolve_path("cases/")
    assert resolved == Path.cwd() / "cases/"


def test_resolve_path_relative(tmp_path):
    """Relative paths resolve against config_dir."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
skill: s
dataset:
  path: cases/
"""))
    resolved = cfg.resolve_path(cfg.dataset.path)
    assert resolved == tmp_path.resolve() / "cases"


def test_resolve_path_absolute(tmp_path):
    """Absolute paths are returned as-is."""
    cfg = EvalConfig(name="t", skill="s", config_dir=tmp_path)
    abs_path = Path("/shared/datasets/common")
    resolved = cfg.resolve_path(abs_path)
    assert resolved == abs_path


def test_absolute_dataset_path_allowed(tmp_path):
    """Absolute dataset.path is accepted by the validator."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
skill: s
dataset:
  path: /shared/datasets/my-cases
"""))
    assert cfg.dataset.path == "/shared/datasets/my-cases"


def test_parent_traversal_rejected(tmp_path):
    """Paths with '..' are rejected."""
    with pytest.raises(ValueError, match="must not contain"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
skill: s
dataset:
  path: ../escape
"""))


def test_dataset_resolves_relative_to_nested_config(tmp_path):
    """dataset.path resolves relative to the config, not cwd."""
    config_dir = tmp_path / "eval" / "my-eval"
    cases_dir = config_dir / "cases"
    cases_dir.mkdir(parents=True)
    p = _write(tmp_path, """
name: t
skill: s
dataset:
  path: cases/
""", name="eval/my-eval/eval.yaml")
    cfg = EvalConfig.from_yaml(p)
    resolved = cfg.resolve_path(cfg.dataset.path)
    assert resolved == cases_dir


def test_shared_dataset_two_configs(tmp_path):
    """Two configs with absolute dataset.path resolve to the same directory."""
    shared = tmp_path / "shared-cases"
    shared.mkdir()
    cfg_a = EvalConfig(name="a", skill="alpha",
                       config_dir=tmp_path / "eval" / "alpha",
                       dataset=DatasetConfig(path=str(shared.resolve())))
    cfg_b = EvalConfig(name="b", skill="beta",
                       config_dir=tmp_path / "eval" / "beta",
                       dataset=DatasetConfig(path=str(shared.resolve())))
    assert cfg_a.resolve_path(cfg_a.dataset.path) == shared.resolve()
    assert cfg_b.resolve_path(cfg_b.dataset.path) == shared.resolve()


def test_absolute_dataset_path_used_as_is(tmp_path):
    """Absolute dataset.path is used directly, ignoring config_dir."""
    abs_path = tmp_path / "global-cases"
    abs_path.mkdir()
    cfg = EvalConfig.from_yaml(_write(tmp_path, f"""
name: t
skill: s
dataset:
  path: {abs_path}
""", name="eval/my-eval/eval.yaml"))
    resolved = cfg.resolve_path(cfg.dataset.path)
    assert resolved == abs_path
