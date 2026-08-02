# execution

The `execution` block describes **what** to run and **how** cases are processed —
independent of the [runner](runner.md) (which agent runtime) and the backend (Local,
Harbor, EvalHub — always a `--runner` CLI flag). It separates *how many invocations*
(`mode`) from *what to execute* (`skill` **or** `prompt`).

```yaml title="eval.yaml (excerpt)"
execution:
  mode: case              # per-case (default) or batch
  skill: my-skill-name    # skill to test  (mutually exclusive with prompt)
  arguments: "{prompt}"   # resolved per case from input.yaml fields
  # timeout: 3600         # per-invocation wall-clock seconds
  # max_budget_usd: 5.0   # per-invocation cost cap
  # parallelism: 4        # concurrent cases (case mode only)
  # env:
  #   JIRA_TOKEN: $JIRA_TOKEN   # $VAR resolved from the caller's environment
```

## Fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `mode` | `case` \| `batch` | `case` | One invocation per case, or one invocation for all cases. |
| `skill` | string | `""` | Skill name to invoke (`/skill-name`). Mutually exclusive with `prompt`. |
| `prompt` | string | `""` | Direct prompt template — no skill wrapper. Mutually exclusive with `skill`. |
| `arguments` | string | `""` | Template resolved per case from `input.yaml`. See [Templating](#argument-templating). |
| `timeout` | int \| null | `null` → **3600** | Per-invocation wall-clock timeout in seconds. |
| `max_budget_usd` | float \| null | `null` → **100.0** | Per-invocation cost cap. |
| `parallelism` | int \| null | `null` → **1** | Max concurrent case executions (case mode only). |
| `env` | map | `{}` | Env vars injected into each workspace's `.claude/settings.json`. See [env](#injecting-environment-variables). |
| `steps` | list | `[]` | Multi-step pipeline — sequential agent invocations sharing the case workspace. Replaces `skill`/`prompt`. See [Multi-step pipelines](#multi-step-pipelines). |

!!! note "Nulls become harness defaults"
    `timeout`, `max_budget_usd`, and `parallelism` are optional in the config (`None`).
    They only acquire their effective values (3600 s, $100, sequential) at run time.
    Explicit checks preserve `0` — set `max_budget_usd: 0` and you get a $0 cap, not
    the default.

## Choosing what to execute

```mermaid
flowchart TD
    E[execution block] --> M{mode}
    M -->|case| C[one invocation per case]
    M -->|batch| B[one invocation, all cases via batch.yaml]
    E --> W{skill or prompt?}
    W -->|skill:| S[/skill-name + arguments/]
    W -->|prompt:| P[direct prompt template]
```

`mode` (how many invocations) and `skill`/`prompt` (what to execute) are orthogonal —
any of the four combinations is valid. See
[the execution model](../../concepts/execution-model.md) for the conceptual overview
and [skill vs prompt](../../guides/skill-vs-prompt.md) for when to use each.

=== "Skill mode (case)"

    One invocation per case; `arguments` is resolved from each case's `input.yaml`.

    ```yaml
    execution:
      mode: case
      skill: rfe.create
      arguments: '--priority {{ input.priority }} "{{ input.prompt }}"'
    ```

=== "Skill mode (batch)"

    One invocation processes all cases; the skill loops internally over `batch.yaml`.

    ```yaml
    execution:
      mode: batch
      skill: rfe.speedrun
      arguments: '--input batch.yaml --headless'
    ```

=== "Prompt mode (case)"

    No skill wrapper — the agent receives the prompt directly. Leave `arguments` empty;
    `prompt` acts as the per-case template.

    ```yaml
    execution:
      mode: case
      prompt: "{{ input.prompt }}"
    ```

## Argument templating

`arguments` (skill mode) and `prompt` (prompt mode) support **two mutually exclusive**
placeholder styles. The style is **auto-detected**: if the template contains `{{` or
`{%`, it is rendered as Jinja2; otherwise brace substitution is used.

=== "Jinja2 — `{{ input.field }}`"

    Rendered with `input` bound to the case's `input.yaml`. Uses `StrictUndefined`, so a
    **missing field raises an error** rather than rendering empty.

    ```yaml
    arguments: '--priority {{ input.priority }} "{{ input.prompt }}"'
    ```

    For genuinely optional fields, guard them explicitly:

    ```yaml
    arguments: '{{ input.get("flags", "") }} {{ input.title | default("") }}'
    ```

=== "Brace — `{field}` / `{field?}`"

    Simple regex substitution against `input.yaml` keys.

    - `{field}` — **required**; a missing (or empty) value raises an error.
    - `{field?}` — **optional**; omitted when the field is missing or empty.

    ```yaml
    arguments: '--title "{title}" {extra_flags?}'
    ```

!!! tip "The `{prompt}` shortcut"
    `{prompt}` is the conventional single-field template — `/eval-analyze` generates it
    by default. In **batch** mode a bare `{prompt}` is filled from the first entry of the
    workspace `batch.yaml`.

!!! warning "Don't mix the two styles"
    A template with any `{{ … }}` is treated as Jinja2 in full — bare `{field}` braces in
    the same string are **not** substituted. Pick one style per template.

## Multi-step pipelines

Some skills form a **pipeline** — e.g. `create → refine → review`. Set `execution.steps`
to run several agent invocations per case, sequentially, in the **same workspace**.
`steps` replaces `skill`/`prompt` and is **case-mode only**.

```yaml
execution:
  mode: case
  steps:
    - id: refine                # required: unique; identifies the step
      skill: strategy-refine    # skill xor prompt, per step
      arguments: "STRAT-{{ input.id }}"
      env: { JIRA_TOKEN: $JIRA_TOKEN }   # merged over execution.env (step wins)
      timeout: 1800             # optional; falls back to execution.timeout
      max_budget_usd: 5.0       # optional; falls back to execution.max_budget_usd
      runner: { type: claude-code, effort: high }   # optional per-step override
    - id: review
      skill: strategy-review
      arguments: "{{ steps.refine.output }}"   # ← reference an earlier step
```

| Step field | Notes |
| --- | --- |
| `id` | Required, unique. Names the step for `{{ steps.<id> }}`, judge `step:`, and the Harbor step. |
| `skill` / `prompt` | One per step (mutually exclusive), same meaning as the top-level fields. |
| `arguments` | Resolved per step (see below). |
| `env` | Merged over `execution.env` (step wins); `$VAR` resolved from the caller. |
| `timeout`, `max_budget_usd`, `runner` | Optional per-step overrides; fall back to the `execution`/top-level defaults. |
| `on_failure` | `fail` (default — abort the remaining steps) or `continue`. |

**State passes through the shared workspace.** Every step runs in the same per-case
working directory, so files written by one step are visible to the next — the same model
[Harbor](../../guides/harbor.md) uses. Steps start a **fresh agent conversation** (no
carried context).

**Referencing an earlier step.** A step's `arguments`/`prompt` may use a `{{ steps.<id> }}`
namespace in addition to `{{ input }}`:

- `{{ steps.<id>.output }}` — that step's final assistant message,
- `{{ steps.<id>.exit_code }}`, `{{ steps.<id>.files }}`.

**Shell work around steps** uses [hooks](hooks.md), not a step type: `before_each`/
`after_each` wrap the whole case; the `before_step`/`after_step` phases wrap each step
(with `STEP_ID`/`STEP_INDEX` in the hook env, so a global hook can target one step via
its `condition`).

**Scoring.** By default judges score the whole case (the final step's workspace). Add
`step: <id>` to a judge to scope it to that step's own trace
(`{{ conversation }}`/`{{ tool_trace }}`), while shared files stay whole-case:

```yaml
judges:
  - name: refine_grounding
    step: refine              # scored against the refine step's trace
    prompt_file: eval/prompts/grounding.md
  - name: feasibility        # whole case (final workspace)
    prompt_file: eval/prompts/feasibility.md
```

!!! note "Harbor round-trip"
    `execution.steps` generates a schema 1.4 `[[steps]]` Harbor task (one verifier per
    step; whole-case judges run on the final step). Two things are **local-only**:
    `after_step` hooks, and `{{ steps.<id> }}` argument references — on Harbor, steps share
    state through the filesystem, so pass data between them via files rather than
    `{{ steps.output }}` (a `{{ steps.* }}` reference fails Harbor task generation).

## Injecting environment variables

`execution.env` writes variables into each case workspace's `.claude/settings.json`, so
they are visible to both the skill and its [hooks](hooks.md). Values beginning with `$`
are resolved from the **caller's** environment; a `$VAR` that is unset is **silently
omitted**. Literal values pass through unchanged.

```yaml
execution:
  env:
    JIRA_SERVER: http://localhost:8080   # literal
    JIRA_TOKEN: $JIRA_TOKEN              # resolved from os.environ["JIRA_TOKEN"]
```

!!! note "`execution.env` vs `runner.env`"
    `execution.env` targets the **workspace** (available to the skill and its hooks).
    [`runner.env`](runner.md) targets the **runner subprocess** itself. Both support the
    `$VAR` syntax.

## Precedence

CLI flags on `/eval-run` (and `execute.py`) always override the config:

| Config field | CLI override | Falls back to |
| --- | --- | --- |
| `execution.timeout` | `--timeout` | 3600 s |
| `execution.max_budget_usd` | `--max-budget` | $100.0 |
| `execution.parallelism` | `--parallelism` | sequential (1) |
| `execution.arguments` / `prompt` | `--skill-args` | config value |
| `models.skill` | `--model` | *(required)* |

## Validation

These errors are raised at **config load time** (`EvalConfig.from_yaml`), not mid-run:

| Condition | Error |
| --- | --- |
| `mode` not in `case`/`batch` | `execution.mode must be one of ['case', 'batch']` |
| Both `skill` and `prompt` set | `execution.skill and execution.prompt are mutually exclusive` |
| Required template field missing at run time | `Missing required field in template: …` |

!!! note "Deprecated top-level `skill:`"
    A top-level `skill:` (outside the `execution` block) still works but is
    auto-normalized into `execution.skill` with a deprecation warning. Author new configs
    with `execution.skill`.

## Gotchas

- **`parallelism` is case-mode only.** Batch mode is a single invocation, so there is
  nothing to parallelize. With [`runner.workspace_mode: repo`](runner.md) parallelism is
  forced to `1` (all cases share the repo checkout).
- **Per-case hooks need case/prompt mode.** `hooks.before_each` / `after_each` are
  ignored in batch mode (a warning is emitted); use `before_all` / `after_all` there. See
  [hooks](hooks.md).
- **`timeout` and `max_budget_usd` are per invocation**, not per run. In case mode they
  apply to each case; the run's total budget is their sum.

## See also

<div class="grid cards" markdown>

- [**eval.yaml reference**](../eval-yaml.md) — all top-level keys
- [**runner**](runner.md) — the agent runtime and `runner.env` / `workspace_mode`
- [**models**](models.md) — model-per-role precedence
- [**Execution model**](../../concepts/execution-model.md) — case vs batch, skill vs prompt
- [**CLI**](../cli.md) — `--model`, `--timeout`, `--parallelism`, and other flags

</div>
