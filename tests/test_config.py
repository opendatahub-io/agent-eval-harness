"""Config schema parsing tests."""

import json
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
execution:
  skill: s
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
execution:
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
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: t\nexecution:\n  skill: s\n"))
    assert cfg.runner.type == "claude-code"


def test_enabled_plugins_wildcard_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
runner:
  settings:
    enabledPlugins:
      "*": false
      memsearch@user-marketplace: true
"""))
    assert cfg.runner.settings["enabledPlugins"]["*"] is False


def test_enabled_plugins_wildcard_rejects_non_boolean(tmp_path):
    with pytest.raises(ValueError, match=r'enabledPlugins."\*" must be a boolean'):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
runner:
  settings:
    enabledPlugins:
      "*": "false"
"""))


def test_models_block_defaults(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
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
execution:
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
execution:
  skill: s
mlflow:
  tracking_uri: sqlite:///x.db
"""))
    assert cfg.mlflow.experiment == "my-eval"


def test_mlflow_disabled_when_block_absent(tmp_path):
    """No `mlflow:` block → experiment empty, MLflow logging off."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: my-eval\nexecution:\n  skill: s\n"))
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


def test_judge_model_backend_validated_at_load(tmp_path):
    """An unroutable judge model fails at config load, naming the source."""
    body = ("name: t\nexecution:\n  skill: s\n"
            "models:\n  judge: gemini:/gemini-2.5-flash\n"
            "judges:\n  - {name: j, prompt: rate it}\n")
    with pytest.raises(ValueError, match=r"models\.judge:.*Unsupported"):
        EvalConfig.from_yaml(_write(tmp_path, body))


def test_unsupported_per_judge_provider_rejected_at_load(tmp_path):
    body = ("name: t\nexecution:\n  skill: s\n"
            "judges:\n  - {name: j, prompt: rate it, model: 'mistral:/mistral-large'}\n")
    with pytest.raises(ValueError, match=r"judge 'j' model:.*Unsupported"):
        EvalConfig.from_yaml(_write(tmp_path, body))


def test_provider_and_runner_prefixed_judge_models_load(tmp_path):
    """openai:/, anthropic:/, runner:/, bare aliases and gateway ids all route."""
    for model in ("openai:/gpt-4o", "anthropic:/claude-sonnet-4-5",
                  "runner:/gpt-5.4-medium", "sonnet", "my-gateway-model"):
        body = (f"name: t\nexecution:\n  skill: s\n"
                f"models:\n  judge: {model}\n"
                f"judges:\n  - {{name: j, prompt: rate it}}\n")
        cfg = EvalConfig.from_yaml(_write(tmp_path, body))
        assert cfg.models.judge == model


def test_agent_judge_unsupported_provider_not_rejected_at_load(tmp_path):
    """Agent judges route through the runner (prefix stripped), so an explicit
    non-SDK provider on them must not be rejected at config load."""
    body = ("name: t\nexecution:\n  skill: s\n"
            "judges:\n  - {name: j, prompt: rate it, model: 'gemini:/x', "
            "agent: {allowed_tools: [Read]}}\n")
    cfg = EvalConfig.from_yaml(_write(tmp_path, body))
    assert cfg.judges[0].model == "gemini:/x"


def test_unsupported_feedback_type_rejected_at_load(tmp_path):
    """Categorical feedback_type is unsupported after the make_judge path was
    removed; it must fail at load rather than be silently graded as numeric."""
    body = ("name: t\nexecution:\n  skill: s\n"
            "judges:\n  - {name: j, prompt: rate it, feedback_type: str}\n")
    with pytest.raises(ValueError, match=r"unsupported feedback_type"):
        EvalConfig.from_yaml(_write(tmp_path, body))


# --- Path resolution tests (T009) ---

def test_config_dir_set_from_yaml(tmp_path):
    """config_dir is set to the parent of the loaded eval.yaml."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: t\nexecution:\n  skill: s\n"))
    assert cfg.config_dir == tmp_path.resolve()


def test_config_dir_subdirectory(tmp_path):
    """config_dir follows the eval.yaml location in subdirectories."""
    sub = tmp_path / "eval" / "my-eval"
    p = _write(tmp_path, "name: t\nexecution:\n  skill: s\n",
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
execution:
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
execution:
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
execution:
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
execution:
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
execution:
  skill: s
dataset:
  path: {abs_path}
""", name="eval/my-eval/eval.yaml"))
    resolved = cfg.resolve_path(cfg.dataset.path)
    assert resolved == abs_path


def test_batch_mode_warns_on_per_case_hooks(tmp_path):
    """Per-case hooks in batch mode emit a warning at config load time."""
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: batch
  skill: s
hooks:
  before_each:
    - command: "echo setup"
  after_each:
    - command: "echo cleanup"
"""))
    assert len(w) == 1
    assert "before_each, after_each" in str(w[0].message)
    assert "batch mode" in str(w[0].message)


def test_case_mode_no_warning_on_per_case_hooks(tmp_path):
    """Per-case hooks in case mode do not emit a warning."""
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: case
  skill: s
hooks:
  before_each:
    - command: "echo setup"
"""))
    assert len(w) == 0


def test_execution_skill_canonical_no_deprecation(tmp_path):
    """execution.skill is the canonical location — no deprecation warning."""
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: case
  skill: rfe.create
"""))
    assert cfg.resolve_skill() == "rfe.create"
    assert cfg.is_prompt_mode() is False
    assert not [x for x in w if issubclass(x.category, DeprecationWarning)]


def test_top_level_skill_deprecated_but_normalized(tmp_path):
    """Top-level skill still works (auto-normalized) but warns."""
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
skill: rfe.create
"""))
    # Normalized into execution.skill and resolvable
    assert cfg.execution.skill == "rfe.create"
    assert cfg.resolve_skill() == "rfe.create"
    # Deprecation warning emitted
    dep = [x for x in w if issubclass(x.category, DeprecationWarning)
           and "Top-level 'skill:'" in str(x.message)]
    assert len(dep) == 1


def test_prompt_mode_resolution(tmp_path):
    """execution.prompt → prompt mode, no skill target."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: case
  prompt: "{{ input.prompt }}"
"""))
    assert cfg.is_prompt_mode() is True
    assert cfg.resolve_skill() is None


# ---------------------------------------------------------------------------
# Agent judge config parsing/validation (specs/010-agent-judge §1, §7)
# ---------------------------------------------------------------------------

from agent_eval.config import RunnerConfig  # noqa: E402


def test_agent_judge_block_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: architecture_score
    prompt_file: eval/prompts/arch.md
    feedback_type: int
    score_range: [0, 2]
    samples: 3
    agent:
      allowed_tools: [Read, Grep, Glob]
      context: [.context/architecture-context]
      inputs: [strat-tasks]
      timeout: 420
      max_budget_usd: 2.0
"""))
    assert len(cfg.judges) == 1
    jc = cfg.judges[0]
    assert jc.name == "architecture_score"
    assert isinstance(jc.agent, dict)
    assert jc.agent["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert jc.agent["context"] == [".context/architecture-context"]
    assert jc.agent["inputs"] == ["strat-tasks"]
    assert jc.agent["timeout"] == 420
    assert jc.agent["max_budget_usd"] == 2.0
    assert jc.samples == 3


def test_agent_block_defaults_to_empty_dict_when_absent(tmp_path):
    """A plain LLM judge (no agent:) has agent == {} (falsy)."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: plain_llm
    prompt: grade it
"""))
    assert cfg.judges[0].agent == {}
    assert not cfg.judges[0].agent


def test_agent_non_dict_raises(tmp_path):
    with pytest.raises(ValueError, match="'agent' must be a mapping"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: bad
    prompt: grade
    agent: "not-a-mapping"
"""))


def test_agent_nested_runner_parses_into_runnerconfig(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: ow_judge
    prompt_file: eval/prompts/arch.md
    agent:
      runner:
        type: cli
        command: "bash run-judge.sh {workspace} {output_dir} {model}"
        effort: high
      context: [.context/architecture-context]
"""))
    jc = cfg.judges[0]
    runner = jc.agent["runner"]
    assert isinstance(runner, RunnerConfig)
    assert runner.type == "cli"
    assert runner.command == "bash run-judge.sh {workspace} {output_dir} {model}"
    assert runner.effort == "high"


def test_agent_nested_runner_type_defaults_to_claude_code(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: j
    prompt: grade
    agent:
      runner:
        effort: medium
"""))
    runner = cfg.judges[0].agent["runner"]
    assert isinstance(runner, RunnerConfig)
    assert runner.type == "claude-code"
    assert runner.effort == "medium"


def test_agent_nested_runner_non_dict_raises(tmp_path):
    with pytest.raises(ValueError, match="agent.runner' must be a mapping"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: bad
    prompt: grade
    agent:
      runner: "claude-code"
"""))


def test_agent_nested_runner_invalid_command_raises(tmp_path):
    """The nested runner is validated by the SAME logic as the top-level
    runner (command must be str or list of str)."""
    with pytest.raises(ValueError, match="command must be a string or list"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: bad
    prompt: grade
    agent:
      runner:
        command: 123
"""))


def test_agent_raw_yaml_not_mutated(tmp_path):
    """Parsing the nested runner shallow-copies so the raw agent dict's runner
    is replaced on the JudgeConfig without leaving a half-parsed structure."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: j
    prompt: grade
    agent:
      runner:
        type: cli
        command: "x.sh"
      inputs: [a]
"""))
    jc = cfg.judges[0]
    # Other agent keys survive alongside the parsed runner.
    assert jc.agent["inputs"] == ["a"]
    assert isinstance(jc.agent["runner"], RunnerConfig)


# ---------------------------------------------------------------------------
# resolve_plugin_dir trust boundary (runtime path used by all runners)
# ---------------------------------------------------------------------------

from agent_eval.config import resolve_plugin_dir  # noqa: E402

_MINIMAL = "name: t\nexecution:\n  skill: s\n"


def _plugin_config(tmp_path, monkeypatch):
    project = tmp_path / "project"
    eval_dir = project / "eval"
    eval_dir.mkdir(parents=True)
    cfg = EvalConfig.from_yaml(_write(eval_dir, _MINIMAL))
    monkeypatch.chdir(project)
    return project, eval_dir, cfg


def test_resolve_plugin_dir_allows_declared_relative_external_path(
        tmp_path, monkeypatch):
    _project, _, cfg = _plugin_config(tmp_path, monkeypatch)
    (tmp_path / "outside-plugin").mkdir()
    assert resolve_plugin_dir(cfg, "../outside-plugin") == (
        tmp_path / "outside-plugin").resolve()


def test_resolve_plugin_dir_rejects_symlink_escape(tmp_path, monkeypatch):
    project, _, cfg = _plugin_config(tmp_path, monkeypatch)
    outside = tmp_path / "outside-plugin"
    outside.mkdir()
    (project / "plugin-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="must not escape"):
        resolve_plugin_dir(cfg, "plugin-link")


def test_resolve_plugin_dir_prefers_project_root_candidate(tmp_path, monkeypatch):
    project, eval_dir, cfg = _plugin_config(tmp_path, monkeypatch)
    (project / "plugins").mkdir()
    (eval_dir / "plugins").mkdir()
    assert resolve_plugin_dir(cfg, "plugins") == (project / "plugins").resolve()


def test_resolve_plugin_dir_never_falls_back_to_config_dir(tmp_path, monkeypatch):
    _project, eval_dir, cfg = _plugin_config(tmp_path, monkeypatch)
    (eval_dir / "plugins").mkdir()
    with pytest.raises(FileNotFoundError, match="plugin directory not found"):
        resolve_plugin_dir(cfg, "plugins")


def test_resolve_plugin_dir_absolute_is_opt_in_but_must_exist(
        tmp_path, monkeypatch):
    _project, _, cfg = _plugin_config(tmp_path, monkeypatch)
    outside = tmp_path / "outside-plugin"
    outside.mkdir()
    assert resolve_plugin_dir(cfg, str(outside)) == outside.resolve()
    with pytest.raises(FileNotFoundError, match="plugin directory not found"):
        resolve_plugin_dir(cfg, str(tmp_path / "missing"))


def test_plugin_manifest_must_be_a_json_object(tmp_path):
    from agent_eval.config import resolve_plugin_skill_roots
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('["skills"]')
    with pytest.raises(ValueError, match="must be a JSON object"):
        resolve_plugin_skill_roots(plugin)


@pytest.mark.parametrize("entry", ["/etc", "../../outside", "escape-link"])
def test_plugin_manifest_skill_roots_cannot_escape_plugin_dir(tmp_path, entry):
    from agent_eval.config import resolve_plugin_skill_roots
    outside = tmp_path / "outside"
    (outside / "leak").mkdir(parents=True)
    (outside / "leak" / "SKILL.md").write_text("leak")
    plugin = tmp_path / "plugins" / "p"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / "escape-link").symlink_to(outside, target_is_directory=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"skills": [entry]}))
    with pytest.raises(ValueError, match="must stay beneath the plugin"):
        resolve_plugin_skill_roots(plugin)


def test_codex_rejects_unenforceable_tool_interception_and_repo_mode(tmp_path):
    with pytest.raises(ValueError, match=r"does not support inputs\.tools"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
runner: {type: codex}
inputs:
  tools:
    - {match: Bash, prompt: mock it}
"""))
    with pytest.raises(ValueError, match="answer-key protections"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
runner: {type: codex, workspace_mode: repo}
"""))


def test_discovery_skips_hidden_files_and_dirs(tmp_path):
    """Hidden entries under eval/ are working files, never configs — a
    git-ignored .entity-map.yaml surfacing as an eval config turns
    single-config auto-selection into a which-config prompt."""
    from agent_eval.config import discover_configs
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "my-skill.yaml").write_text("name: t\nexecution:\n  skill: s\n")
    (eval_dir / ".entity-map.yaml").write_text("SomeCorp: OtherCorp\n")
    hidden_dir = eval_dir / ".raw"
    hidden_dir.mkdir()
    (hidden_dir / "eval.yaml").write_text("name: h\nexecution:\n  skill: x\n")

    found = discover_configs(tmp_path)
    assert [c.path.name for c in found] == ["my-skill.yaml"]

def test_cursor_repo_mode_is_allowed(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  prompt: "{{ input.prompt }}"
runner: {type: cursor, workspace_mode: repo}
"""))

    assert cfg.runner.type == "cursor"
    assert cfg.runner.workspace_mode == "repo"
