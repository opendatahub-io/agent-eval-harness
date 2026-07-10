# Agent Eval Harness

Generic evaluation framework for agents and skills. Analyze, run, score, and improve skills automatically across different agent harnesses (Claude Code, OpenCode, Agent SDK).

**New**: Prompt-based evaluation (`execution.prompt`) for testing agent capabilities directly without skill wrappers. Extensible to any agent capability testing scenario. Initial implementation includes agentic documentation testing for evaluating documentation effectiveness, pattern understanding, and constraint compliance.

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

## Execution Model

The harness separates **how many invocations** (`execution.mode`) from **what to execute** (`execution.skill` or `execution.prompt`):

### Execution Mode
- **case**: One invocation per test case (default). The harness loops over cases.
- **batch**: One invocation for all cases via batch.yaml. The skill/agent loops internally.

### What to Execute
- **Skill mode** (`execution.skill`): Test predefined skill implementations (`/my-skill --args`). Evaluates skill correctness, quality, and cost efficiency.
- **Prompt mode** (`execution.prompt`) ✨ NEW: Test agent capabilities directly by sending prompts without a skill wrapper. Extensible to any agent evaluation scenario.

**Implemented flavor - Agentic Documentation Testing** (see `examples/openshift-agentic-docs.md`):
- **Documentation effectiveness**: Can agents navigate and use your docs?
- **Pattern understanding**: Can agents identify and apply code patterns?
- **Constraint compliance**: Do agents respect documented rules?
- **API usage**: Can agents correctly use APIs from documentation alone?

**Extensible to other scenarios**:
- Code generation from specifications
- API usage pattern validation  
- Reasoning trace quality assessment
- Custom agent capability benchmarks

Useful for testing documentation quality (CLAUDE.md, AGENTS.md, ai-docs/), onboarding effectiveness, and establishing agent baseline capabilities.

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

This makes all eval skills available: `/eval-setup`, `/eval-analyze`, `/eval-dataset`, `/eval-run`, `/eval-review`, `/eval-mlflow`, `/eval-optimize`, and `/eval-check`.

### 2. Set up environment

```
/eval-setup
```

This checks dependencies, configures MLflow, verifies API keys, and creates directories.

### 3a. Analyze your skill (skill mode)

```bash
/eval-analyze --skill my-skill
```

This examines the skill's SKILL.md, discovers test cases, and generates `eval.yaml` with:
- `execution.mode: case` or `batch`
- Natural language `schema` descriptions of your dataset and outputs
- Suggested judges (inline checks + LLM quality assessment)
- Regression thresholds

### 3b. Analyze for prompt mode evaluation

```bash
/eval-analyze --prompt examples/openshift-agentic-docs.md
```

This analyzes your repository's documentation (CLAUDE.md, AGENTS.md, ai-docs/) and generates `eval.yaml` with:
- `execution.prompt: "{{ input.prompt }}"` (prompt mode)
- A `generation:` block with builtin documentation prompts (`docs/navigation`, `docs/anti-pattern`, etc.)
- LLM rubric judges for semantic evaluation
- Documentation tracking to verify agents use docs correctly

**Note**: Prompt mode is extensible. The OpenShift analysis prompt is a domain-specific example. You can create custom analysis prompts for other domains or agent capability testing scenarios.

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

# Execution — what to run and how (runner-agnostic)
execution:
  mode: case              # case (per-case invocation) or batch (single invocation)
  skill: my-skill-name    # skill mode; use `prompt:` instead for prompt mode
                          # (direct agent invocation, no skill wrapper)
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
  # env:                       # Extra env vars for subprocess ($VAR resolves from caller)
  #   CUSTOM_AUTH_TOKEN: "$CUSTOM_AUTH_TOKEN"
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

- **`execution`** — `mode` determines how evaluation runs:
  - **`case`** (default, skill mode): Skill invoked once per test case with `{field}` placeholders resolved from each case's input.yaml
  - **`batch`** (skill mode): All cases bundled into batch.yaml for a single skill invocation
  - **`prompt`** (prompt mode): Agent receives prompts directly without a skill wrapper, useful for testing agent capabilities like documentation navigation, pattern understanding, constraint compliance, etc.
  
  Additional fields: `arguments` template, optional `timeout` (wall-clock seconds per invocation), `max_budget_usd` (cost cap per invocation), `parallelism` (run up to N cases concurrently in case/prompt modes), and `env` for injecting environment variables into workspaces (`$VAR` syntax resolves from caller's environment).
- **`schema`** — natural language description of structure. Used on `dataset` and each `outputs` entry. Agents and judges read these to understand the data.
- **`generation`** — optional top-level block selecting case **provenance** via `strategy`: `skill` (default — agent authors from skill analysis; needs no block), `synthetic` (LLM generates from seeds), or `from-traces` (extracted from MLflow production traces). For `synthetic`: `context` holds repository-specific knowledge (`documentation_structure`, `constraints`, `apis`, `components`, etc.) injected into every generation prompt, and `seeds` is a list where each seed has a `category`, a `count`, and exactly one **generation prompt** discriminator (mirroring judges): `builtin` (from `agent_eval/prompts/`, e.g. `docs/navigation` — discover with `list_prompts.py`), `prompt_file` (a project path), or an inline `prompt`. Each seed's `category` is stamped onto generated cases as `annotations.category`. `seeds`/`context` apply only to `synthetic`.
- **`inputs.tools`** — tool interception for headless and interactive execution. Each entry has a `match` (what to intercept) and a `prompt` (how to handle it). AskUserQuestion uses 3-tier answering: exact `case_overrides` → LLM call (`models.hook`) with case context (`input.yaml` + `answers.yaml`) → fallback to first option.
- **`outputs`** — two types: `path` for file artifacts on disk, `tool` for tool call side effects (Jira, APIs). Both have `schema` descriptions. Optional `batch_pattern` maps output files to cases in batch mode using `{n}` as a 1-based index (e.g. `"RFE-{n:03d}"` → `RFE-001`, `RFE-002`).
- **`traces`** — execution data to capture: stdout/stderr logs, events (tool calls, reasoning text, results), metrics (exit code, tokens, cost, duration). Available to judges via the `outputs` dict.
- **`check`** — inline Python snippet for deterministic validation. Receives an `outputs` dict with file contents, execution metadata, tool calls, logs, and `annotations` (from dataset `annotations.yaml`). Returns `(bool, str)`.
- **`if`** — optional condition on a judge. Python expression evaluated against `annotations` and `outputs`. When false, the judge is skipped for that case (not counted in pass_rate or mean).
- **`prompt`** / **`prompt_file`** / **`llm_rubric`** — LLM judge evaluation instructions. All three compile to the same internal prompt before Jinja2 rendering. Priority order: `llm_rubric` > `prompt` > `prompt_file`.
  - **`llm_rubric`**: Syntactic sugar for simple criteria. Auto-appends `{{ conversation }}` template if missing. Best for synthetic-generation configs. Example: `llm_rubric: "Agent cited documentation sources"`
  - **`prompt`**: Full Jinja2 template with manual control. Use for complex logic or multiple placeholders like `{{ outputs }}`, `{{ conversation }}`, `{{ tool_trace }}`, `{{ reference }}`.
  - **`prompt_file`**: External file path (absolute or relative to project root). Use for sharing prompts across judges. File can contain rubric-style or full template content.
- **`context`** — list of file paths loaded and appended to the LLM judge prompt as supplementary material (rubrics, guidelines, examples).
- **`module`** / **`function`** — external Python code judge for complex validation.
- **`permissions`** — tool access patterns (`allow`/`deny`) for headless execution. Generic across runners — each runner translates to its platform's mechanism.
- **`runner`** — `type` discriminator selects the runner implementation; remaining fields (`effort`, `settings`, `plugin_dirs`, `env`, `system_prompt`) are runner-specific and ignored by other runners.
- **`models`** — `skill`/`subagent`/`judge`/`hook` defaults, overridable per-judge or via CLI flags. `hook` is the model used for LLM-based AskUserQuestion answering.
- **`mlflow`** — `experiment` (and optional `tracking_uri`/`tags`) for result logging.
- **`thresholds`** — per-judge regression detection. Valid keys: `min_mean` (minimum average score), `min_pass_rate` (minimum fraction of cases passing, 0.0–1.0), `min_win_rate` (minimum pairwise win rate).

## Example: eval.yaml for RFE Creator

```yaml
name: rfe-creator
execution:
  mode: batch
  skill: rfe.speedrun
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
execution:
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

## Example: eval.yaml for Prompt-Based Documentation Testing

```yaml
name: docs-navigation-eval
description: Test if agents can navigate and use repository documentation
# Prompt mode — sends prompts directly to the agent (no skill wrapper)

execution:
  mode: case
  prompt: "{{ input.prompt }}"  # Resolved from input.yaml per case

runner:
  type: claude-code

models:
  skill: claude-sonnet-4-6
  judge: claude-opus-4-6

dataset:
  path: eval/dataset/cases
  schema: "input.yaml with 'prompt' (question) and 'expected_files' (docs to consult)"

# Synthetic generation: a top-level block, peer of execution/dataset/judges
generation:
  strategy: synthetic
  # Repository knowledge injected into every generation prompt
  context:
    documentation_structure:
      entry_point: CLAUDE.md
      areas:
        - path: ai-docs/
          topics: [component-docs, workflows]
    constraints:
      - rule: "All new APIs must start with v1alpha1"
        documentation: ai-docs/practices/api-evolution.md
      - rule: "Never modify files in vendor/"
        documentation: CLAUDE.md
  # Each seed picks a generation prompt via builtin / prompt_file / prompt
  seeds:
    - category: navigation
      builtin: docs/navigation          # from agent_eval/prompts/ (see list_prompts.py)
      count: 10
      description: Finding specific documentation
    - category: anti-pattern
      builtin: docs/anti-pattern
      count: 5
      description: Rejecting constraint violations

outputs:
  - path: outputs
    schema: "agent responses (markdown files)"

traces:
  stdout: true
  events: true
  metrics: true

judges:
  # Check if agent read the expected documentation
  - name: consulted_docs
    builtin: consulted_docs
    if: "annotations.get('category') == 'navigation'"
    arguments:
      min_coverage: 0.8
      match: suffix
  
  # Semantic quality assessment
  - name: answer_quality
    llm_rubric: |
      Evaluate the agent's answer against the expected behavior.
      Score 1-5 where 5 is excellent.

thresholds:
  consulted_docs: {min_pass_rate: 0.8}
  answer_quality: {min_mean: 3.5}
```

## Skills

### /eval-setup

Set up the evaluation environment: verify dependencies, configure MLflow tracking and tracing, check API keys, create directory structure.

### /eval-analyze

Analyze a target and generate `eval.yaml`. Two modes:

**Skill mode** (`--skill`): Examines the skill's SKILL.md, discovers test cases, and produces configuration with:
- `execution.mode: case` or `batch`
- Dataset schema, output descriptions
- Suggested judges (inline checks + LLM prompts)

**Prompt mode** (`--prompt`): Uses an analysis prompt to generate evaluation config. Domain-specific analysis prompts (see `examples/`) analyze repository documentation (CLAUDE.md, AGENTS.md, ai-docs/) and produce:
- `execution.prompt: "{{ input.prompt }}"` (direct agent invocation)
- A `generation:` block with seeds (navigation, anti-pattern, authoring, component-usage, architecture)
- LLM rubric judges for semantic evaluation
- `generation.context` for test generation

```bash
/eval-analyze --skill my-skill                           # Skill mode: analyze skill implementation
/eval-analyze --skill my-skill --update                  # Update existing skill mode eval.yaml
/eval-analyze --prompt examples/openshift-agentic-docs.md # Prompt mode: analyze agentic documentation
/eval-analyze --prompt custom.md                         # Prompt mode: use custom analysis prompt
```

Prompt mode is extensible. Create custom analysis prompts for other agent capability testing scenarios (code generation, API usage patterns, reasoning quality, etc.).

### /eval-dataset

Generate evaluation test cases. Case **provenance** is set in the config via `generation.strategy`:

- **`skill`** (default — no `generation` block needed): the agent authors realistic inputs from the skill analysis.
- **`synthetic`**: an LLM generates cases from `generation.seeds` + `generation.context` (from `/eval-analyze --prompt`).
- **`from-traces`**: cases are extracted from MLflow production traces.

Whether a run **creates a fresh set or augments an existing one** is derived from the current dataset (empty → fresh; populated → gap-fill) — there is no `--strategy` flag.

```bash
/eval-dataset                              # generate per the config's generation.strategy
/eval-dataset --count 20                   # target 20 cases (skill/from-traces; synthetic uses per-seed count)
/eval-dataset --run-id <id>                # augment, targeting failures from a prior eval run
```

**Synthetic generation** uses builtin generation prompts (`docs/navigation`, `docs/anti-pattern`, `docs/authoring`, `docs/component-usage`, `docs/architecture` — from `agent_eval/prompts/`) combined with repository-specific `generation.context` to create targeted test cases. Extensible with project-specific prompts via `prompt_file:` or inline `prompt:`.

### /eval-run

Execute the evaluation suite: prepare workspace, run the skill headlessly, collect artifacts, score with judges, detect regressions, and report results.

```
/eval-run --model opus                          # Run all cases
/eval-run --model opus --parallelism 3          # Run 3 cases concurrently
/eval-run --model opus --cases case-001         # Run specific case
/eval-run --model opus --baseline prev-run-id   # Compare against baseline
/eval-run --model opus --no-llm-judges          # Skip LLM judges
```

### /eval-review

Interactive human review of eval results. Presents judge scores and outputs, collects qualitative feedback, analyzes patterns, and proposes SKILL.md changes.

```
/eval-review --run-id 2026-04-04-opus      # Review a completed run
/eval-review --run-id <id> --cases case-003 # Review specific cases
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

### /eval-check

Scan the full configuration (skills, commands, CLAUDE.md, hooks) as a system. Finds content overlap, trigger collisions, CLAUDE.md duplication, and type misclassification. Produces an informational report with restructuring suggestions.

```bash
/eval-check                        # Scan and report to harness-report.md
/eval-check --include-global       # Also scan ~/.claude/CLAUDE.md
/eval-check --output my-report.md  # Custom output path
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
  eval-check/    # Full-harness configuration health check
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
- Optional: `anthropic >= 0.40` (for LLM judges, pairwise comparison, synthetic dataset generation, and hook answering)
