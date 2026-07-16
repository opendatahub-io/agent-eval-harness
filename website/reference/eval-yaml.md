# The eval.yaml schema

A single `eval.yaml` in your project root drives everything. It is parsed into an
`EvalConfig` and validated at load time. **Every top-level key is optional** and has a
sensible default — a minimal config is just a `name`, what to execute, a `dataset`, and
one `judge`.

!!! tip "Describe *what*, not *where*"
    `eval.yaml` describes **what** to evaluate. The **execution backend** (Local,
    Harbor, EvalHub) is always a CLI flag (`--runner`), never a config key — so the
    same file runs unchanged everywhere.

## Top-level keys

| Key | Purpose | Reference |
| --- | --- | --- |
| `name` | Experiment / run name (defaults to the file stem) | *(inline)* |
| `description` | Human-readable description | *(inline)* |
| `execution` | What to run and how cases are processed | [execution](config/execution.md) |
| `runner` | Agent runtime + runtime-specific knobs | [runner](config/runner.md) |
| `models` | Model per role: skill, subagent, judge, hook | [models](config/models.md) |
| `permissions` | Tool allow/deny for headless runs | [permissions](config/permissions.md) |
| `mlflow` | Experiment tracking (opt-in) | [mlflow](config/mlflow.md) |
| `dataset` | Where cases live and what they contain | [dataset](config/dataset.md) |
| `generation` | How `/eval-dataset` sources cases | [generation](config/generation.md) |
| `inputs` | Tool interception handlers (`inputs.tools`) | [inputs.tools](config/inputs-tools.md) |
| `outputs` | Artifacts / tool calls to collect | [outputs](config/outputs.md) |
| `traces` | Which execution data to capture | [traces](config/traces.md) |
| `hooks` | Lifecycle shell hooks | [hooks](config/hooks.md) |
| `judges` | How each case is scored | [judges](config/judges.md) |
| `thresholds` | Regression gates per judge | [thresholds](config/thresholds.md) |
| `reward` | Collapse judges into an RL reward scalar | [reward](config/reward.md) |
| `skill` | **Deprecated** — use `execution.skill` | *(see below)* |

!!! warning "`skill:` at the top level is deprecated"
    A top-level `skill:` still works but is auto-normalized into `execution.skill`
    with a deprecation warning. Always author new configs with `execution.skill`.

## Two minimal configs

Which keys you set depends on whether you're testing a **skill** or a **capability**.
See [the execution model](../concepts/execution-model.md) for the difference.

=== "Skill mode"

    ```yaml
    name: my-skill-eval

    execution:
      mode: case
      skill: my-skill
      arguments: "{prompt}"

    dataset:
      path: eval/dataset/cases
      schema: "Each case has an input.yaml with a 'prompt' field."

    judges:
      - name: output_quality
        prompt: "Score the output 1-5 for completeness and accuracy."
    ```

=== "Prompt mode"

    ```yaml
    name: docs-navigation-eval

    execution:
      mode: case
      prompt: "{{ input.prompt }}"

    runner:
      workspace_mode: repo   # navigate the real repository

    dataset:
      path: eval/dataset/cases
      schema: "Each case has an input.yaml with a 'prompt' field."

    judges:
      - name: used_docs
        builtin: consulted_docs
    ```

## A fully annotated config

The repository's root [`eval.yaml`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/eval.yaml)
is the canonical, heavily-commented reference — every block with inline comments and
commented-out variants for all four judge types, tool interception, `batch_pattern`,
and thresholds. It's the best single file to copy from.

```yaml title="eval.yaml (excerpt)"
name: my-skill-eval
description: Evaluate the main skill pipeline

execution:
  mode: case              # per-case (default) or batch
  skill: my-skill-name    # skill to test (use `prompt:` for prompt mode)
  arguments: "{prompt}"   # resolved per case from input.yaml fields

runner:
  type: claude-code       # claude-code | cli | responses-api
  # effort: high          # low | medium | high | xhigh | max

models:
  skill: claude-opus-4-6  # required (or pass --model)
  judge: claude-opus-4-6  # used by LLM and pairwise judges

permissions:
  deny:
    - "mcp__*"            # block all MCP tools during eval

mlflow:
  experiment: my-skill-eval   # opt-in: omit the block to disable tracking

dataset:
  path: eval/dataset/cases
  schema: |
    Each case has input.yaml (a 'prompt' field) and reference.md (gold output).

outputs:
  - path: artifacts
    schema: "One markdown file per case, named NNN-slug.md."

traces:
  stdout: true
  stderr: true
  events: false
  metrics: true

judges:
  - name: has_content
    check: |
      content = outputs["main_content"]
      if len(content.strip()) < 100:
          return False, f"Output too short ({len(content.strip())} chars)"
      return True, f"Output has {len(content.strip())} chars"

  - name: output_quality
    prompt: "Score 1-5 vs the reference for completeness, clarity, accuracy."

thresholds:
  has_content: { min_pass_rate: 1.0 }
  output_quality: { min_mean: 3.5 }
```

## Conventions

- **Schema fields are natural language.** `dataset.schema` and `outputs[].schema` are
  documentation for the LLM agents and judges — scripts operate on file *paths*, not a
  parsed spec. There are no hardcoded field names.
- **Load-time validation is strict.** Mutually-exclusive keys (`skill` + `prompt`),
  invalid enums (`execution.mode`), and malformed reward formulas fail at load, not
  mid-run.

## Per-key reference

<div class="grid cards" markdown>

- [**execution**](config/execution.md) — mode, skill/prompt, arguments, timeout, budget, parallelism, env
- [**runner**](config/runner.md) — type, effort, settings, plugin_dirs, env, system_prompt, command, workspace_mode
- [**models**](config/models.md) — skill, subagent, judge, hook roles and precedence
- [**permissions**](config/permissions.md) — allow/deny patterns and the path-based compiler
- [**mlflow**](config/mlflow.md) — experiment, tracking_uri, tags
- [**dataset**](config/dataset.md) — path, schema, workspace.files
- [**generation**](config/generation.md) — strategy, context, seeds
- [**inputs.tools**](config/inputs-tools.md) — tool interception handlers
- [**outputs**](config/outputs.md) — path vs tool, schema, batch_pattern, types
- [**traces**](config/traces.md) — stdout, stderr, events, metrics
- [**hooks**](config/hooks.md) — before/after all/each, before_scoring
- [**judges**](config/judges.md) — the four judge types and all fields
- [**thresholds**](config/thresholds.md) — min_mean, min_pass_rate, min_win_rate
- [**reward**](config/reward.md) — single-judge and formula reward modes

</div>
