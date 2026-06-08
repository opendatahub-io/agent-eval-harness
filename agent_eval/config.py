"""Evaluation suite configuration loaded from eval.yaml files."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union
import sys

import yaml


def _validate_relative_path(
    value: str,
    field_name: str,
    reject_root: bool = False,
    allow_absolute: bool = False,
) -> str:
    """Reject parent-traversing paths (and optionally absolute paths).

    Args:
        reject_root: If True, also reject "." (current directory).
            Used for output paths where "." would mean the project root
            and cleaning it would delete the entire project.
        allow_absolute: If True, allow absolute paths (pass through as-is).
            Used for dataset.path which may be an absolute shared path.
    """
    if not value:
        return value
    p = Path(value)
    if ".." in p.parts:
        raise ValueError(f"{field_name} must not contain '..': {value}")
    if p.is_absolute():
        if not allow_absolute:
            raise ValueError(f"{field_name} must be a relative path: {value}")
        return value
    if reject_root and str(p) == ".":
        raise ValueError(
            f"{field_name} cannot be '.' (project root) — use a subdirectory. "
            f"Outputs must be in a named subdirectory so the harness can "
            f"identify, collect, and clean them without affecting the project."
        )
    return value


@dataclass
class DiscoveryResult:
    """A discovered eval config file."""
    path: Path
    eval_name: str
    is_root: bool


@dataclass
class WorkspaceConfig:
    """Workspace file provisioning for evaluation cases.

    ``files`` is a whitelist of relative paths inside each case directory
    to copy into the agent workspace.  Directory entries copy recursively;
    file entries copy the single file.  Paths not listed are left behind.
    """

    files: list = field(default_factory=list)


@dataclass
class DatasetConfig:
    """Dataset location, schema, and workspace provisioning."""

    path: str = ""
    schema: str = ""
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)


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

    path: str = ""  # File artifacts directory
    tool: str = ""  # Tool call name/pattern to capture
    schema: str = ""
    batch_pattern: str = ""  # Batch collection pattern (empty = auto-detect)
    types: dict = None  # Semantic types for artifacts (filename or glob → type)


@dataclass
class TracesConfig:
    """What execution traces to capture and make available to judges."""

    stdout: bool = True  # Capture stdout.log
    stderr: bool = True  # Capture stderr.log
    events: bool = True  # Parse JSONL into events.json
    metrics: bool = True  # Capture run_result.json metrics


@dataclass
class ToolInputConfig:
    """Handler for intercepting a tool during eval execution.

    The `match` field describes what to intercept in natural language.
    eval-analyze populates this based on skill analysis. eval-run resolves
    it to concrete patterns at workspace setup time.
    """

    match: str = ""  # Natural language: what to intercept (tools, scripts, APIs)
    prompt: str = ""  # Natural language instruction for how to handle
    prompt_file: str = ""  # External file with detailed instructions


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

    env: extra environment variables injected into the runner subprocess.
    Keys are variable names, values are literal strings or ``$VAR``
    references resolved from the caller's environment.  Additive to the
    runner's built-in safe defaults (Claude Code allowlist).
    """

    type: str = "claude-code"
    command: Optional[Union[str, list]] = None  # CLI runner: command template
    settings: dict = field(default_factory=dict)
    plugin_dirs: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
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
    context: list = field(
        default_factory=list
    )  # File paths loaded as supplementary context
    feedback_type: str = ""  # Optional: int, float, bool, str. Inferred if omitted.
    model: str = ""  # Override model for this judge (pairwise, LLM)
    # External code judge
    module: str = ""
    function: str = ""
    # Builtin judge (resolves via BuiltinJudgeRegistry)
    builtin: str = ""
    # Arguments passed as **kwargs to Python judges, Jinja var to LLM judges
    arguments: dict = field(default_factory=dict)
    # Sampling — run this judge N times per case and reduce (median/majority).
    # Only meaningful for stochastic (LLM) judges; ignored for deterministic ones.
    samples: int = 1


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

    # Dataset — location, schema, and workspace file provisioning
    dataset: DatasetConfig = field(default_factory=DatasetConfig)

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

    # Directory containing the eval.yaml that created this config.
    # Used as base for resolving dataset.path. None when constructed
    # programmatically (falls back to Path.cwd()).
    config_dir: Optional[Path] = None

    # Runtime overrides (set by CLI or skill, not config file)
    model: str = ""
    subagent_model: str = ""
    run_id: str = ""
    baseline: str = ""

    def resolve_path(self, relative: Path | str) -> Path:
        """Resolve a path relative to the config file's directory.

        Absolute paths are returned as-is. Relative paths resolve against
        config_dir (falling back to cwd when config_dir is None).
        """
        p = Path(relative)
        if p.is_absolute():
            return p
        base = self.config_dir if self.config_dir is not None else Path.cwd()
        return base / p

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
                isinstance(x, str) for x in command
            )
            if not (isinstance(command, str) or valid_list):
                raise ValueError("runner.command must be a string or list of strings")
        runner = RunnerConfig(
            type=runner_raw.get("type", "claude-code"),
            command=command,
            settings=runner_raw.get("settings", {}) or {},
            plugin_dirs=runner_raw.get("plugin_dirs", []) or [],
            env=runner_raw.get("env", {}) or {},
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

        # Dataset — path, schema, and workspace file provisioning
        ws_raw = dataset.get("workspace", {}) or {}
        ws_files_raw = ws_raw.get("files", []) or []
        ws_files = []
        for i, f in enumerate(ws_files_raw):
            if not isinstance(f, str):
                raise ValueError(
                    f"dataset.workspace.files[{i}] must be a string, got {type(f).__name__}"
                )
            ws_files.append(
                _validate_relative_path(f.rstrip("/"), "dataset.workspace.files")
            )
        dataset_config = DatasetConfig(
            path=_validate_relative_path(
                dataset.get("path", ""), "dataset.path", allow_absolute=True
            ),
            schema=dataset.get("schema", ""),
            workspace=WorkspaceConfig(files=ws_files),
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
            config_dir=path.resolve().parent,
            dataset=dataset_config,
        )

        # Outputs (path or tool)
        for i, o in enumerate(raw.get("outputs", [])):
            config.outputs.append(
                OutputConfig(
                    path=_validate_relative_path(
                        o.get("path", ""), f"outputs[{i}].path", reject_root=True
                    ),
                    tool=o.get("tool", ""),
                    schema=o.get("schema", ""),
                    batch_pattern=o.get("batch_pattern", ""),
                    types=o.get("types") or None,
                )
            )

        # Inputs (tool interception)
        inputs_raw = raw.get("inputs", {})
        for t in inputs_raw.get("tools") or []:
            config.inputs.tools.append(
                ToolInputConfig(
                    match=t.get("match", ""),
                    prompt=t.get("prompt", ""),
                    prompt_file=t.get("prompt_file", ""),
                )
            )

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
            builtin_val = j.get("builtin", "")
            if builtin_val is None:
                builtin_val = ""
            if not isinstance(builtin_val, str):
                raise ValueError(
                    f"Judge '{j.get('name', '')}': 'builtin' must be a string"
                )
            args_val = j.get("arguments")
            if args_val is None:
                args_val = {}
            elif not isinstance(args_val, dict):
                raise ValueError(
                    f"Judge '{j.get('name', '')}': 'arguments' must be a mapping"
                )
            config.judges.append(
                JudgeConfig(
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
                    builtin=builtin_val,
                    arguments=args_val,
                    samples=int(j.get("samples", 1)),
                )
            )

        # Thresholds
        config.thresholds = raw.get("thresholds", {})

        if config.skill and not _is_valid_eval_name(config.skill):
            raise ValueError(
                f"Invalid skill name in {path}: {config.skill!r}")

        return config

    @property
    def project_root(self) -> Path:
        """Project root directory (always CWD, not the eval.yaml location)."""
        return Path.cwd()


def _is_valid_eval_name(name: object) -> bool:
    """Check that an eval name is a valid single path segment."""
    if not isinstance(name, str) or not name:
        return False
    if "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
        return False
    return all(ord(c) >= 32 for c in name)


def discover_configs(project_root: Path) -> list[DiscoveryResult]:
    """Scan the project for eval.yaml files across all supported layouts.

    Scan order: eval/*/eval.yaml (nested), eval/*.yaml (flat), root eval.yaml.
    Files without a ``skill`` field or that fail YAML parsing are skipped.
    Eval names with path separators or control characters are rejected.
    """
    results: list[DiscoveryResult] = []
    seen: set[Path] = set()
    seen_names: dict[str, Path] = {}

    def _try_add(yaml_path: Path, is_root: bool) -> None:
        resolved = yaml_path.resolve()
        if resolved in seen:
            return
        try:
            with open(resolved) as f:
                raw = yaml.safe_load(f) or {}
        except Exception as exc:
            print(f"Warning: skipping {yaml_path}: {exc}", file=sys.stderr)
            return
        if not isinstance(raw, dict) or not raw.get("skill"):
            return
        eval_name = raw["skill"]
        if not _is_valid_eval_name(eval_name):
            print(f"Warning: skipping {yaml_path}: invalid eval name {eval_name!r}",
                  file=sys.stderr)
            return
        if eval_name in seen_names:
            print(f"Warning: duplicate eval name {eval_name!r} in "
                  f"{yaml_path} (already seen in {seen_names[eval_name]})",
                  file=sys.stderr)
        seen_names[eval_name] = resolved
        seen.add(resolved)
        results.append(DiscoveryResult(
            path=resolved,
            eval_name=eval_name,
            is_root=is_root,
        ))

    eval_dir = project_root / "eval"
    if eval_dir.is_dir():
        for subdir in sorted(eval_dir.iterdir()):
            if subdir.is_dir():
                candidate = subdir / "eval.yaml"
                if candidate.is_file():
                    _try_add(candidate, is_root=False)
        for candidate in sorted(eval_dir.glob("*.yaml")):
            if candidate.is_file() and candidate.name != "eval.yaml":
                _try_add(candidate, is_root=False)

    root_config = project_root / "eval.yaml"
    if root_config.is_file():
        _try_add(root_config, is_root=True)

    return sorted(results, key=lambda r: r.path)


def infer_layout(configs: list[DiscoveryResult]) -> str:
    """Infer the project's eval layout from discovery results.

    Returns one of: "nested", "flat", "root", "mixed", "none".
    """
    if not configs:
        return "none"

    has_nested = False
    has_flat = False
    has_root = False

    for c in configs:
        if c.is_root:
            has_root = True
        elif c.path.name == "eval.yaml":
            has_nested = True
        else:
            has_flat = True

    patterns = sum([has_nested, has_flat, has_root])
    if patterns > 1:
        return "mixed"
    if has_nested:
        return "nested"
    if has_flat:
        return "flat"
    return "root"
