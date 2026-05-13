# Agent Eval Harness

Generic evaluation framework for agents and skills. Analyze, run, score, and improve skills automatically across different agent harness (Claude Code, OpenCode, Agent SDK).

## Overview

```
                                             ┌──────────────────┐
        ┌──────────────setup────────────────▶│  MLflow Server   │◀────────────┐
        │                                    │ (local / remote) │             │
        │                                    └──┬───────────────┘          sync, log
        │                                    datasets                      feedback
        │                                       │                             │
┌───────┴──────┐  ┌───────────────┐  ┌──────────▼───┐  ┌──────────────┐  ┌────┴───────────┐
│  eval-setup  │─▶│ eval-analyze  │─▶│ eval-dataset │─▶│   eval-run   │─▶│  eval-mlflow   │
│              │  │               │  │              │  │              │  │                │
│ dependencies │  │ analyze skill │  │ generate     │  │ execute eval │  │ sync dataset   │
│ MLflow conf  │  │ gen eval.yaml │  │ test cases   │  │ collect      │  │ log results    │
│ directories  │  │ suggest judges│  │ fill gaps    │  │ score        │  │ traces         │
└──────────────┘  └───────────────┘  └──────────────┘  └──▲──┬─▲──┬───┘  └────────────────┘
                                                          │  │ │  │
                                            ┌─────────────┘  │ │  └────────────┐
                                            │         ┌──────▼─┴─────┐         │
                                            │         │ eval-review  │         │
                                            │         │              │         │
                                            │         │ human review │         │
                                            │         │ feedback     │         │
                                            │         └──────────────┘         │
                                            │                                  │
                                            │        ┌───────────────┐         │
                                            └────────│ eval-optimize │◀────────┘
                                                     │               │
                                                     │ fix skill     │
                                                     │ re-run        │
                                                     └───────────────┘
```

## Quick Start

### 1. Add to your project

Install from the [skills registry](https://github.com/opendatahub-io/skills-registry):

```bash
claude plugin install agent-eval-harness@opendatahub-skills
```

Or clone and load as a local plugin:

```bash
git clone https://github.com/opendatahub-io/agent-eval-harness
pip install -e ./agent-eval-harness
claude --plugin-dir ./agent-eval-harness
```

This makes all eval skills available: `/eval-setup`, `/eval-analyze`, `/eval-dataset`, `/eval-run`, `/eval-review`, `/eval-mlflow`, and `/eval-optimize`.

### 2. Set up environment

```
/eval-setup
```

This checks dependencies, configures MLflow, verifies API keys, and creates directories.

### 3. Analyze your skill

```
/eval-analyze --skill my-skill
```

This examines the skill's SKILL.md, discovers test cases, and generates `eval.yaml` with:
- Natural language `schema` descriptions of your dataset and outputs
- Suggested judges (inline checks + LLM quality assessment)
- Regression thresholds

### 4. Generate test cases (if needed)

```
/eval-dataset
```

Creates 5 starter test cases based on the skill analysis. Skip this if you already have cases.

### 5. Run evaluation

```
/eval-run --model opus
```

This prepares a workspace, runs the skill (headless or interactive), collects artifacts, scores with judges, and reports results.

## eval.yaml

The harness uses natural language to describe evaluation datasets and skills input/output and spawns LLM sub-agents to interpret them.

```yaml
name: my-skill-eval
description: Evaluate the main skill pipeline
skill: my-skill-name

# Execution — how the skill processes test cases (runner-agnostic)
execution:
  mode: case              # case (default) or batch
  arguments: "{prompt}"   # resolved per case from input.yaml fields
  # timeout: 3600            # Wall-clock timeout in seconds per invocation
  # max_budget_usd: 5.0      # Cost cap in USD per invocation
  # parallelism: 3            # Run up to N cases concurrently (case mode only)
  # env:                     # Inject env vars into workspace settings
  #   JIRA_SERVER: http://localhost:8080   # Literal value
  #   JIRA_TOKEN: $JIRA_TOKEN              # $VAR resolved from caller's env

# Runner — agent harness + runner-specific knobs
runner:
  type: claude-code
  # effort: high              # Reasoning effort: low | medium | high | xhigh | max
  # settings: {}              # Arbitrary Claude Code settings merged into workspace
  # plugin_dirs: []           # Directories to load plugins from
  # env_strip: [JIRA_TOKEN]   # Env vars to strip from subprocess
  # system_prompt: |          # Appended to Claude CLI system prompt
  #   Custom instructions for the skill run.

# Models — defaults for each role (CLI flags override)
models:
  skill: claude-opus-4-6
  judge: claude-opus-4-6
  # hook: claude-sonnet-4-6  # Model for LLM-based AskUserQuestion answering

# MLflow logging target (optional)
mlflow:
  experiment: my-skill-eval

# Permissions — tool access during headless execution
permissions:
  allow: []            # Tool patterns to allow (empty = all)
  deny:
    - "mcp__*"         # Block MCP tools during eval

# Dataset — where test cases live and what they look like
dataset:
  path: eval/dataset/cases
  schema: |
    Each case directory contains:
    - input.yaml: YAML file. The 'prompt' field is the main input to
      the skill. Optionally 'context' with additional context.
    - reference.md: Gold standard output for comparison scoring.

# Inputs — tool interception for headless/interactive execution
# AskUserQuestion uses 3-tier answering: exact case_overrides →
# LLM call (models.hook) with input.yaml + answers.yaml context → fallback
inputs:
  tools: []
  # - match: Questions asked to the user via AskUserQuestion.
  #   prompt: |
  #     Answer based on test case context in input.yaml and answers.yaml.
  #     Default to "yes" for confirmations.
  # - match: |
  #     Any interaction with Jira — MCP tools or scripts.
  #   prompt: |
  #     Block production Jira. Only allow test instances.

# Outputs — what the skill produces (files on disk or tool calls)
outputs:
  # File artifacts on disk
  - path: artifacts
    # batch_pattern: "RFE-{n:03d}"  # Map output files to cases in batch mode
    schema: |
      One markdown file per case, named NNN-slug.md where NNN is the
      case number (001, 002, ...).

  # Tool call outputs (for side effects like API calls)
  # - tool: mcp__atlassian__create_issue
  #   schema: |
  #     Creates a Jira issue with title, description, priority.

# Traces — execution data to capture for judges
traces:
  stdout: true     # Capture stdout.log
  stderr: true     # Capture stderr.log
  events: false    # Execution events: tool calls, reasoning, results (verbose)
  metrics: true    # Capture exit code, tokens, cost, duration

# Judges — evaluate output quality
judges:
  # Inline code check
  - name: has_content
    description: |
      Check that the generated output is non-empty and has at least
      100 characters of content.
    check: |
      content = outputs["main_content"]
      if len(content.strip()) < 100:
          return False, f"Output too short ({len(content.strip())} chars)"
      return True, f"Output has {len(content.strip())} chars"

  # LLM judge with inline prompt (conditional — skipped when condition is false)
  - name: output_quality
    if: "not annotations.get('skip_quality', False)"  # Skip based on annotations
    description: |
      Evaluate quality compared to the reference. Score 1-5.
    prompt: |
      Compare the generated output against the reference.
      Consider: completeness, clarity, accuracy, and relevance.
      Score 1-5 where 5 is excellent.

  # LLM judge with prompt file and supplementary context
  # - name: detailed_quality
  #   description: Detailed quality assessment with rubric
  #   prompt_file: eval/prompts/quality-judge.md
  #   context:
  #     - eval/prompts/scoring-rubric.md
  #     - eval/prompts/domain-guidelines.md

  # External code judge (for complex validation)
  # - name: schema_valid
  #   description: Validate output schema
  #   module: eval.judges.schema_checks
  #   function: check_schema

  # Execution efficiency check (uses trace metrics)
  # - name: cost_reasonable
  #   description: Verify cost stays under $0.50 per case
  #   check: |
  #     cost = outputs.get("cost_usd", 0)
  #     if cost and cost > 0.50:
  #         return False, f"Cost ${cost:.2f} exceeds limit"
  #     return True, f"Cost ${cost:.2f}"

  # Tool call check (uses tool outputs)
  # - name: jira_created
  #   description: Verify the skill created a Jira issue
  #   check: |
  #     calls = outputs.get("tool_calls", [])
  #     jira = [c for c in calls if "create_issue" in c.get("name","")]
  #     if not jira:
  #         return False, "No Jira issue created"
  #     return True, "Created issue"

  # Pairwise comparison judge
  # - name: pairwise
  #   description: Compare two runs and pick the better output
  #   prompt_file: eval/prompts/comparison-judge.md
  #   # model: <model-id>   # Optional override; default is models.judge

# Thresholds for regression detection
thresholds:
  output_quality:
    min_mean: 3.5            # Minimum average score
  # has_content:
  #   min_pass_rate: 1.0     # Minimum fraction of cases passing (0.0–1.0)
  # pairwise:
  #   min_win_rate: 0.6      # Minimum pairwise win rate
```

### Key concepts

- **`execution`** — `mode` (`case` or `batch`), `arguments` template, optional `timeout` (wall-clock seconds per invocation), `max_budget_usd` (cost cap per invocation), `parallelism` (run up to N cases concurrently in case mode), and `env` for injecting environment variables into workspaces (`$VAR` syntax resolves from caller's environment). In `case` mode (default), the skill is invoked once per test case with `{field}` placeholders resolved from each case's input.yaml. In `batch` mode, all cases are bundled into batch.yaml for a single invocation.
- **`schema`** — natural language description of structure. Used on `dataset` and each `outputs` entry. Agents and judges read these to understand the data.
- **`inputs.tools`** — tool interception for headless and interactive execution. Each entry has a `match` (what to intercept) and a `prompt` (how to handle it). AskUserQuestion uses 3-tier answering: exact `case_overrides` → LLM call (`models.hook`) with case context (`input.yaml` + `answers.yaml`) → fallback to first option.
- **`outputs`** — two types: `path` for file artifacts on disk, `tool` for tool call side effects (Jira, APIs). Both have `schema` descriptions. Optional `batch_pattern` maps output files to cases in batch mode using `{n}` as a 1-based index (e.g. `"RFE-{n:03d}"` → `RFE-001`, `RFE-002`).
- **`traces`** — execution data to capture: stdout/stderr logs, events (tool calls, reasoning text, results), metrics (exit code, tokens, cost, duration). Available to judges via the `outputs` dict.
- **`check`** — inline Python snippet for deterministic validation. Receives an `outputs` dict with file contents, execution metadata, tool calls, logs, and `annotations` (from dataset `annotations.yaml`). Returns `(bool, str)`.
- **`if`** — optional condition on a judge. Python expression evaluated against `annotations` and `outputs`. When false, the judge is skipped for that case (not counted in pass_rate or mean).
- **`prompt`** / **`prompt_file`** — LLM judge evaluation instructions.
- **`context`** — list of file paths loaded and appended to the LLM judge prompt as supplementary material (rubrics, guidelines, examples).
- **`module`** / **`function`** — external Python code judge for complex validation.
- **`permissions`** — tool access patterns (`allow`/`deny`) for headless execution. Generic across runners — each runner translates to its platform's mechanism.
- **`runner`** — `type` discriminator selects the runner implementation; remaining fields (`effort`, `settings`, `plugin_dirs`, `env_strip`, `system_prompt`) are runner-specific and ignored by other runners.
- **`models`** — `skill`/`subagent`/`judge`/`hook` defaults, overridable per-judge or via CLI flags. `hook` is the model used for LLM-based AskUserQuestion answering.
- **`mlflow`** — `experiment` (and optional `tracking_uri`/`tags`) for result logging.
- **`thresholds`** — per-judge regression detection. Valid keys: `min_mean` (minimum average score), `min_pass_rate` (minimum fraction of cases passing, 0.0–1.0), `min_win_rate` (minimum pairwise win rate).

## Example: eval.yaml for RFE Creator

```yaml
name: rfe-creator
skill: rfe.speedrun
execution:
  mode: batch
  arguments: "--input batch.yaml --headless --dry-run"
runner:
  type: claude-code
models:
  skill: claude-opus-4-6
  judge: claude-opus-4-6
mlflow:
  experiment: rfe-eval
permissions:
  deny: ["mcp__atlassian__*"]  # Block Jira writes during eval

dataset:
  path: eval/dataset/cases
  schema: |
    Each case directory contains:
    - input.yaml: YAML file. The 'prompt' field is the problem statement
      to send to the skill. 'clarifying_context' has additional context.
    - reference-rfe.md: Gold standard RFE (markdown with YAML frontmatter:
      rfe_id, title, priority, size, status).
    - reference-review.md: Gold standard review (markdown with YAML
      frontmatter: score 0-10, pass bool, recommendation, feasibility,
      per-criterion scores: what, why, open_to_how, not_a_task,
      right_sized each 0-2).
    - annotations.yaml: Expected scores and test metadata.

inputs:
  tools:
    - match: Questions asked to the user via AskUserQuestion.
      prompt: |
        Answer based on the test case. If asked about priority,
        say "Normal". If asked to confirm, say "yes".
    - match: |
        Any interaction with Jira — via MCP tools (mcp__atlassian__*)
        or scripts that import jira-python or call the Jira REST API.
      prompt: |
        Block production Jira. Only allow if JIRA_SERVER points to
        a test instance or jira-emulator.

outputs:
  - path: artifacts/rfe-tasks
    schema: |
      One markdown file per case, named RFE-NNN-slug.md where NNN is
      the case number (001, 002, ...). Contains YAML frontmatter with
      rfe_id, title, priority, size, status.
      Skip files ending in -comments.md or -removed-context.md.
  - path: artifacts/rfe-reviews
    schema: |
      One review file per case, named RFE-NNN-slug-review.md. Contains
      YAML frontmatter with score, pass, recommendation, feasibility,
      and per-criterion scores.

traces:
  metrics: true

judges:
  - name: frontmatter_valid
    description: |
      Validate that each generated RFE has valid YAML frontmatter with
      required fields: rfe_id, title, priority, status.
    check: |
      import yaml
      task = outputs["rfe-tasks_content"]
      if not task.startswith("---"):
          return False, "No YAML frontmatter"
      fm = yaml.safe_load(task.split("---", 2)[1])
      required = ["rfe_id", "title", "priority", "status"]
      missing = [f for f in required if f not in fm]
      if missing:
          return False, f"Missing: {', '.join(missing)}"
      return True, "All required fields present"

  - name: quality
    description: |
      Evaluate quality of the generated RFE compared to the reference.
    prompt_file: eval/prompts/quality-judge.md
    context:
      - eval/prompts/rfe-scoring-rubric.md

  - name: cost_efficient
    description: Verify the pipeline doesn't exceed $1 per case.
    check: |
      cost = outputs.get("cost_usd", 0)
      if cost and cost > 1.0:
          return False, f"Cost ${cost:.2f} exceeds $1.00"
      return True, f"Cost ${cost:.2f}"

thresholds:
  frontmatter_valid: {min_pass_rate: 1.0}
  quality: {min_mean: 3.5}
```

## Example: eval.yaml for Architecture Context

```yaml
name: architecture-context
skill: repo-to-architecture-summary
runner: claude-code

dataset:
  path: eval/dataset/cases
  schema: |
    Each case directory contains:
    - input.yaml: YAML file. 'repo_path' is the local path to the
      repository to analyze. 'distribution' (rhoai or odh) and
      'version' identify the platform.
    - reference-architecture.md: Gold standard architecture document
      with sections: Architecture Components, APIs, Dependencies,
      Network Architecture, Security. Claims have source references
      in file:line format.

inputs:
  tools:
    - match: Questions asked to the user via AskUserQuestion.
      prompt: |
        If asked which distribution, answer "rhoai".
        If asked which version, answer the latest.

outputs:
  - path: .
    schema: |
      A single GENERATED_ARCHITECTURE.md file per case with markdown
      sections matching the reference structure.

traces:
  metrics: true
  events: true   # Capture tool calls for source reference analysis

judges:
  - name: required_sections
    description: |
      Check that the generated architecture document contains all
      required sections.
    check: |
      content = outputs["main_content"]
      required = ["Architecture Components", "APIs", "Dependencies",
                  "Network Architecture", "Security"]
      missing = [s for s in required if s.lower() not in content.lower()]
      if missing:
          return False, f"Missing sections: {', '.join(missing)}"
      return True, f"All {len(required)} sections present"

  - name: accuracy
    description: |
      Compare the generated architecture summary against the reference.
    prompt: |
      Compare the generated architecture summary against the reference.
      Are the same components identified? Are APIs correct?
      Are dependencies and security details accurate? Score 1-5.

thresholds:
  required_sections: {min_pass_rate: 1.0}
  accuracy: {min_mean: 3.5}
```

## Skills

### /eval-setup

Set up the evaluation environment: verify dependencies, configure MLflow tracking and tracing, check API keys, create directory structure.

### /eval-analyze

Analyze a target skill and generate `eval.yaml`. Examines the skill's SKILL.md, discovers test cases, and produces the configuration with dataset schema, output descriptions, and suggested judges.

```
/eval-analyze --skill my-skill          # Analyze and generate eval.yaml
/eval-analyze --skill my-skill --update # Update existing eval.yaml
```

### /eval-dataset

Generate evaluation test cases. Creates realistic inputs based on the skill analysis, bootstraps a starter dataset, or expands an existing one.

```
/eval-dataset                              # Bootstrap 5 starter cases
/eval-dataset --count 20                   # Generate 20 cases
/eval-dataset --strategy expand            # Fill coverage gaps in existing dataset
```

### /eval-run

Execute the evaluation suite: prepare workspace, run the skill headlessly, collect artifacts, score with judges, detect regressions, and report results.

```
/eval-run --model opus                          # Run all cases
/eval-run --model opus --parallelism 3          # Run 3 cases concurrently
/eval-run --model opus --case case-001          # Run specific case
/eval-run --model opus --baseline prev-run-id   # Compare against baseline
/eval-run --model opus --no-judge               # Skip LLM judges
```

### /eval-review

Interactive human review of eval results. Presents judge scores and outputs, collects qualitative feedback, analyzes patterns, and proposes SKILL.md changes.

```
/eval-review --run-id 2026-04-04-opus      # Review a completed run
/eval-review --run-id <id> --case case-003 # Review specific cases
```

### /eval-mlflow

MLflow integration: sync datasets, log run results, attach judge feedback to traces. The agent reads the `schema` descriptions to understand case structure — no hardcoded field mappings.

```
/eval-mlflow --action sync-dataset              # Push cases to MLflow dataset
/eval-mlflow --run-id <id> --action log-results # Log scoring results
/eval-mlflow --run-id <id> --action push-feedback # Push judge+human feedback to traces
/eval-mlflow --run-id <id> --action pull-feedback # Pull MLflow UI annotations
/eval-mlflow --run-id <id>                      # Do everything
```

### /eval-optimize

Automated refinement loop: run eval, identify failures, read traces + judge rationale, edit the skill to fix issues, re-run to verify, check for regressions.

```
/eval-optimize --model opus --max-iterations 3
```

## Architecture

```
agent_eval/              # Python package (config, runner, state)
  config.py              # EvalConfig from eval.yaml
  state.py               # Shared state persistence
  agent/
    base.py              # EvalRunner ABC + RunResult
    claude_code.py       # Claude Code CLI runner
    stream_capture.py    # Stream-json processing + SubagentStop hook
  mlflow/
    experiment.py        # MLflow experiment setup
    trace_builder.py     # Hierarchical trace builder
  cli/
    trace_run.py         # claude-trace CLI

skills/
  eval-setup/            # Environment setup
  eval-analyze/          # Skill analysis + config generation
  eval-dataset/          # Test case generation
  eval-run/              # Evaluation execution
  eval-review/           # Interactive human review
  eval-mlflow/           # MLflow integration
  eval-optimize/         # Automated refinement loop
```

## Agent Support

The harness is agent-agnostic via the `EvalRunner` abstraction. Set `runner.type` in eval.yaml:

```yaml
runner:
  type: claude-code    # default — uses claude --print

runner:
  type: cli            # opaque CLI runner — delegates to an arbitrary command
  command: "my-runner run {agent} --model {model} --workspace {workspace}"
```

The `cli` runner executes a configurable command template with placeholder substitution. See **[docs/opaque-cli-runner-contract.md](docs/opaque-cli-runner-contract.md)** for the full contract (placeholders, metrics.json format, what the command MUST and SHOULD do).

Add new runners by subclassing `EvalRunner` in `agent_eval/agent/` and registering in `RUNNERS`.

## MLflow Tracing

The same tracing used by `/eval-mlflow` is available for standalone skill runs via `claude-trace` — a drop-in replacement for `claude --print` that captures stream-json output and builds hierarchical MLflow traces. See **[TRACING.md](TRACING.md)** for full documentation.

```bash
# Install with MLflow support
pip install -e "./agent-eval-harness[mlflow]"

# Run any skill with tracing
echo "/rfe.speedrun --input batch.yaml --headless" | claude-trace --model opus
```

## Dependencies

- `pyyaml >= 6.0`
- Optional: `mlflow[genai] >= 3.5` (for `/eval-mlflow` and `claude-trace`)
- Optional: `anthropic >= 0.40` (for LLM judges, pairwise comparison, and hook answering)
