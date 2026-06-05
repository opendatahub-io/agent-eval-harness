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

Prompt mode is extensible to any agent evaluation scenario:
- Documentation effectiveness: Can agents navigate and use your docs?
- Pattern understanding: Can agents identify and apply code patterns?
- Constraint compliance: Do agents respect documented rules?
- API usage: Can agents correctly use APIs from documentation alone?
- Code generation from specifications
- Reasoning trace quality assessment

Prompt mode provides direct agent invocation with taxonomy-based test generation, LLM rubric judges, and flexible evaluation criteria.

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

skills/eval-setup/       # Skill: environment setup
  SKILL.md               # Dependencies, MLflow, API keys, directories
  scripts/
    check_env.py         # Preflight environment checks

skills/eval-analyze/     # Skill: bootstrap eval config
  SKILL.md               # Analyze skill or docs, generate eval.yaml + eval.md
  scripts/
    find_skills.py       # Skill discovery (reads plugin.json for paths)
    validate_eval.py     # Config and memory validation
    resolve_prompt.py    # Resolve builtin: prompt references
  prompts/
    analyze-skill.md     # Skill analysis prompt (skill mode)
    analyze-docs.md      # Documentation analysis prompt (prompt mode - builtin:docs)
    generate-eval-md.md  # eval.md generation prompt
  references/
    eval-yaml-template.md # Full eval.yaml template for generation

skills/eval-dataset/     # Skill: generate test cases
  SKILL.md               # Bootstrap, expand, or extract cases from traces (skill mode)
                         # OR taxonomy-based generation from templates (prompt mode)
  scripts/
    generate_from_taxonomy.py # Taxonomy-based test case generation (prompt mode)
    harbor.py            # CLI: generate Harbor task packages (thin wrapper → harbor.tasks)
  templates/builtin/     # Builtin test category templates (prompt mode)
    navigation.md        # Finding documentation
    anti-pattern.md      # Rejecting constraint violations
    authoring.md         # Creating content following patterns
    component-usage.md   # API/component usage with examples
    architecture.md      # System design and component interactions
    README.md            # Template documentation

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

skills/eval-check/ # Skill: full-harness configuration health check
  SKILL.md               # Scans all skills, commands, CLAUDE.md, hooks for overlap and issues
  scripts/
    harness_inventory.py # Project artifact discovery and word counting
```

## How It Works

Projects create an `eval.yaml` config file with:
- `skill` — skill to evaluate (required for skill mode, omitted for prompt mode)
- `execution` — `mode` (`case` or `batch`), `skill` or `prompt` (mutually exclusive), `arguments` template with `{field}` placeholders, optional `timeout`/`max_budget_usd`/`parallelism` (concurrent case execution), and `env` for injecting environment variables into workspaces (`$VAR` syntax resolves from caller's env)
- `runner` — `type` discriminator (`claude-code`, etc.) plus runner-specific `effort`/`settings`/`plugin_dirs`/`env`/`system_prompt`
- `models` — defaults for `skill`/`subagent`/`judge`/`hook` roles (CLI flags override). `hook` is the model for LLM-based AskUserQuestion answering.
- `mlflow` — `experiment`, optional `tracking_uri`/`tags`
- `permissions` — `allow`/`deny` tool patterns for headless execution
- `dataset` — `path` to test cases directory, `schema` describing case structure in natural language. Optional `test_categories` for taxonomy-based test generation (used with `prompt` mode).
- `inputs.tools` — tool interception: `match` describes what to intercept, `prompt` how to handle it. AskUserQuestion uses 3-tier answering: exact `case_overrides` → LLM call (`models.hook`) with case context (`input.yaml` + `answers.yaml`) → fallback
- `outputs` — list of artifact dirs (`path`) and/or tool calls (`tool`) with natural language schemas. Optional `batch_pattern` maps output files to cases in batch mode using `{n}` as a 1-based index
- `traces` — execution data to capture: stdout/stderr, events, metrics (exit code, tokens, cost)
- `judges` — `builtin` reusable judges (from `agent_eval/judges/`), inline `check` scripts, LLM `prompt`/`prompt_file` (Jinja2 rendered), /`llm_rubric`, external `module`/`function`. Optional `arguments` dict for parameterization. Optional `if` condition to skip judges per case based on annotations. Judges receive `outputs["annotations"]` from dataset `annotations.yaml`.
- `thresholds` — per-judge regression detection. Valid keys: `min_mean`, `min_pass_rate`, `min_win_rate`
- `reward` — optional. Collapses per-judge results into a single scalar in `[0, 1]` for RL training (GRPO). Two mutually exclusive modes: `judge` names a single judge whose value is the reward (clamped to `[0, 1]` as-is, or set `normalize: true` to map it from `score_range`; `gate` defaults to false here), validated against the defined judges at config load. Otherwise `formula` composes from multiple judges: `weighted` (weighted sum of `weights`) or a Python `<expression>` over judge names (allowed calls: `min`/`max`/`abs`/`round`/`sum`/`len`/`mean`, AST-validated at config load). `gate: true` zeros the reward on any false boolean judge — it gates on *every* boolean judge, so expressions using booleans as their own gate want `gate: false`. Resolution order: `reward:` section → default (boolean gates + averaged normalized numerics).

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
```

### Prompt Mode Workflow (Agentic Documentation Testing)
```text
/eval-setup                            # Setup: dependencies, MLflow, API keys
/eval-analyze --prompt builtin:docs    # Analyze: analyze docs, generate taxonomy-based eval.yaml
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
