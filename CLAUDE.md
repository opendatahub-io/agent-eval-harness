# Agent Eval Harness

Generic evaluation framework for Claude Code skills and agent capabilities. Uses MLflow as the backbone for tracing, evaluation, datasets, and reporting.

## Project Status

Phase 1 (core framework), Phase 2 (scoring integration), and Phase 3 (prompt-based evaluation) are implemented. See `eval/plans/agent-eval-harness-design.md` in the rfe-creator project for the full design doc.

## Execution Model

The harness separates **how many invocations** (`execution.mode`) from **what to execute** (`execution.skill` or `execution.prompt`):

### Execution Mode (case vs batch)
- **case**: One invocation per test case (default). The harness loops over cases.
- **batch**: One invocation for all cases via batch.yaml. The skill/agent loops internally.

### What to Execute (skill vs prompt)
- **Skill mode** (`execution.skill`): Test predefined skill implementations (`/my-skill --args`). Evaluates skill correctness, quality, and cost efficiency.
- **Prompt mode** (`execution.prompt`): Test agent capabilities directly by sending prompts without a skill wrapper.

### Common Patterns

**Skill evaluation (case mode)**:
```yaml
execution:
  mode: case
  skill: rfe.create
  arguments: '--priority {{ input.priority }} "{{ input.prompt }}"'
```

**Skill evaluation (batch mode)**:
```yaml
execution:
  mode: batch
  skill: rfe.speedrun
  arguments: '--input batch.yaml --headless'
```

**Agentic documentation testing (prompt mode)** ✨:
```yaml
execution:
  mode: case
  prompt: "{{ input.prompt }}"
```

**Implemented flavor - Agentic Documentation Testing** (see `examples/openshift-agentic-docs.md`):
- Documentation effectiveness: Can agents navigate and use your docs?
- Pattern understanding: Can agents identify and apply code patterns?
- Constraint compliance: Do agents respect documented rules?
- API usage: Can agents call APIs with the right fields and structure from documentation alone?

Includes builtin documentation generation prompts (navigation, anti-pattern, authoring, component-usage, architecture) for structured evaluation. See `agent_eval/prompts/docs/`.

**Extensible to other scenarios**: Code generation, API pattern validation, reasoning quality, custom agent benchmarks.

Prompt mode provides direct agent invocation with LLM rubric judges and flexible evaluation criteria.

## Architecture

```
agent_eval/              # Python package (config, runner, state)
  config.py              # EvalConfig from eval.yaml
  state.py               # Shared state persistence (key-value store)
  reliability.py         # Chance-corrected IRR (pure stdlib): Krippendorff alpha, Fleiss/Cohen kappa, Figure-1 metric selection, bootstrap CI
  model_families.py      # Conservative model-id → provider-family inference (pure stdlib); None = unknown = silent. Feeds the validity block's same-family caveat
  dataset_audit.py       # Deterministic dataset audit (contamination, duplicates, composition, branch coverage) + soft execution-path preflight; dataset_audit.yaml/manifest.yaml live at the dataset root
  agent/
    base.py              # EvalRunner ABC + RunResult
    claude_code.py       # Claude Code CLI runner (claude --print)
    codex.py             # OpenAI Codex CLI runner (codex exec --json)
    cli_runner.py        # Opaque CLI runner (arbitrary command templates)
    null.py              # Null (do-nothing) runner: dataset solvability probe (--agent null; rejected as runner.type at config load)
    stream_capture.py    # Stream-json processing (events, timestamps, usage, hooks)
  harbor/                # Harbor integration (containerized execution)
    tasks.py             # Generate self-contained Harbor task packages from eval.yaml
    reward.py            # Judge → Harbor reward.json bridge (in-container verifier)
    run.py               # /eval-run --runner harbor orchestration
    results.py           # Parse Harbor job dirs into per-case results
    podman.py            # Podman BaseEnvironment (local containers)
    kubernetes.py        # Kubernetes BaseEnvironment (OpenShift, Python client)
    templates/           # task.toml, instruction.md, test.sh templates
  tools/
    interception.py      # Shared tool interception generation (workspace + Harbor)
  judges/                # Builtin judges (auto-discovered by category)
  prompts/               # Builtin generation prompts (synthetic generation)
    __init__.py          # BuiltinPromptRegistry + resolve_seed_prompt
    docs/                # navigation, anti-pattern, authoring, component-usage, architecture
  mlflow/
    experiment.py        # MLflow experiment setup, server check, feedback logging
    datasets.py          # Dataset create/sync utilities
    traces.py            # Trace search and input extraction
    trace_builder.py     # Hierarchical trace builder (stream-json → MLflow trace)
  cli/
    trace_run.py         # claude-trace CLI (standalone skill tracing)
  anova/                 # eval-anova: DoE matrix testing + ANOVA statistics
    matrix.py            # Factorial (DoE) experiment design + cost estimation
    composite.py         # Composite scoring helpers (bool gates + numeric; aggregate)
    archive.py           # Git-backed results archival (ResultsArchiver)
    stats/
      anova.py           # Repeated-measures, mixed-effects, one-way ANOVA
      pareto.py          # Cost/quality Pareto frontier

skills/eval-setup/       # Skill: environment setup
  SKILL.md               # Dependencies, MLflow, API keys, directories
  scripts/
    check_env.py         # Preflight environment checks

skills/eval-analyze/     # Skill: bootstrap eval config
  SKILL.md               # Analyze skill or docs, generate eval.yaml + eval.md
  scripts/
    find_skills.py       # Skill discovery (reads plugin.json for paths)
    validate_eval.py     # Config and memory validation
    resolve_prompt.py    # Resolve prompt file paths
    assess_skills.py     # --assess: extract per-skill facts (LLM judges eval-worthiness)
  prompts/
    analyze-skill.md     # Skill analysis prompt (skill mode)
    generate-eval-md.md  # eval.md generation prompt
    assess-skills.md     # --assess classification (RECOMMENDED/OPTIONAL/SKIP/EXISTS)
  references/
    eval-yaml-template.md # Full eval.yaml template for generation

skills/eval-dataset/     # Skill: generate test cases
  SKILL.md               # Bootstrap, expand, or extract cases from traces (skill mode)
                         # OR synthetic generation from seeds (prompt mode)
  scripts/
    generate_synthetic.py # Synthetic test case generation (prompt mode); writes manifest.yaml provenance; --force replaces the case-NNN set
    audit_dataset.py     # CLI: deterministic dataset audit → dataset_audit.yaml at the dataset root (agent_eval/dataset_audit.py engine) + --null-run probe audit
    list_prompts.py      # List builtin generation prompts (from agent_eval/prompts/)
    harbor.py            # CLI: generate Harbor task packages (thin wrapper → harbor.tasks)
  # Builtin generation prompts live in agent_eval/prompts/ (like builtin judges)

skills/eval-run/         # Skill: execute eval suite
  SKILL.md               # Prepare, execute, collect, score, report
  scripts/
    workspace.py         # Workspace creation, batch.yaml, symlinks
    execute.py           # Skill execution via agent runner
    collect.py           # Artifact collection + case mapping
    score.py             # Scoring: inline checks, LLM judges, pairwise, regression
    report.py            # HTML report generation (scoring summary, per-case details, diffs)
    tools.py             # PreToolUse hook for tool interception
  prompts/
    analyze-results.md   # Results interpretation prompt
    comparison-judge.md  # Pairwise comparison judge prompt
  references/
    data-pipeline.md     # Dataset → workspace → execution → scoring flow
    tool-interception.md # Tool interception format and field reference

skills/eval-review/      # Skill: interactive human review
  SKILL.md               # Present results, collect feedback, propose changes
  prompts/
    review-results.md    # Analysis framework for feedback patterns

skills/eval-mlflow/      # Skill: MLflow integration
  SKILL.md               # Dataset sync, result logging, trace feedback
  scripts/
    sync_dataset.py      # Push cases to MLflow dataset registry
    log_results.py       # Log run params, metrics, artifacts to MLflow
    attach_feedback.py   # Push/pull feedback between harness and traces
    from_traces.py       # Extract inputs from production traces

skills/eval-optimize/    # Skill: automated refinement loop
  SKILL.md               # Composes with /eval-run via Skill tool

skills/eval-compare/     # Skill: cross-model/cross-run comparison report
  SKILL.md               # Discover runs, generate tabbed HTML, LLM analysis sections
  scripts/
    compare.py           # Discover runs + comparison HTML; renders anova.json stats if present

skills/eval-anova/       # Skill: DoE/ANOVA matrix experiments (orchestrator over eval-run)
  SKILL.md               # matrix: → fan out /eval-run per cell → anova.json → /eval-compare
  QUICKSTART.md          # From-scratch setup and run steps
  scripts/
    orchestrate.py       # Runnable entrypoint: fan-out loop, --dry-run, --analyze-only
    analyze.py           # analyze_runs: ANOVA + Pareto over runs' summary.yaml → anova.json
    design.py            # Factorial design + cost estimation helpers
    report.py            # Deep ANOVA detail report from anova.json
  references/
    matrix-schema.md     # Full matrix: config schema reference

skills/eval-check/ # Skill: full-harness configuration health check
  SKILL.md               # Scans all skills, commands, CLAUDE.md, hooks for overlap and issues
  scripts/
    harness_inventory.py # Project artifact discovery, word counting, eval config discovery
    reference_checker.py # Cross-component reference validation (broken refs, missing scripts, orphans)
```

### Adding a skill (or a script to an existing one)

Skill scripts run as `python3 ${CLAUDE_SKILL_DIR}/scripts/foo.py`, so `sys.path[0]`
is the script's own directory — not the plugin root. Two things are therefore
required, and both have been forgotten before:

1. **Symlink** `scripts/agent_eval -> ../../../agent_eval` in the new
   `skills/<skill>/scripts/` dir. Without it `import agent_eval` raises
   ModuleNotFoundError.
2. **`import agent_eval._bootstrap  # noqa: F401 — auto-activate venv` as the first
   import** in any script that uses a third-party package (`yaml`, `mlflow`,
   `anthropic`, `jinja2`, `pandas`, …) or imports `agent_eval`. It puts
   `.eval-venv`'s site-packages on `sys.path`, or re-execs into the venv
   interpreter on an ABI mismatch. It must precede the third-party imports —
   including ones deferred inside functions, which still need the venv when they run.

Stdlib-only scripts need neither. `scripts/ensure_deps.py` is exempt: it *creates*
the venv. Modules under `agent_eval/` that SKILL.md invokes directly by path count
as entry points too (see `agent_eval/state.py` for the `__package__` guard that case
needs). Spawning a python child? Strip `_AGENT_EVAL_BOOTSTRAP_DONE` from its env —
the sentinel is for surviving `os.execv` within one process, and a child has to
activate the venv itself (`orchestrate.py::_child_env`).

`tests/test_venv_activation.py` enforces all of this, so CI catches a miss.

## How It Works

Projects create an `eval.yaml` config file with:
- `execution` — `mode` (`case` or `batch`), `skill` or `prompt` (mutually exclusive; skill mode vs prompt mode), `arguments` template with `{field}` placeholders, optional `timeout`/`max_budget_usd`/`parallelism` (concurrent case execution), and `env` for injecting environment variables into workspaces (`$VAR` syntax resolves from caller's env). A top-level `skill:` is still accepted (auto-normalized to `execution.skill`) but deprecated.
- `runner` — `type` discriminator (`claude-code`, etc.) plus runner-specific `effort`/`settings`/`plugin_dirs`/`env`/`system_prompt`
- `models` — defaults for `skill`/`subagent`/`judge`/`hook` roles (CLI flags override). `hook` is the model for LLM-based AskUserQuestion answering. `hook_shadow` (max 2 model ids, distinct, none equal to `hook`; cross-family via gateway aliases) adds cross-family shadow simulators: every intercepted question is ALSO answered by each shadow — logged to the hook_answers ledger (`shadows` entries), never injected, skipped first when the in-hook deadline budget is tight — feeding `summary['simulator'].cross_simulator` and the `thresholds.simulator.min_cross_simulator_agreement` gate. Costs up to 2 extra LLM calls per intercepted question.
- `mlflow` — `experiment`, optional `tracking_uri`/`tags`
- `permissions` — `allow`/`deny` tool patterns for headless execution
- `dataset` — `path` to test cases directory, `schema` describing case structure in natural language. Optional `workspace.files`: a whitelist of relative paths inside each case directory to copy into the agent workspace (directory entries copy recursively; unlisted files stay behind; ignored in batch mode).
- `generation` — optional. `strategy` selects case provenance: `skill` (default — agent authors from skill analysis; needs no block), `synthetic` (LLM generates from seeds), or `from-traces` (extracted from MLflow traces). For `synthetic`: `context` holds repository knowledge injected into every prompt, and `seeds` is a list where each has a `category`, a `count`, and exactly one prompt discriminator (mirroring judges): `builtin` (from `agent_eval/prompts/`, e.g. `docs/navigation`), `prompt_file` (project path), or inline `prompt`. Each seed's `category` is stamped onto cases as `annotations.category` (derived, never declared). `seeds`/`context` apply only to `synthetic`. Whether `/eval-dataset` creates a fresh set or augments is derived from the existing dataset — there is no `--strategy` flag.
- `inputs.tools` — tool interception: `match` describes what to intercept, `prompt` how to handle it. AskUserQuestion uses 3-tier answering: exact `case_overrides` → LLM call (`models.hook`) with case context (`input.yaml` + `answers.yaml`) → fallback. Every intercepted question is recorded to a `hook_answers.jsonl` provenance ledger (`tier: override|llm|fallback`, plus `disabled` records when interception silently turns off), exposed to judges as `outputs["hook_answers"]` and gateable via the builtin `process/simulator_provenance` judge. Optional `calibration: true` shadow-runs the LLM tier on every override-answered question — HELD OUT (`answers.yaml` stripped from the shadow's context) and logged into the record's `calibration` object, never injected; `score.py` aggregates the pairs into `summary['simulator']` stratified by case-override provenance (`case_overrides_source: human|agent` file-level, or per-entry `{answer, source}` — unmarked counts as agent). Generated PreToolUse hook entries carry an explicit 120s `timeout`, and the hook enforces an in-hook deadline budget that skips the optional shadow with a `skipped: deadline` record rather than risking an external kill
- `outputs` — list of artifact dirs (`path`) and/or tool calls (`tool`) with natural language schemas. Optional `batch_pattern` maps output files to cases in batch mode using `{n}` as a 1-based index
- `traces` — execution data to capture: stdout/stderr, events, metrics (exit code, tokens, cost)
- `hooks` — optional lifecycle shell hooks around the run: `before_all`/`before_each`/`after_each`/`before_step`/`after_step`/`before_scoring`/`after_all`/`before_report`, each a list of `{command, timeout (default 120), description, on_failure: fail|continue, condition}` entries. Per-case and per-step hooks warn and are skipped in batch mode. `before_each` outputs surface to judges as `outputs["hook_outputs"]`
- `judges` — `builtin` reusable judges (from `agent_eval/judges/`), inline `check` scripts, LLM `prompt`/`prompt_file` (Jinja2 rendered), `llm_rubric`, external `module`/`function`. Optional `arguments` dict for parameterization. Optional `if` condition to skip judges per case based on annotations. Optional `samples: N` (default 1) runs a stochastic (LLM/agent) judge N times per case and reduces — median_low for numeric, strict majority for bool (ties fail); ignored with a warning on deterministic judges. Judges receive `outputs["annotations"]` from dataset `annotations.yaml`. `feedback_type: bool` selects a pass/fail verdict, anything else a numeric `score` on `score_range` — a declared range is stated in the LLM judge's prompt and tool schema and enforced on the returned value of every judge type (off-scale → error sample, dropped from the mean, never clamped), and omitting it on a numeric LLM/agent judge warns at config load. Rejected at load: `bool` with a `score_range`, `int` with fractional bounds, a non-`bool` `feedback_type` or any `score_range` on a **builtin LLM** judge, unknown `builtin:` names. Optional `consequence: exploratory|safety|gating` tags a judge with a reliability tier: it injects a default `min_alpha` (0.67/0.70/0.80) at detection time via `effective_thresholds()` — an explicit `min_alpha` wins, `config.thresholds` is never mutated, and only 0.67 is literature-backed (0.70/0.80 are author-proposed). Warns at load when the judge cannot produce IRR data (`samples: 1` on an LLM/agent judge, any builtin LLM judge, deterministic judges). `model` accepts a string or a LIST of 2-4 model ids — a **judge panel**: the call fans out per model (`samples` applies per model), each model's draws reduce first, then a majority (bool) / median_low (numeric) over the per-model reduced verdicts becomes the case value; scoring adds a cross-model Krippendorff alpha + family composition (`panel` on the judge aggregate). Panels are valid only on LLM judges (rejected on check/builtin/module/agent); non-Anthropic members are gateway aliases via an Anthropic-Messages-compatible proxy on `ANTHROPIC_BASE_URL` — with a bare Anthropic key a panel is within-family and labeled as such. Consequence tiers inject `min_alpha` ONLY: a consequence-tagged panel judge without an explicit `min_panel_alpha` warns at load. Load also emits a one-shot Appendix-B.4 same-family advisory when reliability features are engaged (a panel, a consequence tag, or models.hook_shadow) and >=2 configured models all resolve to one known provider family. `score.py clarity --run-id <id> --config <cfg> --raters m1,m2,m3` is the instrument-clarity diagnostic (Sec 10.2): rater models re-rate a deterministic case subsample with the judge's own rubric, m-way alpha vs the 0.67 exploratory floor — report-only, never a CI gate.
- `thresholds` — per-judge regression detection. Valid keys: `min_mean`, `min_pass_rate`, `min_win_rate`, `max_error_rate` (opt-in coverage gate — the other three are computed over the cases that produced a value, so errors shrink the sample rather than lowering the metric), `min_alpha` (gates the single-judge self-consistency alpha — an upper bound on inter-rater reliability — computed over the sampling matrix when `samples > 1`; the perfect-agreement degenerate passes, while configured-but-unavailable — `samples: 1`, all-errored, insufficient data — is a regression; skipped with a notice on the Harbor/EvalHub paths, whose aggregation carries no stability data), `min_human_agreement` (gates the post-hoc judge-vs-human kappa/alpha merged by `score.py calibration` from `/eval-review` verdicts; a never-calibrated judge is silently skipped, the perfect-agreement degenerate passes, and a judge the run-level `human_calibration` block lists but whose row lost `human_agreement` — a re-score rewrote `summary['judges']` — is a loud stale-calibration regression), `min_panel_alpha` (gates a panel judge's cross-model alpha; same three-state semantics as `min_alpha` — perfect-agreement matrices pass, configured-but-unavailable regresses — and the same Harbor/EvalHub skip-with-notice; note panels still execute in-container on Harbor at m× judge cost while the cross-case alpha is not aggregated there yet; never tier-injected — consequence tiers inject `min_alpha` only). Unknown keys warn at load; `*_alpha`/`*_agreement` values must be finite and ≤ 1.0. The mapping key `simulator` is RESERVED (never a judge name — a judge named `simulator` next to the block is a load error, alone it's a DeprecationWarning): it gates the run-level `summary['simulator']` block aggregated from the hook_answers ledgers, with sub-keys `max_fallback_rate` (arbitrary/uninterceded answer share), `min_gold_agreement` (held-out shadow-vs-override agreement over HUMAN-provenance pairs ONLY — zero human pairs regresses loudly; configured-but-no-block also regresses), and `min_cross_simulator_agreement` (gates the `cross_simulator.all_agree_rate` recorded by `models.hook_shadow` shadow simulators — configured with no shadow answers is a regression, and a breach on a single-family shadow panel says so in the detail); stripped with a notice on the Harbor/EvalHub paths (no ledger aggregation there)
- `reward` — optional. Collapses per-judge results into a single scalar in `[0, 1]` for RL training (GRPO). Two mutually exclusive modes: `judge` names a single judge whose value is the reward (clamped to `[0, 1]` as-is, or set `normalize: true` to map it from `score_range`; `gate` defaults to false here), validated against the defined judges at config load. Otherwise `formula` composes from multiple judges: `weighted` (weighted sum of `weights`) or a Python `<expression>` over judge names (allowed calls: `min`/`max`/`abs`/`round`/`sum`/`len`/`mean`, AST-validated at config load). `gate: true` zeros the reward on any false boolean judge — it gates on *every* boolean judge, so expressions using booleans as their own gate want `gate: false`. Resolution order: `reward:` section → default (boolean gates + averaged normalized numerics). On both paths a numeric judge is normalized over its own declared `score_range` — except `raw` judges and a single `judge` without `normalize`, which are clamped instead. The deprecated `reward.score_range` only covers composed judges that declare no range of their own (`[1, 5]` if it too is absent), and warns at load once one of them declares a different range.

Runs are stored in `$AGENT_EVAL_RUNS_DIR` (default `eval/runs`), configured during `/eval-setup`.

The `schema` descriptions are documentation for the LLM agents and judges. Scripts operate on file paths from eval.yaml directly — no extraction spec, no hardcoded field names.

## Usage

### Skill Mode Workflow
```
/eval-setup                            # Setup: dependencies, MLflow, API keys
/eval-analyze --skill my-skill         # Analyze: understand skill, generate eval.yaml
/eval-dataset                          # Dataset: generate test cases
/eval-run --model opus                 # Run: execute eval suite
/eval-review --run-id <id>             # Review: interactive human feedback + changes
/eval-mlflow --run-id <id>             # MLflow: sync dataset, log results
/eval-optimize --model opus            # Optimize: automated refinement loop
/eval-compare <input-dir>              # Compare: tabbed HTML report across models/runs
/eval-anova --config eval.yaml         # DoE: matrix of configs → runs → ANOVA + comparison
```

### eval-anova / eval-compare relationship

eval-run runs one condition. `/eval-anova` reads a `matrix:` block and fans out `/eval-run`
per cell (condition × replication) → standard runs, then computes ANOVA/Pareto over their
`summary.yaml` into `anova.json`. `/eval-compare` renders the cross-condition comparison and
surfaces the statistics section when `anova.json` is present — but stays standalone (no stats
dependency) for manual/CI-produced run sets. Per-judge reliability (`stability.irr`, `panel`,
`human_agreement`) lives in each run's own `summary.yaml` — neither analyze.py nor compare.py
aggregates it across cells; a cross-replication ICC is an explicitly deferred follow-up
(`agent_eval/anova/stats/test_retest.py`, never a second reliability.py).

### Prompt Mode Workflow (Agentic Documentation Testing)
```text
/eval-setup                            # Setup: dependencies, MLflow, API keys
/eval-analyze --prompt examples/openshift-agentic-docs.md # Analyze: analyze docs, generate synthetic-generation eval.yaml
/eval-dataset                          # Dataset: generate test cases from templates
/eval-run --model sonnet               # Run: test agent against documentation
/eval-review --run-id <id>             # Review: analyze documentation effectiveness
/eval-mlflow --run-id <id>             # MLflow: sync dataset, log results
```

## Tests

```bash
python3 -m pytest tests/ -v                # Unit tests only (e2e skipped by default)
python3 -m pytest tests/e2e/ -v -s -m e2e  # E2E tests (requires ANTHROPIC_API_KEY, ~$0.50)
python3 -m pytest tests/ -v -s -m ""       # Everything
```

E2E tests invoke real Claude API calls against a fake Jira skill fixture to verify the eval-analyze/eval-dataset pipeline. Use `-s` to see real-time progress from the runner.

## Key Design Decisions

1. **Schema-driven** — dataset and output structures described in natural language in eval.yaml; agents and judges interpret them, scripts just move files
2. **Agent-agnostic runner** — `EvalRunner` ABC with `--agent` flag on execute.py; Claude Code included, extensible to OpenCode/Agent SDK
3. **Four judge types** — `builtin` reusable judges, inline `check` scripts, LLM `prompt`/`prompt_file`, external `module`/`function`
4. **MLflow as separate skill** — `/eval-mlflow` handles dataset sync, result logging, trace feedback; eval-run works without it

## Brainstorms

The `brainstorm/` directory contains exploratory ideas and design thinking. These are just ideas, not reflected in the code. Do not treat brainstorm content as implemented features or current architecture. Implemented brainstorms are moved to `brainstorm/attic/`.

## Remaining Work

- CI integration patterns and examples
- Measurement-validity follow-ups (deferred from spec 012): v1-3 construct-fidelity screen, pairwise verdict-alpha + `min_pairwise_alpha` gate, cross-replication ICC for eval-anova (`agent_eval/anova/stats/test_retest.py`), simulator `samples: k` self-consistency draws

## Execution Paths

The same `eval.yaml` works unchanged across three parallel execution paths.
The execution substrate is a CLI flag, never in the eval config.

### Local (`/eval-run` or `agent-eval run`)
Process-level execution — the harness invokes the agent CLI directly, collects
artifacts, scores with judges, generates the report. No containers.

### Harbor (`harbor run` or `/eval-run --runner harbor`)
Containerized execution via [Harbor](https://github.com/laude-institute/harbor).
Self-contained task packages (from `/eval-dataset`) carry instruction, inputs,
tool interception, and the verifier (judge engine as `reward.json`). Any Harbor
agent (claude-code, opencode, codex, etc.) runs them directly — no custom agent
wrapper. Environments: Podman (local) and Kubernetes (OpenShift). See
`deploy/harbor/README.md`.

### EvalHub (platform-triggered)
The `agent_eval.evalhub` adapter runs the eval **in-process** inside the Job pod
created by EvalHub's server — matching EvalHub's architecture where adapter pods
are execution-only (no sub-pod creation). Uses `ClaudeCodeRunner` directly, not
Harbor. In-process parallelism handles concurrent cases within the pod.

- Implements `FrameworkAdapter` from `eval-hub-sdk`
- Downloads test cases from S3 via `s3_dataset.py`
- Maps `RunResult` + judge scores to `JobResults` via `results_mapper.py`
- Ships as a UBI9 container image (`deploy/evalhub/Containerfile`)

## Container Images

| Image | Containerfile | Used by |
|---|---|---|
| `agent-eval-harness` | `deploy/Containerfile` | Trial pods (Harbor), EvalHub Job pods (base) |
| `agent-eval-hub` | `deploy/evalhub/Containerfile` | EvalHub provider (FROM base + eval-hub-sdk) |

No project-specific images needed. Project resources via ConfigMap (K8s),
bind-mount (Podman), or `FROM agent-eval-harness` in the project's own repo.

<!-- SPECKIT START -->
For additional context about the current feature work, read `specs/005-eval-directory-layout/plan.md`
<!-- SPECKIT END -->
