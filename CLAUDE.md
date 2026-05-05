# Agent Eval Harness

Generic evaluation framework for Claude Code skills projects. Uses MLflow as the backbone for tracing, evaluation, datasets, and reporting.

## Project Status

Phase 1 (core framework) and Phase 2 (scoring integration) are implemented. See `eval/plans/agent-eval-harness-design.md` in the rfe-creator project for the full design doc.

## Architecture

```
agent_eval/              # Python package (config, runner, state)
  config.py              # EvalConfig from eval.yaml
  state.py               # Shared state persistence (key-value store)
  agent/
    base.py              # EvalRunner ABC + RunResult
    claude_code.py       # Claude Code CLI runner (claude --print)
    stream_capture.py    # Stream-json processing (events, timestamps, usage, hooks)
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
```

## How It Works

Skills projects create an `eval.yaml` config file with:
- `skill` — skill to evaluate
- `execution` — `mode` (`case` or `batch`), `arguments` template with `{field}` placeholders, optional `timeout`/`max_budget_usd`/`parallelism` (concurrent case execution), and `env` for injecting environment variables into workspaces (`$VAR` syntax resolves from caller's env)
- `runner` — `type` discriminator (`claude-code`, etc.) plus runner-specific `effort`/`settings`/`plugin_dirs`/`env_strip`/`system_prompt`
- `models` — defaults for `skill`/`subagent`/`judge`/`hook` roles (CLI flags override). `hook` is the model for LLM-based AskUserQuestion answering.
- `mlflow` — `experiment`, optional `tracking_uri`/`tags`
- `permissions` — `allow`/`deny` tool patterns for headless execution
- `dataset` — `path` to test cases directory, `schema` describing case structure in natural language
- `inputs.tools` — tool interception: `match` describes what to intercept, `prompt` how to handle it. AskUserQuestion uses 3-tier answering: exact `case_overrides` → LLM call (`models.hook`) with case context (`input.yaml` + `answers.yaml`) → fallback
- `outputs` — list of artifact dirs (`path`) and/or tool calls (`tool`) with natural language schemas. Optional `batch_pattern` maps output files to cases in batch mode using `{n}` as a 1-based index
- `traces` — execution data to capture: stdout/stderr, events, metrics (exit code, tokens, cost)
- `judges` — inline `check` scripts, LLM `prompt`/`prompt_file`, external `module`/`function`. Optional `if` condition to skip judges per case based on annotations. Judges receive `outputs["annotations"]` from dataset `annotations.yaml`.
- `thresholds` — per-judge regression detection. Valid keys: `min_mean`, `min_pass_rate`, `min_win_rate`

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
3. **Three judge types** — inline `check` scripts, LLM `prompt`/`prompt_file`, external `module`/`function`
4. **MLflow as separate skill** — `/eval-mlflow` handles dataset sync, result logging, trace feedback; eval-run works without it

## Remaining Work

- CI integration patterns and examples
- `traces.events` implementation — parse stream-json into structured `outputs["events"]` for judges

## EvalHub Integration

The `agent_eval.evalhub` package provides a custom EvalHub provider that runs
agent skill evaluations on Red Hat OpenShift AI. The adapter:

- Implements `FrameworkAdapter` from `eval-hub-sdk`
- Downloads test cases from S3 via `s3_dataset.py`
- Translates `eval.yaml` to EvalHub provider definitions via `config_translator.py`
- Maps `RunResult` + judge scores to `JobResults` via `results_mapper.py`
- Ships as a UBI9 container image (`provider/Containerfile`)

### Local skills (unchanged)
eval-analyze, eval-dataset, eval-optimize, eval-review — authoring workflows

### EvalHub-managed (via provider)
Execution, result storage, regression detection, OCI export (MLflow tracking is optional)


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
