"""Evaluation suite configuration loaded from eval.yaml files."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import yaml


def _validate_relative_path(value: str, field_name: str,
                            reject_root: bool = False) -> str:
    """Reject absolute or parent-traversing paths.

    Args:
        reject_root: If True, also reject "." (current directory).
            Used for output paths where "." would mean the project root
            and cleaning it would delete the entire project.
    """
    if not value:
        return value
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"{field_name} must be a relative path without '..': {value}")
    if reject_root and str(p) == ".":
        raise ValueError(
            f"{field_name} cannot be '.' (project root) — use a subdirectory. "
            f"Outputs must be in a named subdirectory so the harness can "
            f"identify, collect, and clean them without affecting the project.")
    return value


@dataclass
class OutputConfig:
    """One output source with a natural language schema.

    Output types (determined by which field is set):
    - path: file artifacts in a directory on disk
    - tool: tool calls to capture from stream-json events

    Batch collection (optional):
    - batch_pattern: maps output files to cases when the skill processes
      all cases in a single invocation.  Uses {n} as a 1-based batch
      index (e.g. "RFE-{n:03d}" → "RFE-001", "RFE-002").  Files whose
      name starts with the expanded prefix are assigned to that case.
      Use "*" for shared directories (copied to every case).
    """
    path: str = ""       # File artifacts directory
    tool: str = ""       # Tool call name/pattern to capture
    schema: str = ""
    batch_pattern: str = ""  # Batch collection pattern (empty = auto-detect)
    types: dict = None   # Semantic types for artifacts (filename or glob → type)


@dataclass
class TracesConfig:
    """What execution traces to capture and make available to judges."""
    stdout: bool = True    # Capture stdout.log
    stderr: bool = True    # Capture stderr.log
    events: bool = True    # Parse JSONL into events.json
    metrics: bool = True   # Capture run_result.json metrics


@dataclass
class ToolInputConfig:
    """Handler for intercepting a tool during eval execution.

    The `match` field describes what to intercept in natural language.
    eval-analyze populates this based on skill analysis. eval-run resolves
    it to concrete patterns at workspace setup time.
    """
    match: str = ""           # Natural language: what to intercept (tools, scripts, APIs)
    prompt: str = ""          # Natural language instruction for how to handle
    prompt_file: str = ""     # External file with detailed instructions


@dataclass
class InputsConfig:
    """Tool interception configuration for headless execution."""
    tools: list = field(default_factory=list)  # List of ToolInputConfig


@dataclass
class ExecutionConfig:
    """How the skill is invoked against test cases.

    Modes:
    - case (default): one skill invocation per case, with case-specific
      arguments resolved from input.yaml fields via {field} placeholders.
    - batch: all cases in one invocation via batch.yaml.

    Arguments template placeholders:
    - {field} → substitutes the value of 'field' from input.yaml
    - {field?} → substitutes if present, omitted if missing

    Constraints:
    - timeout: subprocess wall-clock timeout in seconds (None = harness default).
    - max_budget_usd: per-invocation cost cap (None = no cap).

    Environment:
    - env: extra environment variables injected into each case workspace's
      .claude/settings.json.  Available to both the skill and its hooks.
      Values starting with ``$`` are resolved from the caller's environment
      (e.g., ``$JIRA_TOKEN`` → ``os.environ["JIRA_TOKEN"]``).  Missing
      vars are silently omitted.  Literal values are passed through as-is.
    """
    mode: str = "case"
    arguments: str = ""
    timeout: Optional[int] = None
    max_budget_usd: Optional[float] = None
    parallelism: Optional[int] = None
    env: dict = field(default_factory=dict)


@dataclass
class RunnerConfig:
    """Which agent harness runs the skill, and runner-specific knobs.

    type: discriminator selecting the runner implementation (e.g. claude-code).
    Other fields are runner-specific; unused fields are harmless for runners
    that don't read them.
    """
    type: str = "claude-code"
    command: Optional[Union[str, list]] = None  # CLI runner: command template
    settings: dict = field(default_factory=dict)
    plugin_dirs: list = field(default_factory=list)
    env_strip: list = field(default_factory=list)
    system_prompt: Optional[str] = None
    effort: Optional[str] = None  # Claude Code: low | medium | high | xhigh | max


@dataclass
class MlflowConfig:
    """MLflow logging target.

    experiment: experiment name. Defaults to EvalConfig.name when an
        `mlflow:` block is present but `experiment` is unset. Stays empty
        when the eval.yaml has no `mlflow:` block at all — so MLflow
        tracing/logging is opt-in via the block, not implicit from `name:`.
    tracking_uri: MLflow server URI; if unset, falls back to
        MLFLOW_TRACKING_URI env var.
    tags: tags applied to every run logged for this eval.
    """
    experiment: str = ""
    tracking_uri: Optional[str] = None
    tags: dict = field(default_factory=dict)


@dataclass
class ModelsConfig:
    """Default models for each role.

    Precedence (high to low):
    - skill: CLI --model > models.skill (must resolve to non-empty)
    - subagent: CLI --subagent-model > models.subagent > skill model
    - judge: per-judge JudgeConfig.model > models.judge > EVAL_JUDGE_MODEL
      env var (must resolve to non-empty for LLM judges)
    """
    skill: Optional[str] = None
    subagent: Optional[str] = None
    judge: Optional[str] = None
    hook: Optional[str] = None


@dataclass
class JudgeConfig:
    """Configuration for a single judge.

    Judge types (determined by which fields are set):
    - Inline check: `check` contains a Python snippet
    - LLM judge: `prompt` or `prompt_file` contains evaluation instructions
    - External code: `module` and `function` reference a Python callable
    """
    name: str = ""
    description: str = ""  # What this judge checks (context for LLM judges)
    # Condition — Python expression evaluated against the outputs dict.
    # If it returns False, the judge is skipped for that case (not counted
    # in pass_rate or mean).  Example: "not annotations.get('dedup_is_duplicate')"
    condition: str = ""
    # Inline code check (returns (bool, str))
    check: str = ""
    # LLM judge / pairwise
    prompt: str = ""
    prompt_file: str = ""
    context: list = field(default_factory=list)  # File paths loaded as supplementary context
    feedback_type: str = ""  # Optional: int, float, bool, str. Inferred if omitted.
    model: str = ""  # Override model for this judge (pairwise, LLM)
    # External code judge
    module: str = ""
    function: str = ""


@dataclass
class EvalConfig:
    """Complete evaluation suite configuration.

    Structure is schema-driven: dataset and output structures are described
    in natural language. The harness interprets these descriptions via LLM
    (once, cached) to drive prepare, collect, and score steps.
    """
    name: str = ""
    description: str = ""
    skill: str = ""
    permissions: dict = field(default_factory=dict)

    # Execution — how the skill is invoked (mode, arguments, timeout, budget)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    # Runner — which agent harness + runner-specific config
    runner: RunnerConfig = field(default_factory=RunnerConfig)

    # Models — default models for skill/subagent/judge roles
    models: ModelsConfig = field(default_factory=ModelsConfig)

    # MLflow logging target
    mlflow: MlflowConfig = field(default_factory=MlflowConfig)

    # Dataset — natural language schema + path
    dataset_path: str = ""
    dataset_schema: str = ""
    dataset_input_files_dir: str = "files"

    # Outputs — file artifacts and/or tool calls
    outputs: list = field(default_factory=list)

    # Inputs — tool interception for headless execution
    inputs: InputsConfig = field(default_factory=InputsConfig)

    # Traces — execution metadata to capture
    traces: TracesConfig = field(default_factory=TracesConfig)

    # Judges (inline checks, LLM, pairwise, external code)
    judges: list = field(default_factory=list)

    # Regression thresholds
    thresholds: dict = field(default_factory=dict)

    # Runtime overrides (set by CLI or skill, not config file)
    model: str = ""
    subagent_model: str = ""
    run_id: str = ""
    baseline: str = ""

    @classmethod
    def from_yaml(cls, path: str | Path) -> "EvalConfig":
        """Load config from a YAML file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        # Dataset
        dataset = raw.get("dataset", {})

        # Execution config
        exec_raw = raw.get("execution", {})
        execution = ExecutionConfig(
            mode=exec_raw.get("mode", "case"),
            arguments=exec_raw.get("arguments", ""),
            timeout=exec_raw.get("timeout"),
            max_budget_usd=exec_raw.get("max_budget_usd"),
            parallelism=exec_raw.get("parallelism"),
            env=exec_raw.get("env") or {},
        )

        # Runner config (block form)
        runner_raw = raw.get("runner") or {}
        command = runner_raw.get("command")
        if command is not None:
            valid_list = isinstance(command, list) and all(
                isinstance(x, str) for x in command)
            if not (isinstance(command, str) or valid_list):
                raise ValueError(
                    "runner.command must be a string or list of strings")
        runner = RunnerConfig(
            type=runner_raw.get("type", "claude-code"),
            command=command,
            settings=runner_raw.get("settings", {}) or {},
            plugin_dirs=runner_raw.get("plugin_dirs", []) or [],
            env_strip=runner_raw.get("env_strip", []) or [],
            system_prompt=runner_raw.get("system_prompt"),
            effort=runner_raw.get("effort"),
        )

        # Models block
        models_raw = raw.get("models", {}) or {}
        models = ModelsConfig(
            skill=models_raw.get("skill"),
            subagent=models_raw.get("subagent"),
            judge=models_raw.get("judge"),
            hook=models_raw.get("hook"),
        )

        # MLflow block. Experiment defaults to the eval's top-level
        # `name` only when an `mlflow:` block is present — so omitting
        # the block entirely leaves MLflow off (no accidental experiment
        # creation on shared tracking servers).
        has_mlflow_block = "mlflow" in raw and raw["mlflow"] is not None
        mlflow_raw = raw.get("mlflow") or {}
        if has_mlflow_block:
            experiment = mlflow_raw.get("experiment") or raw.get("name", "")
        else:
            experiment = ""
        mlflow = MlflowConfig(
            experiment=experiment,
            tracking_uri=mlflow_raw.get("tracking_uri"),
            tags=mlflow_raw.get("tags", {}) or {},
        )

        config = cls(
            name=raw.get("name", path.stem),
            description=raw.get("description", ""),
            skill=raw.get("skill", ""),
            permissions=raw.get("permissions", {}),
            execution=execution,
            runner=runner,
            models=models,
            mlflow=mlflow,
            dataset_path=_validate_relative_path(
                dataset.get("path", ""), "dataset.path"),
            dataset_schema=dataset.get("schema", ""),
            dataset_input_files_dir=_validate_relative_path(
                dataset.get("input_files_dir", "files"),
                "dataset.input_files_dir"),
        )

        # Outputs (path or tool)
        for i, o in enumerate(raw.get("outputs", [])):
            config.outputs.append(OutputConfig(
                path=_validate_relative_path(
                    o.get("path", ""), f"outputs[{i}].path",
                    reject_root=True),
                tool=o.get("tool", ""),
                schema=o.get("schema", ""),
                batch_pattern=o.get("batch_pattern", ""),
                types=o.get("types") or None,
            ))

        # Inputs (tool interception)
        inputs_raw = raw.get("inputs", {})
        for t in (inputs_raw.get("tools") or []):
            config.inputs.tools.append(ToolInputConfig(
                match=t.get("match", ""),
                prompt=t.get("prompt", ""),
                prompt_file=t.get("prompt_file", ""),
            ))

        # Traces
        traces = raw.get("traces", {})
        if traces:
            config.traces = TracesConfig(
                stdout=traces.get("stdout", True),
                stderr=traces.get("stderr", True),
                events=traces.get("events", True),
                metrics=traces.get("metrics", True),
            )

        # Judges
        for j in raw.get("judges", []):
            config.judges.append(JudgeConfig(
                name=j.get("name", ""),
                description=j.get("description", ""),
                condition=j.get("if", ""),
                check=j.get("check", ""),
                prompt=j.get("prompt", ""),
                prompt_file=j.get("prompt_file", ""),
                context=j.get("context", []),
                feedback_type=j.get("feedback_type", ""),
                model=j.get("model", ""),
                module=j.get("module", ""),
                function=j.get("function", ""),
            ))

        # Thresholds
        config.thresholds = raw.get("thresholds", {})

        return config

    @property
    def project_root(self) -> Path:
        """Project root (where eval.yaml lives)."""
        return Path.cwd()
