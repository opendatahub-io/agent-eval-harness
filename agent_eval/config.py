"""Evaluation suite configuration loaded from eval.yaml files."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union
import sys

import yaml


def resolve_arguments(template: str, input_data: dict) -> str:
    """Resolve a skill/prompt argument template against input.yaml data.

    Two mutually-exclusive placeholder styles are auto-detected:

    - Jinja2 (``{{ input.field }}`` / ``{% ... %}``): rendered with ``input``
      bound to the case data.  Uses ``StrictUndefined`` so a missing required
      field raises ``ValueError`` rather than silently rendering empty.  For
      genuinely optional fields use ``{{ input.get('field', '') }}`` or the
      ``| default('')`` filter.
    - Brace (``{field}`` / ``{field?}``): ``{field}`` is required (raises
      ``KeyError`` if missing); ``{field?}`` is optional (omitted if missing).
    """
    if not template:
        return ""

    if "{{" in template or "{%" in template:
        from jinja2 import StrictUndefined, Template
        from jinja2 import UndefinedError

        try:
            result = Template(template, undefined=StrictUndefined).render(
                input=input_data
            )
        except UndefinedError as e:
            raise ValueError(
                f"Missing required field in template: {e}. Template: {template}"
            ) from e
        return re.sub(r"[ \t]+", " ", result).strip()

    def _replacer(match):
        f = match.group(1)
        optional = f.endswith("?")
        if optional:
            f = f[:-1]
        value = input_data.get(f)
        if value is None:
            if optional:
                return ""
            raise KeyError(f"Required field '{f}' not found in input.yaml")
        return str(value)

    result = re.sub(r"\{([^}]+)\}", _replacer, template)
    return re.sub(r"[ \t]+", " ", result).strip()


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


def _validate_path_segment(value: str, name: str) -> str:
    """Validate that a value is a single path segment (no directory traversal).

    Ensures the value contains no path separators (/ or \\), is not a
    relative directory reference (. or ..), and contains no control characters.
    Used to prevent path traversal attacks (CWE-22) when constructing
    filesystem paths from user-controlled input.

    Args:
        value: The path segment to validate (e.g., run_id, skill name)
        name: Parameter name for error messages

    Returns:
        The validated value

    Raises:
        ValueError: If value is not a valid single path segment
    """
    if not _is_valid_eval_name(value):
        # Provide detailed error message based on what failed
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string, got: {value!r}")
        if "/" in value or "\\" in value:
            raise ValueError(
                f"{name} must be a single path segment, "
                f"cannot contain path separators: {value!r}"
            )
        if value in (".", ".."):
            raise ValueError(
                f"{name} cannot be a relative directory reference: {value!r}"
            )
        # Control characters or other invalid chars
        raise ValueError(f"{name} contains invalid characters: {value!r}")
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
class HookEntry:
    """A single lifecycle hook command."""
    command: str = ""
    timeout: int = 120
    description: str = ""
    on_failure: str = "fail"  # "fail" | "continue"
    condition: str = ""


@dataclass
class HooksConfig:
    """Lifecycle hooks that run at defined points in the eval pipeline."""
    before_all: list = field(default_factory=list)
    before_each: list = field(default_factory=list)
    after_each: list = field(default_factory=list)
    before_scoring: list = field(default_factory=list)
    after_all: list = field(default_factory=list)


@dataclass
class ExecutionConfig:
    """How the eval target is invoked against test cases.

    Modes (orthogonal to skill/prompt):
    - case (default): one invocation per test case, with case-specific
      arguments resolved from input.yaml fields via {field} placeholders.
    - batch: all cases in one invocation via batch.yaml.

    What to execute (mutually exclusive):
    - skill: skill name to invoke (e.g., 'rfe.create'). Pairs with arguments.
    - prompt: direct prompt template (e.g., '{{ input.prompt }}'). No skill wrapper.

    Examples:
    - Skill mode (case): skill: 'rfe.create', arguments: '--priority {{ input.priority }}'
    - Skill mode (batch): skill: 'rfe.speedrun', arguments: '--input batch.yaml'
    - Prompt mode (case): prompt: '{{ input.prompt }}', arguments: ''
    - Prompt mode (batch): prompt: '{{ input.prompt }}', arguments: '' (uncommon)

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
    skill: str = ""       # Skill name for skill mode (mutually exclusive with prompt)
    prompt: str = ""      # Prompt template for prompt mode (mutually exclusive with skill)
    arguments: str = ""
    timeout: Optional[int] = None
    max_budget_usd: Optional[float] = None
    parallelism: Optional[int] = None
    env: dict = field(default_factory=dict)

    def __post_init__(self):
        # Validate mode
        valid_modes = ["case", "batch"]
        if self.mode not in valid_modes:
            raise ValueError(
                f"execution.mode must be one of {valid_modes}, got: {self.mode}"
            )

        # Validate skill/prompt mutual exclusivity
        has_skill = bool(self.skill and self.skill.strip())
        has_prompt = bool(self.prompt and self.prompt.strip())

        if has_skill and has_prompt:
            raise ValueError(
                "execution.skill and execution.prompt are mutually exclusive. "
                "Use skill for '/skill-name' invocations or prompt for direct prompts."
            )



@dataclass
class RunnerConfig:
    """Which agent harness runs the skill, and runner-specific knobs.

    type: discriminator selecting the runner implementation (e.g. claude-code).
    workspace_mode: execution context (repo = run in repository, default = isolated workspace).
    Other fields are runner-specific; unused fields are harmless for runners
    that don't read them.

    env: extra environment variables injected into the runner subprocess.
    Keys are variable names, values are literal strings or ``$VAR``
    references resolved from the caller's environment.  Additive to the
    runner's built-in safe defaults (Claude Code allowlist).
    """

    type: str = "claude-code"
    command: Optional[Union[str, list]] = None  # CLI runner: command template
    workspace_mode: Optional[str] = None  # repo | None (default: isolated workspace)
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
class GenerationSeed:
    """One seed in a synthetic ``generation`` block.

    Each seed produces ``count`` test cases of a given ``category`` from a
    generation prompt. The prompt is chosen by exactly one discriminator
    (mirroring judges):

    - ``builtin`` — a builtin generation prompt, e.g. ``docs/navigation``
      (from ``agent_eval/prompts/``)
    - ``prompt_file`` — a project file path, relative to the eval config
    - ``prompt`` — an inline prompt string

    ``category`` is stamped onto every generated case as ``annotations.category``.
    """
    category: str
    count: int
    builtin: str = ""
    prompt_file: str = ""
    prompt: str = ""
    description: str = ""


#: Valid ``generation.strategy`` values (case provenance).
GENERATION_STRATEGIES = ("skill", "synthetic", "from-traces")


@dataclass
class GenerationConfig:
    """Test-case generation provenance (how ``/eval-dataset`` sources cases).

    ``strategy`` selects the source: ``skill`` (agent authors from skill
    analysis — the default), ``synthetic`` (LLM generates from ``seeds`` +
    ``context``), or ``from-traces`` (extracted from MLflow production traces).
    ``seeds`` and ``context`` apply only to ``synthetic``.
    """
    strategy: str = "skill"
    context: Union[str, dict] = field(default_factory=dict)
    seeds: list = field(default_factory=list)  # List of GenerationSeed


@dataclass
class JudgeConfig:
    """Configuration for a single judge.

    Judge types (determined by which fields are set):
    - Inline check: `check` contains a Python snippet
    - LLM judge: `prompt`, `prompt_file`, or `llm_rubric` contains evaluation instructions
    - External code: `module` and `function` reference a Python callable
    - Builtin: `builtin` references a registered judge from agent_eval/judges/

    LLM judge fields (all compile to same internal prompt before rendering):

    Priority order: llm_rubric > prompt > prompt_file

    1. llm_rubric — Syntactic sugar for simple evaluation criteria.
       Automatically appends "{{ conversation }}" template if not present.
       Use for concise, criteria-focused judges in synthetic-generation configs.
       Example: llm_rubric: "Agent cited relevant documentation sources"

    2. prompt — Full Jinja2 template with manual control over structure.
       Use when you need multiple placeholders or complex prompt logic.
       Use {{ conversation }} for response quality, {{ tool_trace }} for behavior (navigation, tool usage).
       Example: prompt: "{{ description }}\n\nCase: {{ outputs.case_id }}\n\n{{ conversation }}"

    3. prompt_file — External file path (absolute or relative to project root).
       Use for sharing prompts across multiple judges or configs.
       File can contain either rubric-style (auto-wrapped) or full template.

    All three compile to the same internal prompt variable: llm_rubric gets
    wrapped, prompt_file gets loaded, then Jinja2 renders with case data.
    """

    name: str = ""
    description: str = ""  # What this judge checks (context for LLM judges)
    # Condition — Python expression evaluated against the outputs dict.
    # If it returns False, the judge is skipped for that case (not counted
    # in pass_rate or mean).  Example: "not annotations.get('dedup_is_duplicate')"
    condition: str = ""
    # Inline code check (returns (bool, str))
    check: str = ""
    # LLM judge fields (see docstring above for equivalence and priority)
    prompt: str = ""
    prompt_file: str = ""
    llm_rubric: str = ""
    context: list = field(
        default_factory=list
    )  # File paths loaded as supplementary context
    feedback_type: str = ""  # Optional: int, float, bool, str. Inferred if omitted.
    # Numeric scale [lo, hi] for this judge's value. Used by the report to
    # color per-cell bands proportionally, and can be honored by any consumer
    # that needs to normalize this judge's raw value. If omitted, LLM judges
    # default to [1, 5] and other numeric judges to [0, 1]. Set explicitly for
    # judges on a non-default range (e.g. 1-10, 0-100). Independent of
    # `reward.score_range`, which governs the reward composition normalization.
    score_range: Optional[list] = None
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
class RewardConfig:
    """Reward composition from judge results for RL training.

    Two ways to produce the reward, mutually exclusive:

    1. ``judge``: a single judge whose value IS the reward. By default the
       value is used as-is, clamped to [0, 1] (for a judge that already emits
       a [0, 1] reward, e.g. a learned reward model). Set ``normalize: true``
       to instead map it from ``score_range`` to [0, 1].
    2. ``formula`` (+ ``weights``): compose from multiple judges —
       - "weighted": weighted sum of ``weights``, each normalized via
         ``score_range`` (or clamped if listed in ``raw``).
       - "<expression>": Python expression with judge names as variables.

    When gate is True, any boolean judge that returned False zeros the reward.
    Note this gates on *every* boolean judge, independent of whether the
    formula references it — so an ``<expression>`` that uses booleans as its
    own gate (e.g. ``passed * score``) usually wants ``gate: false`` to avoid
    double-gating. ``gate`` defaults to False in ``judge`` mode.
    score_range normalizes numeric judge scores to [0, 1].
    raw: list of judge names whose values are already in [0, 1] and should
         NOT be normalized via score_range (e.g. efficiency).
    """

    formula: str = "weighted"
    weights: dict = field(default_factory=dict)
    gate: bool = True
    score_range: list = field(default_factory=lambda: [1, 5])
    raw: list = field(default_factory=list)
    # Single-judge mode: name of the judge whose value is the reward.
    judge: Optional[str] = None
    # In judge mode, map the value from score_range instead of clamping as-is.
    normalize: bool = False


@dataclass
class EvalConfig:
    """Complete evaluation suite configuration.

    Structure is schema-driven: dataset and output structures are described
    in natural language. The harness interprets these descriptions via LLM
    (once, cached) to drive prepare, collect, and score steps.
    """

    name: str = ""
    description: str = ""
    skill: Optional[str] = None  # Deprecated: use execution.skill instead. Fallback for backward compat.
    permissions: dict = field(default_factory=dict)

    # Lifecycle hooks — shell commands at defined pipeline points
    hooks: HooksConfig = field(default_factory=HooksConfig)

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

    # Generation — synthetic test-case generation (optional, prompt-mode)
    generation: GenerationConfig = field(default_factory=GenerationConfig)

    # Outputs — file artifacts and/or tool calls
    outputs: list = field(default_factory=list)

    # Inputs — tool interception for headless execution
    inputs: InputsConfig = field(default_factory=InputsConfig)

    # Traces — execution metadata to capture
    traces: TracesConfig = field(default_factory=TracesConfig)

    # Judges (inline checks, LLM, pairwise, external code)
    judges: list = field(default_factory=list)

    # Reward composition for RL training (optional)
    reward: Optional[RewardConfig] = None

    # Regression thresholds
    thresholds: dict = field(default_factory=dict)

    # Directory containing the eval.yaml that created this config.
    # Used as base for resolving dataset.path. None when constructed
    # programmatically (falls back to Path.cwd()).
    config_dir: Optional[Path] = None

    # Full path to the eval.yaml file (for eval_name derivation).
    # None when constructed programmatically.
    config_path: Optional[Path] = None

    # Runtime overrides (set by CLI or skill, not config file)
    model: str = ""
    subagent_model: str = ""
    run_id: str = ""
    baseline: str = ""

    def __post_init__(self):
        if self.skill and not self.execution.skill:
            self.execution.skill = self.skill

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

    def resolve_skill(self) -> Optional[str]:
        """Canonical skill name for skill mode, or None for prompt mode.

        Prefers ``execution.skill`` (the current location) and falls back to
        the deprecated top-level ``skill`` field.  Returns None when neither
        is set — i.e. prompt mode or an unconfigured target.  All execution
        substrates (local, Harbor, EvalHub) MUST resolve the target through
        this method so a config authored with only ``execution.skill`` runs
        the skill instead of silently degrading to prompt mode.
        """
        return self.execution.skill or self.skill or None

    def is_prompt_mode(self) -> bool:
        """True when the eval runs a direct prompt (no skill wrapper)."""
        return bool(self.execution.prompt and self.execution.prompt.strip())

    def eval_name(self) -> str:
        """Derive eval identifier with backward-compatible fallback chain.

        Priority order (backward-compatible with existing skill evals):
        1. skill field - preserves existing skill-based eval runs
        2. name field - allows explicit naming for prompt-mode evals
        3. directory/filename - pure path-based derivation
        4. "eval" - final fallback

        This ensures existing skill evals continue to work while enabling
        prompt mode to use either explicit names or path-based identifiers.
        """
        # Priority 1: skill field (backward compat with existing evals).
        # Resolve through resolve_skill() so execution.skill-only configs
        # still name the run after the skill under test.
        skill = self.resolve_skill()
        if skill:
            return skill

        # Priority 2: name field (explicit identifier, sanitized)
        # Skip if name == path.stem (auto-set default from from_yaml)
        if self.name and not (self.config_path and self.name == self.config_path.stem):
            # Sanitize: convert spaces to hyphens, keep only safe chars
            sanitized = self.name.lower().replace(" ", "-")
            sanitized = "".join(c for c in sanitized if c.isalnum() or c in "._-")
            if sanitized and _is_valid_eval_name(sanitized):
                return sanitized

        # Priority 3: derive from path (new behavior for prompt mode)
        if self.config_path:
            if self.config_path.name == "eval.yaml":
                # Nested: eval/user-guides/eval.yaml → "user-guides"
                # Check if grandparent directory is named "eval"
                if self.config_path.parent.parent.name == "eval":
                    return self.config_path.parent.name
                # Root: eval.yaml at project root → "eval"
                else:
                    return "eval"
            # Flat: eval/user-guides.yaml → "user-guides"
            else:
                return self.config_path.stem

        # Final fallback
        return "eval"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "EvalConfig":
        """Load config from a YAML file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        # Deprecation: top-level `skill:` is auto-normalized into
        # execution.skill (below) but the canonical home is the execution
        # block, symmetric with execution.prompt. Warn once per load; only
        # for a non-empty value that isn't already mirrored in execution.
        exec_raw = raw.get("execution", {})
        if raw.get("skill") and not (exec_raw.get("skill") or "").strip():
            import warnings
            warnings.warn(
                f"Top-level 'skill:' in {path} is deprecated; move it under "
                "execution.skill (it is auto-normalized for now and will be "
                "removed in a future release).",
                DeprecationWarning,
                stacklevel=2,
            )

        # Dataset
        dataset = raw.get("dataset", {})

        # Execution config
        execution = ExecutionConfig(
            mode=exec_raw.get("mode", "case"),
            skill=exec_raw.get("skill", "") or raw.get("skill", ""),
            prompt=exec_raw.get("prompt", ""),
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
        # Validate workspace_mode (prevent typos that silently change behavior)
        workspace_mode = runner_raw.get("workspace_mode")
        if workspace_mode is not None and workspace_mode not in ("repo",):
            raise ValueError(
                f"runner.workspace_mode must be None or 'repo', got: {workspace_mode!r}")

        runner = RunnerConfig(
            type=runner_raw.get("type", "claude-code"),
            command=command,
            workspace_mode=workspace_mode,
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
        # Generation — synthetic test-case generation (optional) with validation
        gen_raw = raw.get("generation") or {}
        seeds = []
        for i, s in enumerate(gen_raw.get("seeds") or []):
            category = s.get("category", "")
            count = s.get("count")
            if not category or not isinstance(category, str):
                raise ValueError(
                    f"generation.seeds[{i}].category must be a non-empty string, got: {category!r}")
            # count is required — a silent default would swallow a mistyped field name
            if not isinstance(count, int) or count < 1:
                raise ValueError(
                    f"generation.seeds[{i}].count must be an integer >= 1, got: {count!r}")

            # Exactly one prompt discriminator (mirrors judges: builtin/prompt_file/prompt)
            discriminators = [
                k for k in ("builtin", "prompt_file", "prompt") if s.get(k)
            ]
            if len(discriminators) != 1:
                raise ValueError(
                    f"generation.seeds[{i}] ('{category}') must set exactly one of "
                    f"builtin / prompt_file / prompt, got: {discriminators or 'none'}")

            seeds.append(GenerationSeed(
                category=category,
                count=count,
                builtin=s.get("builtin", ""),
                prompt_file=s.get("prompt_file", ""),
                prompt=s.get("prompt", ""),
                description=s.get("description", ""),
            ))

        # Provenance: absent normalizes to 'skill' (the default source).
        strategy = gen_raw.get("strategy") or "skill"
        if strategy not in GENERATION_STRATEGIES:
            raise ValueError(
                f"generation.strategy must be one of "
                f"{', '.join(GENERATION_STRATEGIES)}, got: {strategy!r}")
        if strategy == "synthetic" and not seeds:
            raise ValueError(
                "generation.strategy is 'synthetic' but generation.seeds is empty.")
        if seeds and strategy != "synthetic":
            raise ValueError(
                f"generation.seeds are only valid with strategy: synthetic "
                f"(got strategy: {strategy}).")

        generation_config = GenerationConfig(
            strategy=strategy,
            context=gen_raw.get("context", {}),
            seeds=seeds,
        )

        config = cls(
            name=raw.get("name", path.stem),
            description=raw.get("description", ""),
            skill=raw.get("skill") or None,  # Convert empty string to None
            permissions=raw.get("permissions", {}),
            execution=execution,
            runner=runner,
            models=models,
            mlflow=mlflow,
            config_dir=path.resolve().parent,
            config_path=path.resolve(),
            dataset=dataset_config,
            generation=generation_config,
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
            score_range_val = j.get("score_range")
            if score_range_val is not None:
                jname = j.get("name", "")
                if (not isinstance(score_range_val, list)
                        or len(score_range_val) != 2):
                    raise ValueError(
                        f"Judge '{jname}': 'score_range' must be a [min, max] list")
                try:
                    lo, hi = float(score_range_val[0]), float(score_range_val[1])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Judge '{jname}': 'score_range' values must be numeric") from exc
                if lo >= hi:
                    raise ValueError(
                        f"Judge '{jname}': 'score_range' must be increasing [min, max]")
                score_range_val = [lo, hi]
            config.judges.append(
                JudgeConfig(
                    name=j.get("name", ""),
                    description=j.get("description", ""),
                    condition=j.get("if", ""),
                    check=j.get("check", ""),
                    prompt=j.get("prompt", ""),
                    prompt_file=j.get("prompt_file", ""),
                    llm_rubric=j.get("llm_rubric", ""),
                    context=j.get("context", []),
                    feedback_type=j.get("feedback_type", ""),
                    score_range=score_range_val,
                    model=j.get("model", ""),
                    module=j.get("module", ""),
                    function=j.get("function", ""),
                    builtin=builtin_val,
                    arguments=args_val,
                    samples=int(j.get("samples", 1)),
                )
            )

        # Reward composition
        if "reward" in raw:
            reward_raw = raw.get("reward")
            if not isinstance(reward_raw, dict):
                raise ValueError("reward must be a mapping when provided")
            sr = reward_raw.get("score_range", [1, 5])
            if not isinstance(sr, list) or len(sr) != 2:
                raise ValueError("reward.score_range must be a [min, max] list")
            try:
                score_min = float(sr[0])
                score_max = float(sr[1])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "reward.score_range values must be numeric") from exc
            if not score_min < score_max:
                raise ValueError(
                    "reward.score_range must be increasing [min, max]")
            weights = reward_raw.get("weights", {}) or {}
            if not isinstance(weights, dict):
                raise ValueError("reward.weights must be a mapping")
            try:
                weights = {str(k): float(v) for k, v in weights.items()}
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "reward.weights values must be numeric") from exc
            if any(v < 0 for v in weights.values()):
                raise ValueError("reward.weights values must be non-negative")
            raw_list = reward_raw.get("raw", []) or []
            if not isinstance(raw_list, list):
                raw_list = [raw_list]
            # Single-judge mode: one judge's value is the reward. Mutually
            # exclusive with the composition inputs.
            judge = reward_raw.get("judge")
            if judge is not None:
                if not isinstance(judge, str) or not judge.strip():
                    raise ValueError(
                        "reward.judge must be a non-empty judge name")
                conflicting = [k for k in ("formula", "weights", "raw")
                               if k in reward_raw]
                if conflicting:
                    raise ValueError(
                        "reward.judge cannot be combined with "
                        f"{'/'.join(conflicting)}")
                judge_names = {j.name for j in config.judges if j.name}
                if judge not in judge_names:
                    raise ValueError(
                        f"reward.judge '{judge}' does not match any defined "
                        "judge")
            normalize = reward_raw.get("normalize", False)
            if not isinstance(normalize, bool):
                raise ValueError("reward.normalize must be a boolean")
            # gate defaults to False in judge mode, True for composition.
            gate = reward_raw.get("gate", judge is None)
            if not isinstance(gate, bool):
                raise ValueError("reward.gate must be a boolean")
            formula = str(reward_raw.get("formula", "weighted"))
            # Validate expression formulas now so a typo or unsafe construct
            # fails loudly here, not silently as reward 0.0 on every case at
            # run time. Bare references ("weighted") are resolved at compute
            # time, so skip the expression check for them. Skipped in judge
            # mode, where formula is unused.
            if judge is None and not re.fullmatch(
                    r"[A-Za-z_][\w.\-]*", formula.strip()):
                from agent_eval.harbor.reward import validate_formula
                try:
                    validate_formula(formula)
                except ValueError as exc:
                    raise ValueError(
                        f"reward.formula is invalid: {exc}") from exc
            config.reward = RewardConfig(
                formula=formula,
                weights=weights,
                gate=gate,
                score_range=[score_min, score_max],
                raw=[str(r) for r in raw_list],
                judge=judge,
                normalize=normalize,
            )

        # Thresholds
        config.thresholds = raw.get("thresholds", {})

        # Hooks
        hooks_raw = raw.get("hooks", {}) or {}
        phases = ["before_all", "before_each", "after_each",
                  "before_scoring", "after_all"]
        for phase in phases:
            entries = []
            for h in (hooks_raw.get(phase) or []):
                on_failure_val = h.get("on_failure", "fail")
                if on_failure_val not in ("fail", "continue"):
                    raise ValueError(
                        f"hooks.{phase}: on_failure must be 'fail' or "
                        f"'continue', got '{on_failure_val}'")
                timeout_val = h.get("timeout", 120)
                if not isinstance(timeout_val, int) or timeout_val <= 0:
                    raise ValueError(
                        f"hooks.{phase}: timeout must be a positive "
                        f"integer, got {timeout_val}")
                entries.append(HookEntry(
                    command=h.get("command", ""),
                    timeout=timeout_val,
                    description=h.get("description", ""),
                    on_failure=on_failure_val,
                    condition=h.get("condition", ""),
                ))
            setattr(config.hooks, phase, entries)

        if config.execution.mode == "batch":
            per_case = []
            if config.hooks.before_each:
                per_case.append("before_each")
            if config.hooks.after_each:
                per_case.append("after_each")
            if per_case:
                import warnings
                warnings.warn(
                    f"hooks.{', '.join(per_case)} ignored in batch mode "
                    f"(per-case hooks only run in case/prompt mode)",
                    stacklevel=2,
                )

        resolved_skill = config.resolve_skill()
        if resolved_skill:
            try:
                _validate_path_segment(resolved_skill, f"skill name in {path}")
            except ValueError as e:
                raise ValueError(str(e)) from e

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
    Files that fail YAML parsing are skipped.

    Eval names use backward-compatible fallback chain:
    1. skill field (preserves existing skill-based evals)
    2. name field (explicit naming, sanitized)
    3. directory/filename (path-based derivation)

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
        if not isinstance(raw, dict):
            print(f"Warning: skipping {yaml_path}: not a YAML dictionary", file=sys.stderr)
            return

        # Derive eval_name using fallback chain (same as EvalConfig.eval_name())
        eval_name = None

        # Priority 1: skill field (execution.skill canonical, top-level fallback)
        skill_ref = (raw.get("execution") or {}).get("skill") or raw.get("skill")
        if skill_ref:
            eval_name = skill_ref

        # Priority 2: name field (explicit identifier, sanitized)
        if not eval_name and raw.get("name"):
            sanitized = raw["name"].lower().replace(" ", "-")
            sanitized = "".join(c for c in sanitized if c.isalnum() or c in "._-")
            if sanitized and _is_valid_eval_name(sanitized):
                eval_name = sanitized

        # Priority 3: derive from path
        if not eval_name:
            if is_root:
                eval_name = "eval"
            elif yaml_path.name == "eval.yaml":
                # Nested: eval/api-docs/eval.yaml → "api-docs"
                eval_name = yaml_path.parent.name
            else:
                # Flat: eval/user-guides.yaml → "user-guides"
                eval_name = yaml_path.stem

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
