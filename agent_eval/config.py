"""Evaluation suite configuration loaded from eval.yaml files."""

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union
import sys

import yaml


def resolve_arguments(
    template: str, input_data: dict, steps: Optional[dict] = None
) -> str:
    """Resolve a skill/prompt argument template against input.yaml data.

    ``steps`` (optional) binds the ``{{ steps.<id>.* }}`` namespace for
    multi-step execution — the accumulated results of earlier steps in the same
    case (Jinja2 style only; the brace style resolves ``input`` fields only).

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
                input=input_data, steps=steps or {}
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


def resolve_plugin_path(configured: str, project_root, config_dir=None) -> Path:
    """Trust-boundary resolution for plugin dirs, shared by every consumer.

    This is the single implementation of the plugin-path security rules —
    the runtime runners and the eval.yaml validator must agree on them, so
    neither may carry its own copy. Relative paths use the project root, which
    matches the runner behavior before this helper existed. A path that is
    lexically inside the project may not escape it through a symlink. A path
    declared lexically outside (for example ``../shared-plugins``) is an
    explicit operator opt-in equivalent to an absolute external path.
    Existence is not checked here; callers decide how a missing directory is
    reported. ``config_dir`` remains accepted for API compatibility but is not
    a resolution base.
    """
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path.resolve()
    root = Path(project_root).resolve()
    lexical = Path(os.path.abspath(root / path))
    resolved = lexical.resolve()
    if lexical.is_relative_to(root) and not resolved.is_relative_to(root):
        raise ValueError(
            "A plugin_dirs entry declared inside the project must not escape "
            f"the project root through a symlink: {configured!r} resolved to "
            f"{resolved}")
    return resolved


def resolve_plugin_dir(config, configured: str) -> Path:
    """Resolve one runner plugin directory, requiring it to exist.

    Validation happens before the first case runs so a misconfigured plugin
    fails fast rather than mid-suite.
    """
    resolved = resolve_plugin_path(configured, config.project_root,
                                   config.config_dir)
    if not resolved.is_dir():
        raise FileNotFoundError(f"Runner plugin directory not found: {resolved}")
    return resolved


def resolve_plugin_skill_roots(plugin_dir: str | Path) -> list[Path]:
    """Resolve the skill roots exported by one Claude plugin.

    ``.claude-plugin/plugin.json`` may override the conventional ``skills/``
    directory with a string or list in its ``skills`` field. Invalid manifests
    and missing roots fail fast: silently starting Codex without the configured
    skills would turn a setup error into a misleading model-quality failure.
    """
    plugin = Path(plugin_dir).resolve()
    manifest_path = plugin / ".claude-plugin" / "plugin.json"
    configured_roots = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"Cannot read plugin manifest {manifest_path}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError(
                f"Plugin manifest {manifest_path} must be a JSON object")
        configured_roots = manifest.get("skills")

    if configured_roots is None:
        entries = ["skills"]
    elif isinstance(configured_roots, str) and configured_roots:
        entries = [configured_roots]
    elif (isinstance(configured_roots, list) and configured_roots
          and all(isinstance(entry, str) and entry for entry in configured_roots)):
        entries = configured_roots
    else:
        raise ValueError(
            f"Plugin manifest {manifest_path} field 'skills' must be a "
            "non-empty string or list of non-empty strings")

    roots = []
    for entry in entries:
        # The manifest is third-party content; its entries must not name
        # host paths outside the plugin the operator actually opted into,
        # whether spelled absolute, with ``..``, or through a symlink.
        root = (plugin / entry).resolve()
        if not root.is_relative_to(plugin):
            raise ValueError(
                "Plugin skill roots must stay beneath the plugin directory "
                f"{plugin}: {entry!r} resolved to {root}")
        roots.append(root)
    missing = [root for root in roots if not root.is_dir()]
    if missing:
        raise FileNotFoundError(
            "Plugin skill directory not found: " + ", ".join(map(str, missing)))
    if not any(any(child.is_dir() and (child / "SKILL.md").is_file()
                       for child in root.iterdir()) for root in roots):
        raise ValueError(f"Configured plugin has no discoverable skills: {plugin}")
    return roots


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
    # Simulator calibration shadow: when True, tools.py ALSO runs the LLM
    # tier on every override-answered AskUserQuestion — held out (the shadow
    # context excludes answers.yaml) and logged to the hook_answers.jsonl
    # ledger, never injected. Feeds summary['simulator'].calibration.
    calibration: bool = False


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
    before_step: list = field(default_factory=list)
    after_step: list = field(default_factory=list)
    before_scoring: list = field(default_factory=list)
    after_all: list = field(default_factory=list)
    before_report: list = field(default_factory=list)


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
    # Multi-step pipeline. When non-empty, REPLACES skill/prompt/arguments —
    # each entry is one agent invocation run sequentially in the shared per-case
    # workspace (see StepConfig). Mutually exclusive with skill/prompt; case
    # mode only.
    steps: list = field(default_factory=list)

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

        # Multi-step: steps replaces skill/prompt and is case-mode only.
        if self.steps:
            if has_skill or has_prompt:
                raise ValueError(
                    "execution.steps is mutually exclusive with execution.skill/"
                    "execution.prompt — put each invocation in its own step."
                )
            if self.mode != "case":
                raise ValueError(
                    "execution.steps is only supported in mode: case "
                    f"(got mode: {self.mode})."
                )
            ids = [getattr(s, "id", "") for s in self.steps]
            if any(not (i and str(i).strip()) for i in ids):
                raise ValueError(
                    "execution.steps: every step needs a non-empty 'id'."
                )
            # ids become filesystem path components (workspace, run output,
            # Harbor task) — reject separators / '.'/'..' / control chars (CWE-22).
            for i in ids:
                _validate_path_segment(str(i), "execution.steps[].id")
            if len(set(ids)) != len(ids):
                raise ValueError(
                    f"execution.steps: step ids must be unique, got {ids}."
                )

    def resolved_steps(self) -> list:
        """The pipeline as an explicit step list — one code path for the executor.

        Multi-step configs return ``steps`` verbatim.  A single skill/prompt
        config is normalized to a one-element list so the executor always loops.
        """
        if self.steps:
            return self.steps
        return [StepConfig(
            id=(self.skill or "step-1"),
            skill=self.skill,
            prompt=self.prompt,
            arguments=self.arguments,
            env=dict(self.env),
            timeout=self.timeout,
            max_budget_usd=self.max_budget_usd,
        )]



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
    # Claude Code: low..max; Codex: minimal..xhigh (runner validates precisely).
    effort: Optional[str] = None
    # Claude Code: default | acceptEdits | plan | auto | dontAsk | bypassPermissions.
    # Passed as --permission-mode (a CLI flag), so it applies even in untrusted
    # isolated workspaces where settings-file permissions are trust-gated.
    permission_mode: Optional[str] = None


def _parse_runner_config(runner_raw, *, context="runner"):
    """Parse a runner block into a RunnerConfig with validation.

    Shared by the top-level ``runner:`` block, a judge's nested
    ``agent.runner:`` block, and per-step ``execution.steps[].runner:`` so all
    honor identical defaults and validation (command type-check,
    workspace_mode whitelist). ``context`` is the field path used in error
    messages.
    """
    runner_raw = runner_raw or {}
    command = runner_raw.get("command")
    if command is not None:
        valid_list = isinstance(command, list) and all(
            isinstance(x, str) for x in command
        )
        if not (isinstance(command, str) or valid_list):
            raise ValueError(f"{context}.command must be a string or list of strings")
    # Validate workspace_mode (prevent typos that silently change behavior)
    workspace_mode = runner_raw.get("workspace_mode")
    if workspace_mode is not None and workspace_mode not in ("repo",):
        raise ValueError(
            f"{context}.workspace_mode must be None or 'repo', got: {workspace_mode!r}")
    # Runner type. YAML `type: null` parses to None — that is just an absent
    # key (default applies), NOT the null probe runner. Only the literal
    # string "null" is rejected: the null runner is the CLI-only solvability
    # probe, and a config permanently pinned to it is always a mistake.
    runner_type = runner_raw.get("type")
    if runner_type is None:
        runner_type = "claude-code"
    if runner_type == "null":
        raise ValueError(
            f'{context}.type "null" is not a valid config runner: the null '
            "(do-nothing) runner is the CLI-only dataset solvability probe — "
            "invoke it with `--agent null` on execute.py instead")
    return RunnerConfig(
        type=runner_type,
        command=command,
        workspace_mode=workspace_mode,
        settings=runner_raw.get("settings", {}) or {},
        plugin_dirs=runner_raw.get("plugin_dirs", []) or [],
        env=runner_raw.get("env", {}) or {},
        system_prompt=runner_raw.get("system_prompt"),
        effort=runner_raw.get("effort"),
        permission_mode=runner_raw.get("permission_mode"),
    )


@dataclass
class StepConfig:
    """One step in a multi-step execution pipeline (``execution.steps[]``).

    A step is a single agent invocation (``skill`` xor ``prompt``) run in the
    shared per-case workspace.  Steps run sequentially; later steps see earlier
    steps' files on disk and can reference their results via the
    ``{{ steps.<id>.* }}`` template namespace.  Per-step ``timeout`` /
    ``max_budget_usd`` / ``runner`` fall back to the ``execution`` / top-level
    defaults when unset.
    """

    id: str = ""
    name: str = ""
    skill: str = ""       # skill xor prompt (validated per step)
    prompt: str = ""
    arguments: str = ""
    env: dict = field(default_factory=dict)
    timeout: Optional[int] = None
    max_budget_usd: Optional[float] = None
    runner: Optional[RunnerConfig] = None
    on_failure: str = "fail"  # "fail" (abort remaining steps) | "continue"

    def __post_init__(self):
        if self.on_failure not in ("fail", "continue"):
            raise ValueError(
                f"execution.steps: step '{self.id}': on_failure must be 'fail' "
                f"or 'continue', got '{self.on_failure}'")
        has_skill = bool(self.skill and self.skill.strip())
        has_prompt = bool(self.prompt and self.prompt.strip())
        if has_skill and has_prompt:
            raise ValueError(
                f"execution.steps: step '{self.id}': skill and prompt are "
                "mutually exclusive.")


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


#: Valid ``JudgeConfig.consequence`` tiers (measurement-validity program).
CONSEQUENCE_LEVELS = ("exploratory", "safety", "gating")

#: Tier-default ``min_alpha`` for consequence-tagged judges, resolved at
#: detection time via ``effective_thresholds()`` — never written into
#: ``config.thresholds``. The gated coefficient is the single-judge
#: self-consistency alpha, an upper bound on inter-rater reliability.
#: Only 0.67 is literature-backed (Krippendorff's customary floor for
#: tentative conclusions); 0.70 and 0.80 are author-proposed tiers.
CONSEQUENCE_TIER_MIN_ALPHA = {
    "exploratory": 0.67,
    "safety": 0.70,
    "gating": 0.80,
}

#: Recognized per-judge ``thresholds`` keys. Unknown keys warn at config load
#: (never error); regression detection ignores them. ``min_human_agreement``
#: gates the post-hoc judge-vs-human calibration coefficient merged by
#: ``score.py calibration`` (its value validation rides the generic
#: ``*_agreement`` rule in ``_parse_thresholds``).
#: ``min_panel_alpha`` gates the cross-model panel alpha of a judge whose
#: ``model`` is a list (a judge panel); its value validation rides the
#: generic ``*_alpha`` rule below. Consequence tiers inject ``min_alpha``
#: ONLY — a panel gate is always explicit (user decision Q3).
THRESHOLD_KEYS = frozenset({
    "min_mean", "min_pass_rate", "min_win_rate", "max_error_rate", "min_alpha",
    "min_human_agreement", "min_panel_alpha",
})

#: RESERVED ``thresholds`` mapping key: ``thresholds.simulator`` gates the
#: run-level ``summary['simulator']`` block (aggregated from the
#: hook_answers.jsonl ledgers by ``score.py``), never a judge — a judge
#: literally named "simulator" is rejected when the block coexists (and
#: DeprecationWarning'd otherwise). ``min_cross_simulator_agreement`` is
#: accepted-but-warned: it activates with cross-family shadow simulators
#: (``models.hook_shadow``) in a later commit.
SIMULATOR_THRESHOLD_KEYS = frozenset({
    "max_fallback_rate", "min_gold_agreement", "min_cross_simulator_agreement",
})


def _parse_simulator_thresholds(entry):
    """Validate the reserved ``thresholds.simulator`` block (see above).

    Sub-keys outside :data:`SIMULATOR_THRESHOLD_KEYS` warn (parallel to the
    judge-key rule: never error on unknown). Values must be numeric, finite
    and <= 1.0; ``max_fallback_rate`` must additionally be >= 0 (it is a
    rate, and a negative bound would regress every run).
    """
    import warnings
    if not isinstance(entry, dict):
        raise ValueError(
            "thresholds.simulator is a RESERVED key and must be a mapping "
            f"with keys from: {', '.join(sorted(SIMULATOR_THRESHOLD_KEYS))}")
    for key, value in entry.items():
        if key not in SIMULATOR_THRESHOLD_KEYS:
            warnings.warn(
                f"thresholds.simulator: unknown key '{key}' is ignored by "
                "regression detection (valid keys: "
                f"{', '.join(sorted(SIMULATOR_THRESHOLD_KEYS))})",
                stacklevel=3)
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or float(value) > 1.0):
            raise ValueError(
                f"thresholds.simulator.{key} must be a finite number <= 1.0, "
                f"got: {value!r}")
        if key == "max_fallback_rate" and float(value) < 0:
            raise ValueError(
                f"thresholds.simulator.max_fallback_rate must be >= 0 "
                f"(it is a rate), got: {value!r}")
        if key == "min_cross_simulator_agreement":
            warnings.warn(
                "thresholds.simulator.min_cross_simulator_agreement is "
                "reserved for cross-family shadow simulators "
                "(models.hook_shadow — not yet active); it is accepted but "
                "not evaluated by regression detection yet",
                stacklevel=3)


def _parse_thresholds(raw_thresholds):
    """Validate the ``thresholds`` block; returns it unchanged.

    The one thresholds validation helper. Unknown keys warn — never error —
    so a typo like ``min_apha`` stops silently never gating. Any
    ``*_alpha`` / ``*_agreement`` key value must be numeric, finite, and
    <= 1.0 (the coefficient maximum); anything else raises ``ValueError``.
    The ``simulator`` mapping key is RESERVED (never a judge name) and
    validated against :data:`SIMULATOR_THRESHOLD_KEYS` instead.
    """
    if raw_thresholds is None:
        return {}
    if not isinstance(raw_thresholds, dict):
        raise ValueError("thresholds must be a mapping")
    import warnings
    for judge_name, entry in raw_thresholds.items():
        if judge_name == "simulator":
            _parse_simulator_thresholds(entry)
            continue
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if key not in THRESHOLD_KEYS:
                warnings.warn(
                    f"thresholds.{judge_name}: unknown key '{key}' is ignored "
                    "by regression detection (valid keys: "
                    f"{', '.join(sorted(THRESHOLD_KEYS))})",
                    stacklevel=2)
            if key.endswith("_alpha") or key.endswith("_agreement"):
                if (isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or float(value) > 1.0):
                    raise ValueError(
                        f"thresholds.{judge_name}.{key} must be a finite "
                        f"number <= 1.0 (the coefficient maximum), "
                        f"got: {value!r}")
    return raw_thresholds


def effective_thresholds(thresholds: dict, judges) -> dict:
    """Merged thresholds VIEW with consequence-tier defaults injected.

    Returns a copy: an explicit threshold always wins; a consequence-tagged
    judge with no explicit ``min_alpha`` gets its tier default injected into
    the returned view only. ``thresholds`` itself is NEVER mutated —
    ``harbor/run.py`` reads ``config.thresholds`` as a required-judges set,
    so tier resolution happens at detection time, not at load time.

    ``judges`` entries are duck-typed: ``JudgeConfig`` instances or raw dicts
    (report.py passes the raw eval.yaml judges list). Invalid consequence
    strings in raw dicts are skipped silently — ``EvalConfig.from_yaml``
    already rejects them at load.

    The RESERVED ``simulator`` mapping key passes through UNTOUCHED: it is
    not a judge key, so the tier-injection loop below never writes into it
    (a deprecated judge literally named "simulator" is skipped here — its
    tier default would otherwise leak ``min_alpha`` into the simulator
    gate block).
    """
    out = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in (thresholds or {}).items()}
    for judge in judges or []:
        if isinstance(judge, dict):
            name = judge.get("name") or ""
            consequence = judge.get("consequence") or ""
        else:
            name = getattr(judge, "name", "") or ""
            consequence = getattr(judge, "consequence", "") or ""
        tier = CONSEQUENCE_TIER_MIN_ALPHA.get(consequence)
        if not name or name == "simulator" or tier is None:
            continue
        entry = out.setdefault(name, {})
        if isinstance(entry, dict):
            entry.setdefault("min_alpha", tier)
    return out


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
    # Optional verdict shape: "bool" (pass/fail) vs "int"/"float" (numeric
    # score). Never inferred — an omitted value means numeric, and int-vs-float
    # is then read off `score_range` (whole bounds => integer). "str" and
    # "Literal[...]" apply only on the MLflow make_judge fallback path.
    feedback_type: str = ""
    # Numeric scale [lo, hi] for this judge's value. When declared it is stated
    # in the LLM judge's system prompt and tool schema, enforced on the returned
    # value (an off-scale value is recorded as an error sample, not clamped),
    # used by the report to color per-cell bands proportionally, and used to
    # normalize this judge in the reward composition. If omitted, LLM
    # judges are told [1, 5] and nothing is enforced — an inline check returning
    # a raw count keeps returning it. Set explicitly for judges on a non-default
    # range (e.g. 0-2, 1-10, 0-100). This is the scale EVERY reward composition
    # normalizes the judge over; `reward.score_range` is only a fallback for
    # composed judges that declare none.
    score_range: Optional[list] = None
    # Override model for this judge (pairwise, LLM). In YAML, `model` also
    # accepts a LIST of 2-4 model ids — a judge panel: `model` then holds the
    # first entry and `panel_models` the full list.
    model: str = ""
    # Judge panel — derived from a list-valued `model:` in YAML, never its
    # own YAML key. Non-empty only for LLM judges (llm_rubric/prompt/
    # prompt_file); the scorer fans out per model and reduces by majority
    # (bool) / median_low (numeric) over the per-model reduced verdicts.
    panel_models: list = field(default_factory=list)
    # External code judge
    module: str = ""
    function: str = ""
    # Builtin judge (resolves via BuiltinJudgeRegistry)
    builtin: str = ""
    # Arguments passed as **kwargs to Python judges, Jinja var to LLM judges
    arguments: dict = field(default_factory=dict)
    # Multi-step: scope this judge to one execution step's sub-record. Empty =
    # whole case (final workspace), the default. Must match an execution.steps id.
    step: str = ""
    # Sampling — run this judge N times per case and reduce (median/majority).
    # Only meaningful for stochastic (LLM and agent) judges; ignored otherwise.
    samples: int = 1
    # Consequence tier (measurement-validity P5): exploratory | safety |
    # gating. Injects a tier-default `min_alpha` at detection time via
    # `effective_thresholds()` (0.67 / 0.70 / 0.80 — only 0.67 is
    # literature-backed; 0.70 and 0.80 are author-proposed). The gated
    # coefficient is the single-judge self-consistency alpha, an UPPER BOUND
    # on inter-rater reliability — a self-consistent-but-biased judge still
    # passes. Needs `samples >= 2` on an LLM/agent judge to produce IRR data.
    consequence: str = ""
    # Agent judge — presence of this block upgrades an (otherwise LLM) judge to
    # a tool-using agent run through the runner abstraction, with read-only file
    # tools and a staged, isolated workspace. Permissive mapping (mirrors
    # `arguments`); recognized keys: runner (RunnerConfig), allowed_tools,
    # context, inputs, timeout, max_budget_usd. A nested `runner:` sub-block is
    # parsed into a RunnerConfig by from_yaml.
    agent: dict = field(default_factory=dict)


@dataclass
class RewardConfig:
    """Reward composition from judge results for RL training.

    Two ways to produce the reward, mutually exclusive:

    1. ``judge``: a single judge whose value IS the reward. By default the
       value is used as-is, clamped to [0, 1] (for a judge that already emits
       a [0, 1] reward, e.g. a learned reward model). Set ``normalize: true``
       to instead map it from the judge's own ``score_range`` to [0, 1].
    2. ``formula`` (+ ``weights``): compose from multiple judges —
       - "weighted": weighted sum of ``weights``, each normalized over its own
         declared ``score_range`` (or clamped if listed in ``raw``).
       - "<expression>": Python expression with judge names as variables.

    When gate is True, any boolean judge that returned False zeros the reward.
    Note this gates on *every* boolean judge, independent of whether the
    formula references it — so an ``<expression>`` that uses booleans as its
    own gate (e.g. ``passed * score``) usually wants ``gate: false`` to avoid
    double-gating. ``gate`` defaults to False in ``judge`` mode.
    score_range: DEPRECATED fallback, used only for composed judges that
         declare no ``score_range`` of their own ([1, 5] when absent — read it
         through ``effective_score_range``).
    raw: list of judge names whose values are already in [0, 1] and should
         be clamped rather than normalized over any range (e.g. efficiency).
    """

    formula: str = "weighted"
    weights: dict = field(default_factory=dict)
    gate: bool = True
    # Fallback scale for composed numeric judges that declare no `score_range`
    # of their own. DEPRECATED — declare the scale on the judge instead.
    # `None` means "absent from the YAML", which is what makes the deprecation
    # warning targetable. Read it through `effective_score_range`.
    score_range: Optional[list] = None
    raw: list = field(default_factory=list)
    # Single-judge mode: name of the judge whose value is the reward.
    judge: Optional[str] = None
    # In judge mode, map the value from the judge's own score_range instead of
    # clamping as-is.
    normalize: bool = False

    @property
    def effective_score_range(self) -> list:
        """The fallback range, resolved. Never ``None``."""
        return list(self.score_range) if self.score_range else [1.0, 5.0]


def _reward_normalized_judges(reward, judge_names: set) -> set:
    """Judges whose value the reward composition normalizes over a range.

    Excludes `raw` judges, a clamped single judge, and names a formula never
    reads — none of those consult a range, so a range conflict cannot move
    them, and warning about them would be noise.
    """
    if reward.judge is not None:
        return {reward.judge} if reward.normalize else set()
    formula = (reward.formula or "").strip()
    if formula == "weighted":
        named = set(reward.weights)
    else:
        from agent_eval.harbor.reward import formula_judge_names
        named = formula_judge_names(formula) & judge_names
    return named - set(reward.raw)


def _warn_reward_range_precedence(config) -> None:
    """Warn when a written `reward.score_range` no longer governs a judge.

    Only fires when the key is present in the YAML AND a judge it would have
    normalized declares a different range of its own — i.e. only where the
    precedence change actually moves a number.
    """
    reward = config.reward
    ranges = {j.name: [float(j.score_range[0]), float(j.score_range[1])]
              for j in config.judges if getattr(j, "score_range", None)}
    composed = _reward_normalized_judges(
        reward, {j.name for j in config.judges if j.name})
    fallback = reward.effective_score_range
    shadowed = sorted(n for n in composed if n in ranges and ranges[n] != fallback)
    if not shadowed:
        return
    still = sorted(n for n in composed if n not in ranges)
    tail = (f"It still applies to {', '.join(repr(n) for n in still)}; drop it "
            "once every composed judge declares a 'score_range'."
            if still else
            "No composed judge relies on it any more — delete it.")
    import warnings
    warnings.warn(
        f"reward.score_range {fallback} is deprecated and no longer normalizes "
        + ", ".join(f"'{n}' {ranges[n]}" for n in shadowed)
        + ": a judge's own 'score_range' wins. " + tail,
        stacklevel=2)


def _fmt_num(value) -> str:
    """Render a bound without a pointless trailing .0 — config coerces to float."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _warn_reward_judge_clamp(config) -> None:
    """Warn when a clamped single-judge reward is scored off [0, 1].

    `reward: {judge: x}` uses x's value as the reward directly, clamped — the
    right thing for a judge that already emits [0, 1] and wrong for every other
    scale, silently. A scale reaching 1 or beyond saturates: every value at or
    above 1 is the maximum reward. A narrower one (say [0, 0.5]) never
    saturates but never reaches the top of the reward range either. Both are
    fixed by `normalize: true`. Pre-existing; surfaced here because the same
    change makes a declared `score_range` authoritative everywhere else.
    """
    reward = config.reward
    if reward.judge is None or reward.normalize:
        return
    declared = next((j.score_range for j in config.judges
                     if j.name == reward.judge and j.score_range), None)
    if not declared or [float(declared[0]), float(declared[1])] == [0.0, 1.0]:
        return
    lo, hi = float(declared[0]), float(declared[1])
    effect = ("every score at or above 1 becomes the maximum reward"
              if hi >= 1 else
              f"the reward can never exceed {_fmt_num(hi)}")
    import warnings
    warnings.warn(
        f"reward.judge '{reward.judge}' declares score_range "
        f"[{_fmt_num(lo)}, {_fmt_num(hi)}] but 'normalize' is not set, so its "
        f"value is clamped to [0, 1] — {effect}. Set 'normalize: true' to map "
        f"it from [{_fmt_num(lo)}, {_fmt_num(hi)}].", stacklevel=2)


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

        # Execution config — including an optional multi-step pipeline.
        steps = []
        for i, s in enumerate(exec_raw.get("steps") or []):
            if not isinstance(s, dict):
                raise ValueError(f"execution.steps[{i}] must be a mapping")
            step_runner = None
            if s.get("runner"):
                step_runner = _parse_runner_config(
                    s.get("runner"), context=f"execution.steps[{i}].runner")
            step_env = s.get("env") or {}
            if not isinstance(step_env, dict):
                raise ValueError(
                    f"execution.steps[{i}].env must be a mapping")
            step_timeout = s.get("timeout")
            if step_timeout is not None and (
                    not isinstance(step_timeout, int)
                    or isinstance(step_timeout, bool)
                    or step_timeout <= 0):
                raise ValueError(
                    f"execution.steps[{i}].timeout must be a positive integer")
            step_budget = s.get("max_budget_usd")
            if step_budget is not None and (
                    not isinstance(step_budget, (int, float))
                    or isinstance(step_budget, bool)
                    or step_budget < 0):
                raise ValueError(
                    f"execution.steps[{i}].max_budget_usd must be a "
                    "non-negative number")
            step = StepConfig(
                id=s.get("id", "") or "",
                name=s.get("name", "") or "",
                skill=s.get("skill", "") or "",
                prompt=s.get("prompt", "") or "",
                arguments=s.get("arguments", "") or "",
                env=step_env,
                timeout=step_timeout,
                max_budget_usd=step_budget,
                runner=step_runner,
                on_failure=s.get("on_failure", "fail"),
            )
            if not ((step.skill and step.skill.strip())
                    or (step.prompt and step.prompt.strip())):
                raise ValueError(
                    f"execution.steps[{i}] ('{step.id}') must set either "
                    "skill or prompt")
            steps.append(step)

        execution = ExecutionConfig(
            mode=exec_raw.get("mode", "case"),
            skill=exec_raw.get("skill", "") or raw.get("skill", ""),
            prompt=exec_raw.get("prompt", ""),
            arguments=exec_raw.get("arguments", ""),
            timeout=exec_raw.get("timeout"),
            max_budget_usd=exec_raw.get("max_budget_usd"),
            parallelism=exec_raw.get("parallelism"),
            env=exec_raw.get("env") or {},
            steps=steps,
        )

        # Runner config (block form)
        runner = _parse_runner_config(raw.get("runner"), context="runner")

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
        for i, t in enumerate(inputs_raw.get("tools") or []):
            calibration_val = t.get("calibration", False)
            if not isinstance(calibration_val, bool):
                raise ValueError(
                    f"inputs.tools[{i}].calibration must be a boolean, "
                    f"got: {calibration_val!r}")
            if (calibration_val
                    and "askuserquestion" not in str(t.get("match", "")).lower()):
                import warnings
                warnings.warn(
                    f"inputs.tools[{i}].calibration is set but the match text "
                    "does not mention AskUserQuestion — the calibration "
                    "shadow only affects the AskUserQuestion answering tier",
                    stacklevel=2)
            config.inputs.tools.append(
                ToolInputConfig(
                    match=t.get("match", ""),
                    prompt=t.get("prompt", ""),
                    prompt_file=t.get("prompt_file", ""),
                    calibration=calibration_val,
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
            agent_val = j.get("agent")
            if agent_val is None:
                agent_val = {}
            elif not isinstance(agent_val, dict):
                raise ValueError(
                    f"Judge '{j.get('name', '')}': 'agent' must be a mapping"
                )
            elif agent_val.get("runner") is not None:
                # Parse the nested runner: sub-block with the SAME block-parsing
                # logic as the top-level runner, so a judge's runner is fully
                # validated. Shallow-copy so the raw YAML isn't mutated.
                if not isinstance(agent_val["runner"], dict):
                    raise ValueError(
                        f"Judge '{j.get('name', '')}': 'agent.runner' must be a mapping"
                    )
                agent_val = dict(agent_val)
                agent_val["runner"] = _parse_runner_config(
                    agent_val["runner"],
                    context=f"Judge '{j.get('name', '')}': agent.runner",
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
                if not (math.isfinite(lo) and math.isfinite(hi)) or lo >= hi:
                    raise ValueError(
                        f"Judge '{jname}': 'score_range' must be finite and "
                        "increasing [min, max]")
                score_range_val = [lo, hi]
            # `model` — a string, or a LIST of 2-4 model ids (a judge panel).
            # Normalize None FIRST: a bare `model:` key (explicit YAML null)
            # loaded fine before lists existed and must keep loading.
            jname = j.get("name", "")
            model_raw = j.get("model", "")
            if model_raw is None:
                model_raw = ""
            panel_models_val = []
            if isinstance(model_raw, list):
                entries = list(model_raw)
                if not entries:
                    raise ValueError(
                        f"Judge '{jname}': 'model' list cannot be empty — "
                        "use a string, or 2-4 model ids for a judge panel")
                if not all(isinstance(e, str) and e.strip() for e in entries):
                    raise ValueError(
                        f"Judge '{jname}': model list entries must be "
                        "non-empty strings")
                if len(set(entries)) != len(entries):
                    raise ValueError(
                        f"Judge '{jname}': duplicate model in panel list — "
                        "a duplicate panel model would double-weight a rater")
                if len(entries) > 4:
                    raise ValueError(
                        f"Judge '{jname}': a judge panel supports 2-4 "
                        f"models, got {len(entries)}")
                # A 1-item list is a plain single-model judge, not a panel.
                model_val = entries[0]
                if len(entries) >= 2:
                    panel_models_val = entries
            elif isinstance(model_raw, str):
                model_val = model_raw
            else:
                raise ValueError(
                    f"Judge '{jname}': 'model' must be a string or a list "
                    "of strings")
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
                    model=model_val,
                    panel_models=panel_models_val,
                    module=j.get("module", ""),
                    function=j.get("function", ""),
                    builtin=builtin_val,
                    arguments=args_val,
                    step=j.get("step", "") or "",
                    samples=int(j.get("samples", 1)),
                    consequence=str(j.get("consequence", "") or ""),
                    agent=agent_val,
                )
            )

        # Per-step judge scoping: a judge's `step:` must name a defined
        # execution step (fail loud on typos, like reward.judge validation).
        step_ids = {s.id for s in execution.steps}
        for jc in config.judges:
            if not jc.step:
                continue
            if not execution.steps:
                raise ValueError(
                    f"Judge '{jc.name}': 'step: {jc.step}' requires an "
                    "execution.steps pipeline")
            if jc.step not in step_ids:
                raise ValueError(
                    f"Judge '{jc.name}': 'step: {jc.step}' does not match any "
                    f"execution step id ({sorted(step_ids)})")

        # Scale coherence: a judge's declared scale has to agree with its
        # feedback_type and with the scorer that will actually run it. Each of
        # these used to be accepted and then quietly ignored at scoring time,
        # which is how a judge shipped scoring on a scale nobody declared.
        from agent_eval.judges import builtin_judge_kind, builtin_judge_names

        for jc in config.judges:
            builtin_kind = builtin_judge_kind(jc.builtin) if jc.builtin else None
            if jc.builtin and builtin_kind is None:
                raise ValueError(
                    f"Judge '{jc.name}': unknown builtin judge '{jc.builtin}' "
                    f"(available: {', '.join(builtin_judge_names())})")
            # Judge panels (list-valued `model`) are valid ONLY on LLM judges
            # (llm_rubric/prompt/prompt_file): deterministic judges take no
            # model, builtins own their prompt contract, and the agent-judge
            # runner path resolves one model per judge — a panel there would
            # silently run one model, so it is rejected loudly at load.
            if jc.panel_models:
                if jc.builtin or jc.check or jc.module:
                    kind = ("builtin" if jc.builtin
                            else "check" if jc.check else "module")
                    raise ValueError(
                        f"Judge '{jc.name}': 'model' list (judge panel) is "
                        f"not valid on a {kind} judge — a panel needs an "
                        "LLM judge (llm_rubric/prompt/prompt_file)")
                if jc.agent:
                    raise ValueError(
                        f"Judge '{jc.name}': judge panels are not supported "
                        "for agent judges — the agent-judge runner path is "
                        "pinned to one model per judge")
                if not (jc.prompt or jc.prompt_file or jc.llm_rubric):
                    raise ValueError(
                        f"Judge '{jc.name}': 'model' list on a non-LLM "
                        "judge — a judge panel needs an LLM judge "
                        "(llm_rubric/prompt/prompt_file)")
            if jc.feedback_type == "bool" and jc.score_range:
                raise ValueError(
                    f"Judge '{jc.name}': 'score_range' has no meaning with "
                    "'feedback_type: bool' (the verdict is pass/fail) — "
                    "drop one of the two")
            if (jc.feedback_type == "int" and jc.score_range
                    and any(float(b) != int(b) for b in jc.score_range)):
                raise ValueError(
                    f"Judge '{jc.name}': 'feedback_type: int' cannot express "
                    f"the fractional 'score_range' {jc.score_range} — use "
                    "'feedback_type: float'")
            if (builtin_kind == "llm"
                    and (jc.feedback_type not in ("", "bool") or jc.score_range)):
                raise ValueError(
                    f"Judge '{jc.name}': builtin LLM judge '{jc.builtin}' is "
                    "always scored as pass/fail, so 'feedback_type'/"
                    "'score_range' would be silently ignored")
            # `feedback_type` is optional, and score.py's `_numeric_bounds`
            # treats anything that is not "bool" as numeric — so the judge that
            # most needs this warning is the one that declares neither field,
            # and gating on ("int", "float") alone never reached it.
            if (jc.feedback_type in ("int", "float", "") and not jc.score_range
                    and not jc.builtin
                    and (jc.prompt or jc.prompt_file or jc.llm_rubric)):
                import warnings
                warnings.warn(
                    f"Judge '{jc.name}': numeric judge has no 'score_range', "
                    "so it is scored on the unenforced [1, 5] default — "
                    "declare one to have the returned value checked",
                    stacklevel=2)
            # Consequence tiers gate the sampling-stability alpha; warn at
            # load when the judge cannot produce IRR data, because the
            # tier-default min_alpha will then regress as
            # configured-but-unavailable at detection time.
            if jc.consequence and jc.consequence not in CONSEQUENCE_LEVELS:
                raise ValueError(
                    f"Judge '{jc.name}': consequence must be one of "
                    f"{', '.join(CONSEQUENCE_LEVELS)}, got: {jc.consequence!r}")
            if jc.consequence:
                import warnings
                stochastic = bool(jc.prompt or jc.prompt_file or jc.llm_rubric
                                  or jc.agent)
                if builtin_kind == "llm":
                    warnings.warn(
                        f"Judge '{jc.name}': consequence '{jc.consequence}' "
                        f"is set but builtin LLM judge '{jc.builtin}' is "
                        "pinned to samples: 1 at scoring time, so no IRR "
                        "data will exist and the tier-default min_alpha will "
                        "regress as unavailable",
                        stacklevel=2)
                elif stochastic and jc.samples <= 1:
                    warnings.warn(
                        f"Judge '{jc.name}': consequence '{jc.consequence}' "
                        "is set but samples: 1 produces no IRR data — the "
                        "tier-default min_alpha will regress as unavailable "
                        "unless the judge runs with --samples >= 2",
                        stacklevel=2)
                elif not stochastic:
                    warnings.warn(
                        f"Judge '{jc.name}': consequence '{jc.consequence}' "
                        "is set on a deterministic judge, which is never "
                        "sampled and produces no IRR data — the tier-default "
                        "min_alpha will regress as unavailable",
                        stacklevel=2)

        # Reward composition
        if "reward" in raw:
            reward_raw = raw.get("reward")
            if not isinstance(reward_raw, dict):
                raise ValueError("reward must be a mapping when provided")
            sr = reward_raw.get("score_range")
            reward_score_range = None
            if sr is not None:
                if not isinstance(sr, list) or len(sr) != 2:
                    raise ValueError(
                        "reward.score_range must be a [min, max] list")
                try:
                    score_min = float(sr[0])
                    score_max = float(sr[1])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "reward.score_range values must be numeric") from exc
                if (not (math.isfinite(score_min) and math.isfinite(score_max))
                        or score_min >= score_max):
                    raise ValueError(
                        "reward.score_range must be finite and increasing "
                        "[min, max]")
                reward_score_range = [score_min, score_max]
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
                score_range=reward_score_range,
                raw=[str(r) for r in raw_list],
                judge=judge,
                normalize=normalize,
            )
            if sr is not None:
                _warn_reward_range_precedence(config)
            _warn_reward_judge_clamp(config)

        # Thresholds — validated (unknown keys warn; bad *_alpha values
        # raise) but stored verbatim; consequence-tier defaults resolve at
        # detection time via `effective_thresholds()`, never here.
        config.thresholds = _parse_thresholds(raw.get("thresholds", {}))

        # TWO-STAGE reservation of the judge name "simulator" (backcompat):
        # `thresholds.simulator` is the reserved simulator-gate block, so a
        # judge with that name plus the block is a genuine collision — the
        # detector could not tell the judge's gates from the simulator's.
        # Without the block, existing configs keep loading with a
        # DeprecationWarning nudging a rename.
        if any(jc.name == "simulator" for jc in config.judges):
            if "simulator" in (config.thresholds or {}):
                raise ValueError(
                    "'simulator' is a reserved thresholds key (simulator "
                    "gates: max_fallback_rate/min_gold_agreement) and cannot "
                    "also be a judge name — rename the judge")
            import warnings
            warnings.warn(
                "the name 'simulator' is reserved for simulator gates "
                "(thresholds.simulator); rename the judge",
                DeprecationWarning, stacklevel=2)

        # Q3 (consequence x panels): tiers inject `min_alpha` ONLY — the
        # self-consistency gate. A consequence-tagged PANEL judge without an
        # explicit `min_panel_alpha` has an ungated panel alpha; say so at
        # load rather than silently leaving the truer coefficient ungated.
        for jc in config.judges:
            if not (jc.consequence and jc.panel_models):
                continue
            entry = (config.thresholds or {}).get(jc.name)
            if not (isinstance(entry, dict) and "min_panel_alpha" in entry):
                import warnings
                warnings.warn(
                    f"Judge '{jc.name}': consequence '{jc.consequence}' "
                    "injects a tier-default min_alpha only (single-judge "
                    "self-consistency); the judge's cross-model panel alpha "
                    "is NOT tier-gated — set an explicit "
                    f"thresholds.{jc.name}.min_panel_alpha to gate it",
                    stacklevel=2)

        # Appendix-B.4 same-family advisory (user decision Q2): fires ONLY
        # when reliability features are engaged — a judges[].model panel or
        # a consequence-tagged judge. (models.hook_shadow does not exist
        # yet; when it ships it joins this engagement test.) At most one
        # warning per load; unknown ids (gateway aliases) stay silent inside
        # same_family_advisory. The run-report same-family caveat is
        # independent of this and always renders.
        if any(jc.panel_models or jc.consequence for jc in config.judges):
            from agent_eval.model_families import same_family_advisory
            role_models = [m for m in (models.skill, models.subagent,
                                       models.judge, models.hook)
                           if m and isinstance(m, str)]
            panel_entries = []
            for jc in config.judges:
                if jc.panel_models:
                    panel_entries.extend(jc.panel_models)
                elif jc.model:
                    role_models.append(jc.model)
            advisory = same_family_advisory(role_models, panel_entries)
            if advisory:
                import warnings
                warnings.warn(advisory, stacklevel=2)

        # Hooks
        hooks_raw = raw.get("hooks", {}) or {}
        phases = ["before_all", "before_each", "after_each", "before_step",
                  "after_step", "before_scoring", "after_all", "before_report"]
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
            if config.hooks.before_step:
                per_case.append("before_step")
            if config.hooks.after_step:
                per_case.append("after_step")
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

        codex_runners = [config.runner]
        codex_runners.extend(
            step.runner for step in config.execution.steps if step.runner)
        codex_runners = [runner for runner in codex_runners
                         if runner.type == "codex"]
        if codex_runners and config.inputs.tools:
            raise ValueError(
                "runner.type 'codex' does not support inputs.tools interception; "
                "use claude-code or remove the tool interceptors")
        if any(runner.workspace_mode == "repo" for runner in codex_runners):
            raise ValueError(
                "runner.type 'codex' does not support workspace_mode: repo "
                "because repository answer-key protections cannot be enforced")

        return config

    @property
    def project_root(self) -> Path:
        """Project root directory (always CWD, not the eval.yaml location)."""
        return Path.cwd()

    def effective_thresholds(self) -> dict:
        """Thresholds view with consequence-tier ``min_alpha`` defaults.

        Detection-time resolution: explicit thresholds win, and
        ``self.thresholds`` is never mutated (see the module-level
        ``effective_thresholds``).
        """
        return effective_thresholds(self.thresholds or {}, self.judges)


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
