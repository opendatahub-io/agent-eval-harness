# Agent Eval Harness

Generic evaluation framework for Claude Code skills projects. Uses MLflow as the backbone for tracing, evaluation, datasets, and reporting.

## Project Status

Phase 1 (core framework), Phase 2 (scoring integration), and Phase 3 (eval-anova DoE/ANOVA) are implemented. See `eval/plans/agent-eval-harness-design.md` in the rfe-creator project for the full design doc.

## Architecture

```
agent_eval/              # Python package (config, runner, state)
  config.py              # EvalConfig from eval.yaml
  state.py               # Shared state persistence (key-value store)
  agent/
    base.py              # EvalRunner ABC + RunResult
    claude_code.py       # Claude Code CLI runner (claude --print)
    cli_runner.py        # Opaque CLI runner (arbitrary command templates)
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
  mlflow/
    experiment.py        # MLflow experiment setup, server check, feedback logging
    datasets.py          # Dataset create/sync utilities
    traces.py            # Trace search and input extraction
    trace_builder.py     # Hierarchical trace builder (stream-json → MLflow trace)
  cli/
    trace_run.py         # claude-trace CLI (standalone skill tracing)
  matrix.py              # Factorial experiment design + cost estimation
  composite.py           # Composite scoring (bool gates + numeric)
  anova_runner.py        # Bridge: eval-anova → eval-run execution + scoring
  archive.py             # Git-backed results archival
  stats/
    anova.py             # Repeated-measures, mixed-effects, one-way ANOVA
    pareto.py            # Cost/quality Pareto frontier

skills/eval-setup/       # Skill: environment setup
  SKILL.md               # Dependencies, MLflow, API keys, directories
  scripts/
    check_env.py         # Preflight environment checks

skills/eval-analyze/     # Skill: bootstrap eval config
  SKILL.md               # Analyze skill, generate eval.yaml + eval.md
  scripts/
    find_skills.py       # Skill discovery (reads plugin.json for paths)
    validate_eval.py     # Config and memory validation
  prompts/
    analyze-skill.md     # Skill analysis prompt
    generate-eval-md.md  # eval.md generation prompt
  references/
    eval-yaml-template.md # Full eval.yaml template for generation

skills/eval-dataset/     # Skill: generate test cases
  SKILL.md               # Bootstrap, expand, or extract cases from traces
  scripts/
    harbor.py            # CLI: generate Harbor task packages (thin wrapper → harbor.tasks)

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

skills/eval-anova/       # Skill: DoE/ANOVA experiments
  SKILL.md               # Full-factorial matrix design → run → analyze → report
  QUICKSTART.md          # From-scratch setup and run steps
  scripts/
    orchestrate.py       # Cell execution, condition application, preflight
    analyze.py           # ANOVA analysis + archival
    design.py            # Interactive experiment design + cost estimation
    report.py            # Per-run ANOVA detail + pooled model comparison HTML
  prompts/
    interpret-anova.md   # ANOVA results interpretation prompt
  references/
    matrix-schema.md     # Full matrix: config schema reference

skills/eval-check/ # Skill: full-harness configuration health check
  SKILL.md               # Scans all skills, commands, CLAUDE.md, hooks for overlap and issues
  scripts/
    harness_inventory.py # Project artifact discovery and word counting

eval/                    # Committed benchmarks and reproducers
  harbor-maas-v1/        # SWE-bench-style ANOVA benchmark (4 PR tasks)
    README.md            # Dataset provenance, usage, results
    eval.yaml            # Judges, thresholds, matrix config
    driver.py            # Smoke + full matrix reproducer
    dataset/             # 4 task dirs (input.yaml, oracle.diff, annotations.yaml)
  runs/                  # Ephemeral run outputs (gitignored)
```

## How It Works

Skills projects create an `eval.yaml` config file with:
- `skill` — skill to evaluate
- `execution` — `mode` (`case` or `batch`), `arguments` template with `{field}` placeholders, optional `timeout`/`max_budget_usd`/`parallelism` (concurrent case execution), and `env` for injecting environment variables into workspaces (`$VAR` syntax resolves from caller's env)
- `runner` — `type` discriminator (`claude-code`, etc.) plus runner-specific `effort`/`settings`/`plugin_dirs`/`env`/`system_prompt`
- `models` — defaults for `skill`/`subagent`/`judge`/`hook` roles (CLI flags override). `hook` is the model for LLM-based AskUserQuestion answering.
- `mlflow` — `experiment`, optional `tracking_uri`/`tags`
- `permissions` — `allow`/`deny` tool patterns for headless execution
- `dataset` — `path` to test cases directory, `schema` describing case structure in natural language
- `inputs.tools` — tool interception: `match` describes what to intercept, `prompt` how to handle it. AskUserQuestion uses 3-tier answering: exact `case_overrides` → LLM call (`models.hook`) with case context (`input.yaml` + `answers.yaml`) → fallback
- `outputs` — list of artifact dirs (`path`) and/or tool calls (`tool`) with natural language schemas. Optional `batch_pattern` maps output files to cases in batch mode using `{n}` as a 1-based index
- `traces` — execution data to capture: stdout/stderr, events, metrics (exit code, tokens, cost)
- `judges` — `builtin` reusable judges (from `agent_eval/judges/`), inline `check` scripts, LLM `prompt`/`prompt_file` (Jinja2 rendered), external `module`/`function`. Optional `arguments` dict for parameterization. Optional `if` condition to skip judges per case based on annotations. Judges receive `outputs["annotations"]` from dataset `annotations.yaml`.
- `thresholds` — per-judge regression detection. Valid keys: `min_mean`, `min_pass_rate`, `min_win_rate`
- `matrix` — full-factorial experiment design for `/eval-anova`. `factors` (named lists of levels), `replications`. Factor `model` maps to the runner's model; `effort` maps to thinking effort level; other factors pass through as runner kwargs.
- `reward` — optional. Collapses per-judge results into a single scalar in `[0, 1]` for RL training (GRPO). Two mutually exclusive modes: `judge` names a single judge whose value is the reward (clamped to `[0, 1]` as-is, or set `normalize: true` to map it from `score_range`; `gate` defaults to false here), validated against the defined judges at config load. Otherwise `formula` composes from multiple judges: `weighted` (weighted sum of `weights`) or a Python `<expression>` over judge names (allowed calls: `min`/`max`/`abs`/`round`/`sum`/`len`/`mean`, AST-validated at config load). `gate: true` zeros the reward on any false boolean judge — it gates on *every* boolean judge, so expressions using booleans as their own gate want `gate: false`. Resolution order: `reward:` section → default (boolean gates + averaged normalized numerics).

Runs are stored in `$AGENT_EVAL_RUNS_DIR` (default `eval/runs`), configured during `/eval-setup`.

The `schema` descriptions are documentation for the LLM agents and judges. Scripts operate on file paths from eval.yaml directly — no extraction spec, no hardcoded field names.

## Usage

```
/eval-setup                            # Setup: dependencies, MLflow, API keys
/eval-analyze --skill my-skill         # Analyze: understand skill, generate eval.yaml
/eval-dataset                          # Dataset: generate test cases
/eval-run --model opus                 # Run: execute eval suite
/eval-review --run-id <id>             # Review: interactive human feedback + changes
/eval-mlflow --run-id <id>             # MLflow: sync dataset, log results
/eval-optimize --model opus            # Optimize: automated refinement loop
/eval-anova                            # Compare: models × effort × cases → ANOVA
```

## Setup

```
pip install -e "."                 # Core only
pip install -e ".[anthropic]"      # + LLM judges
pip install -e ".[anova]"          # + eval-anova (scipy, statsmodels, pandas, pingouin)
pip install -e ".[all]"            # Everything
```

## Tests

```
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
5. **eval-anova reuses eval-run** — `anova_runner.py` dynamically loads eval-run's `score.py` and `collect.py` modules; no duplication of execution or scoring logic
6. **Composite scoring** — boolean judges are gates (fail → score 0); numeric judges are weighted-averaged to [0,1]; separation prevents pass/fail from diluting quality scores

## Brainstorms

The `brainstorm/` directory contains exploratory ideas and design thinking. These are just ideas, not reflected in the code. Do not treat brainstorm content as implemented features or current architecture. Implemented brainstorms are moved to `brainstorm/attic/`.

## Remaining Work

- CI integration patterns and examples
- `traces.events` implementation — parse stream-json into structured `outputs["events"]` for judges

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
