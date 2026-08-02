# 0011: Multi-Step Execution

## Status

Proposed

## Problem

Today an eval case is exactly **one** invocation. `ExecutionConfig` is flat — a single
`skill` **xor** `prompt` with one `arguments`/`env`/`timeout`/`max_budget_usd`
(`agent_eval/config.py`, `ExecutionConfig`) — and the executor calls
`runner.execute(...)` **once** per case against one staged workspace
(`skills/eval-run/scripts/execute.py`, `_run_single_case`).

But real skills form **pipelines**. The motivating case is strat-creator:
`strategy-create → strategy-refine → strategy-review`, three separate headless
`claude -p` sessions that hand artifacts to each other. Because the harness can only
run one invocation, that eval is driven by a bespoke **shell script** that chains the
`claude` calls, stages artifacts between them, and hand-parses `total_cost_usd` out of
stream-json:

```bash
# strat-creator eval/run-strat-pipeline.sh  (a `cli` runner driver, ~simplified)
claude -p "/strategy-refine STRAT-<n> --dry-run ..." --output-format stream-json | tee ...
claude -p "/strategy-review STRAT-<n> --dry-run ..." --output-format stream-json | tee ...
# ...then regex the cost + assemble a metrics.json by hand
```

This works but is brittle and throws away everything the harness gives you: no
per-step traces, no per-step cost attribution, no per-step scoring, no portability to
Harbor/EvalHub, and the pipeline logic lives in a shell script instead of `eval.yaml`.

Meanwhile **Harbor already models multi-step tasks** (`schema_version 1.4`, a
`[[steps]]` array-of-tables) and **the harness already _consumes_ their results** —
`agent_eval/harbor/results.py` (`_parse_multi_step_trial`) reads each
`steps/<name>/verifier/reward.json`, records it as a `judge_type: "step"` judge, and
means the per-step rewards into the trial reward. But the harness **cannot _generate_
or _locally execute_** a multi-step task: `agent_eval/harbor/tasks.py`
(`generate_tasks`) emits a single `[agent]`/`[verifier]`, and there is no local step
loop. This spec closes that asymmetry.

## Scope

**In:**

- A typed `execution.steps[]` field; each step is an **agent invocation** (`skill` xor
  `prompt`) with its own `arguments`/`env`/`timeout`/`max_budget_usd` and an optional
  per-step `runner:` override.
- Local **sequential** execution of steps in the **shared per-case workspace**.
- A run-time `{{ steps.<id>.* }}` template namespace for wiring one step's output into a
  later step.
- **Per-step + whole-case scoring**: an optional `step:` selector on judges, backed by a
  per-step sub-record; whole-case judging stays the default.
- **Harbor round-trip**: generate `[[steps]]` (schema 1.4) from `execution.steps` so a
  multi-step eval runs on Harbor (the results side already exists).
- **Per-step hooks**: `before_step`/`after_step` phases (local), reusing the existing
  phase-generic hook machinery, for setup/staging/validation around individual steps.
- Backward compatibility: a single `skill`/`prompt` config is normalized into a
  one-element step list — one code path, no behavior change.

**Out (deferred):**

- `mode: batch` + steps (batch is a single collapsed invocation; rejected at load in v1).
- Cross-step **conversation resume** (Harbor `agent.resume_trajectory`). The runner ABC
  is stateless per call; fresh-conversation-per-step is the default and matches Harbor.
- Non-agent **shell / `run:` steps** as a step *type*. Shell setup/staging/validation runs
  on lifecycle hooks — including the new per-step `before_step`/`after_step` (§4) — not a
  bespoke step type (see Alternatives).
- **Harbor generation of per-step hooks.** `before_step` maps to Harbor's per-step
  `workdir/setup.sh`, but serializing hooks into it is deferred (best-effort, in-container
  `command` hooks only); `after_step` has no Harbor equivalent and stays local-only.
- A conditional-step DSL and declarative retry/backpressure — neither round-trips to
  Harbor.

## Design

### 0. Where it fits

`steps` is **orthogonal** to `mode` (case|batch) and lives **inside** `execution:`,
exactly the way `skill`/`prompt` already do. A step is "one `runner.execute()` against
the case workspace" — the same primitive used today, run N times in sequence instead of
once. No new runtime concept, no runner ABC change.

### 1. Config surface (`eval.yaml`)

```yaml
execution:
  mode: case                      # steps run in case mode (v1)
  env: { COMMON: value }          # case-level env (base layer)
  steps:
    - id: create                  # REQUIRED: identity for {{ steps.create.* }},
                                  #   judge `step:` targeting, and the Harbor step name
      name: "Create strategy"     # optional display label
      skill: strategy-create      # skill XOR prompt, validated PER STEP
      arguments: "{{ input.rfe }}"
      env: { JIRA_TOKEN: $JIRA_TOKEN }   # merged OVER execution.env (step wins)
      timeout: 1800               # optional; falls back to execution.timeout
      max_budget_usd: 5.0         # optional; falls back to execution.max_budget_usd
      runner:                     # optional per-step runner override
        type: claude-code         #   parsed by the SAME _parse_runner_config as
        effort: high              #   the top-level runner and agent.runner
    - id: refine
      skill: strategy-refine
      arguments: "{{ steps.create.output }} {{ input.focus }}"   # ← prior-step output
    - id: review
      skill: strategy-review
      arguments: "{{ steps.refine.output }}"

judges:
  - name: refine_quality
    step: refine                  # OPTIONAL: score this step's sub-record;
                                  #   default (unset) = whole case / final workspace
    prompt_file: eval/prompts/refine.md
```

Internal model — a **typed** dataclass (do **not** copy the agent-judge's permissive
untyped dict; steps are the primary execution path and need per-step validation):

```python
@dataclass
class StepConfig:
    id: str = ""                       # required in practice; unique within steps
    name: str = ""
    skill: str = ""                    # skill XOR prompt
    prompt: str = ""
    arguments: str = ""
    env: dict = field(default_factory=dict)
    timeout: Optional[int] = None      # -> execution.timeout
    max_budget_usd: Optional[float] = None  # -> execution.max_budget_usd
    runner: Optional[RunnerConfig] = None   # per-step override
    on_failure: str = "fail"           # fail (default, Harbor-compatible) | continue

    def __post_init__(self):
        # reuse ExecutionConfig's skill-XOR-prompt rule, per step
        ...

# ExecutionConfig gains:
#   steps: list[StepConfig] = field(default_factory=list)
```

**Parsing/validation** (in `EvalConfig.from_yaml`), following the agent-judge precedent:

- Each step mapping is shallow-validated; `id` required and unique; `skill` xor `prompt`
  re-checked via the step's `__post_init__`.
- A per-step `runner:` sub-block is promoted to a typed `RunnerConfig` via the **existing**
  `_parse_runner_config(runner_raw, context=f"execution.steps[{i}].runner")` — the same
  helper used for the top-level `runner:` and `agent.runner:`.
- `execution.steps` and a top-level `execution.skill`/`prompt` are **mutually exclusive**
  at load (same failure style as skill/prompt today).

### 2. Backward compatibility & normalization

A config with a bare `execution.skill`/`execution.prompt` (no `steps`) is normalized
into a **single-element `steps` list** in `EvalConfig.__post_init__`. Everything then
runs the one code path. `resolve_skill()`, `is_prompt_mode()`, and `eval_name()` read
`steps[0]` for the single-step case, so their existing callers (executor, Harbor,
EvalHub) keep working unchanged. There is **one** execution model, and "single skill" is
its degenerate case.

### 3. Execution — sequential steps in the shared workspace

`_run_single_case` becomes: stage the case workspace **once**, then loop the steps:

```python
case_ws = stage(case)                       # unchanged: workspace/cases/<case_id>
steps_ctx = {}                              # {id: {output, exit_code, files, ...}}
for i, step in enumerate(config.execution.steps):
    args = resolve_template(step.arguments or step.prompt,
                            input=case_data, steps=steps_ctx)   # run-time
    merged_env = {**config.execution.env, **step.env}          # step wins
    step_cfg = copy.copy(config)                               # shallow view
    if step.runner:
        step_cfg.runner = step.runner
    runner = RUNNERS[step_cfg.runner.type].from_config(step_cfg, effort=...)
    result = runner.execute(target=step.skill or None, args=args,
                            workspace=case_ws, model=..., extra_env=merged_env,
                            timeout_s=step.timeout, max_budget_usd=step.max_budget_usd)
    steps_ctx[step.id] = _step_output(result, case_ws)
    if result.exit_code != 0 and step.on_failure == "fail":
        break                                # abort remaining steps (Harbor fail-fast)
collect(case_ws)                            # unchanged: gather artifacts for scoring
```

Key properties:

- **State passing = the shared filesystem** (Harbor's model exactly): files written by
  step N are visible to N+1 because every step runs in the same `case_ws`. No new
  plumbing, and it round-trips to Harbor for free.
- **Per-step runner override** reuses the agent-judge's `copy.copy(config)` + swap
  `.runner` + `RUNNERS[type].from_config(...)` pattern verbatim.
- **Fresh conversation per step** — `runner.execute()` is stateless, which *is* Harbor's
  default; no ABC change and no session threading.
- **`on_failure: fail`** (default) aborts remaining steps, matching Harbor's fail-fast.
  `continue` is local-only and flagged as non-round-trippable.
- **Parallelism** stays at the case level (cases parallel, steps sequential). The per-case
  function owns the step loop, so the ThreadPool and serial paths call the **same**
  function — parallel runs can't diverge onto a single-case-only path.
- **Security:** resolved args/prompts (which may embed prior-step stdout) are **not**
  logged (CWE-532).

Per-step cost/tokens/turns are captured and written under each case in
`run_result.json` (a `steps` breakdown), in addition to the case totals the report
already sums.

### 4. Per-step hooks (`before_step` / `after_step`)

Shell work around a step — staging its inputs, seeding a fixture, validating its output —
runs on **hooks**, not a bespoke step type. The hook system is already phase-generic:
`run_hooks(hooks, env, cwd, log_dir, phase_name, case_id)` (`agent_eval/hooks.py`) runs
any list, and `from_yaml` builds every phase from one `phases` list with shared
`on_failure`/`condition` validation. Two new phases slot in with no new machinery:

- `HooksConfig` gains `before_step` and `after_step` lists; adding them to the `phases`
  list makes them inherit the existing validation.
- The step loop (§3) wraps each `runner.execute()` exactly like the per-case block already
  wraps the case (`execute.py`): `run_hooks(before_step, …)` → `collect_hook_outputs`
  merged into that step's `extra_env` → `execute()` → `run_hooks_safe(after_step, …)` in a
  `finally` (so `after_step` fires even if the step raises).
- `build_hook_env` gains `STEP_ID`/`STEP_INDEX` (mirroring `CASE_ID`), so a single global
  `before_step` can target one step via the existing condition DSL.

```yaml
hooks:
  before_step:
    - command: "python3 eval/stage_inputs.py"       # runs before every step
    - command: "python3 eval/seed_review.py"
      condition: "env.STEP_ID == 'review'"           # ...or gate to one step
  after_step:
    - command: "python3 eval/validate_step.py"       # local-only (no Harbor analog)
```

A `before_step` hook's `.hook-outputs.yaml` env is injected into that step's invocation
(the same handoff `before_each` uses), so it can stage the step's input or export a value
the step's `arguments` template reads. Per-step hooks are **case-mode only**, like
`before_each`/`after_each`. Harbor emission of `before_step`
(→ `steps/<id>/workdir/setup.sh`) is deferred; `after_step` is local-only.

### 5. State passing & templating

The one genuine engine addition: a `{{ steps.<id>.* }}` namespace, resolved **per step
at run time** (today `resolve_arguments` binds only `input`, once per case). Before each
step, its `arguments`/`prompt` is rendered with `input=case_data` **and**
`steps=steps_ctx` accumulated from completed steps:

- `{{ steps.<id>.output }}` — the step's last assistant message / stdout text.
- `{{ steps.<id>.exit_code }}`, `{{ steps.<id>.files }}` — status + files it produced.

`id` is required for any step referenced downstream (GitHub Actions precedent). Bare
`{{ input.* }}` keeps working unchanged.

### 6. Scoring — per-step + whole-case

- **Default (no `step:`)** — judges score the whole case over the final shared workspace,
  exactly as today. `load_case_record` is unchanged for these.
- **`step: <id>`** — the judge is scoped to that step. `load_case_record` gains a
  per-step sub-record:

  ```python
  record["steps"] = {
    "<id>": { "conversation", "events", "tool_trace", "exit_code",
              "cost_usd", "duration_s", "files" },
    ...
  }
  ```

  A step-scoped judge's template vars (`{{ conversation }}`, `{{ tool_trace }}`,
  `{{ outputs }}`) resolve to that step's sub-record; top-level `{{ conversation }}` etc.
  keep meaning "the final step" for backward compatibility. Load-time warning if `step:`
  names an unknown step.

Each judge remains one row in `summary.yaml` (keyed by name, annotated with its step),
which mirrors how `results.py` already keys Harbor per-step rewards — so **local and
Harbor reports line up**.

### 7. Harbor round-trip (`tasks.py` → `[[steps]]`)

`generate_tasks` learns to emit a Harbor multi-step task when `execution.steps` has more
than one step (`schema_version = "1.4"`):

- One `steps/<id>/instruction.md` per step (the step's resolved `arguments`/`prompt`).
- Per-step `[steps.agent]` (`timeout_sec` from `step.timeout`) and, for step-scoped
  judges, `steps/<id>/tests/test.sh` (a `[steps.verifier]`) running that step's judges.
- Whole-case judges map to a **final-step verifier** (equivalently
  `multi_step_reward_strategy: final`) or task-level `tests/`.
- `min_reward` on a step is emitted from that step's judge threshold, when present.

The consumer side (`results.py`, per-step reward ingestion + mean rollup) already exists,
so this is a **generator-only** change plus a template bump. EvalHub's
`config_translator.py` treats a multi-step execution opaquely (it already only serializes
metrics/benchmarks), so no change is required there for v1.

### 8. Reuse of existing machinery

| Need | Reused from |
| --- | --- |
| Per-step `runner:` parsing | `_parse_runner_config(context=...)` (top-level + `agent.runner`) |
| Per-step runner instantiation | `copy.copy(config)` + `RUNNERS[type].from_config(...)` (agent-judge) |
| State between steps | the existing per-case workspace (`case_ws`) |
| Env handoff | the `extra_env` merge already used for hook→skill env |
| `$VAR` resolution in `env` | `ExecutionConfig.env` resolution |
| Argument templating | `resolve_arguments` (extended with a `steps` namespace) |
| Harbor per-step results | `results.py._parse_multi_step_trial` (already keys by step) |
| Step failure semantics | `HookEntry.on_failure` (`fail`/`continue`) convention |
| Per-step hooks | `run_hooks`/`run_hooks_safe` + `collect_hook_outputs` (as `before_each`/`after_each`) |

### 9. Implementation map

1. **Config** — `StepConfig` dataclass + `ExecutionConfig.steps`; `from_yaml` parsing
   (per-step runner via `_parse_runner_config`, `id` uniqueness, skill/prompt xor);
   `__post_init__` normalization of single skill/prompt → 1-step;
   `resolve_skill`/`is_prompt_mode`/`eval_name` read `steps[0]`; reject `mode: batch` +
   steps; add `before_step`/`after_step` to `HooksConfig` + the `phases` list.
   (`agent_eval/config.py`)
2. **Templating** — a `steps` namespace in the argument resolver, resolved per step.
   (`agent_eval/config.py`)
3. **Local execution** — step loop in `_run_single_case`; per-step `execute()`,
   `on_failure` handling, per-step metrics in `run_result.json`; wrap each step with
   `before_step`/`after_step` (`STEP_ID`/`STEP_INDEX` env, hook-output → step env); no
   separate parallel path. (`skills/eval-run/scripts/execute.py`, `agent_eval/hooks.py`)
4. **Scoring** — `record["steps"]` sub-records; `step:` selector on `JudgeConfig`;
   step-scoped record dispatch. (`skills/eval-run/scripts/score.py`, `agent_eval/config.py`)
5. **Harbor generation** — `[[steps]]` emission + per-step `instruction.md`/verifier;
   `schema_version` 1.4; templates. (`agent_eval/harbor/tasks.py`, `templates/`)
6. **Docs + tests** — `website/reference/config/execution.md`, a multi-step example, unit
   tests for parsing/normalization/templating/scoring/Harbor emission, and an integration
   test through the jira-emulator.

## Examples

### strat-creator: create → refine → review

Replaces the `run-strat-pipeline.sh` `cli` driver with native steps. The mechanical
"create" (stage the RFE stub) moves to a `before_each` hook; the two agent stages become
steps:

```yaml
execution:
  mode: case
  steps:
    - id: refine
      skill: strategy-refine
      arguments: "STRAT-{{ input.id }} --dry-run --architecture-context .context/architecture-context"
    - id: review
      skill: strategy-review
      arguments: "STRAT-{{ input.id }} --dry-run --architecture-context .context/architecture-context"

hooks:
  before_each: [{ command: "python3 eval/stage_stub.py {{ input.id }}" }]

judges:
  - name: feasibility          # whole-case (final review artifact)
    prompt_file: eval/prompts/feasibility.md
  - name: refine_grounding     # scoped to the refine step's trace
    step: refine
    prompt_file: eval/prompts/grounding.md
```

### Wiring a prior step's output into a later prompt

```yaml
execution:
  steps:
    - id: plan
      prompt: "Draft an implementation plan for: {{ input.task }}"
    - id: build
      skill: implement
      arguments: "Follow this plan:\n{{ steps.plan.output }}"
```

## Alternatives Considered

- **A separate top-level `workflow:` key.** Rejected: a second execution model that
  duplicates `ExecutionConfig` fields and is mutually exclusive with `skill:`. Nesting
  under `execution:` makes single-skill a degenerate 1-step case — one model.
- **Untyped permissive dict, like `JudgeConfig.agent`.** Rejected: the agent block is a
  leaf; steps are the primary path and need per-step skill/prompt validation and stable
  keys. Type them; reuse only `_parse_runner_config`.
- **Explicit `outputs → inputs` wiring between steps.** Rejected as over-engineering: the
  shared per-case workspace already passes files (Harbor's own model); `{{ steps.<id> }}`
  covers explicit references.
- **A `run:` / `script` step type for shell work.** Rejected. Pre/post-pipeline shell
  belongs on `before_each`/`after_each`, and genuine inter-step shell work is served by
  **per-step hooks** (`before_step`/`after_step`, §4), which stay consistent with the hook
  model and degrade to Harbor's `workdir/setup.sh`. A bespoke step type produces no
  trace/score and doesn't round-trip.
- **Cross-step conversation resume by default.** Rejected for v1: the runner ABC is
  stateless and Harbor also starts each step fresh. A future `resume` flag can map to
  Harbor `agent.resume_trajectory`.

## Migration

No change for single-skill/prompt evals — they normalize to a 1-step pipeline and behave
identically.

```yaml
# before (still valid)
execution: { mode: case, skill: my-skill, arguments: "{prompt}" }

# after (multi-step)
execution:
  mode: case
  steps:
    - { id: a, skill: my-skill, arguments: "{{ input.prompt }}" }
    - { id: b, skill: my-other-skill, arguments: "{{ steps.a.output }}" }
```

strat-creator migrates off its `cli` shell driver onto native steps + a `before_each`
staging hook (see Examples).

## Open Questions

- **`{{ steps.<id>.output }}` contract** — last assistant message, full stdout, or an
  explicit contract file a step writes? Leaning "last assistant message" (parallels
  `record["conversation"]`), since steps have no `$GITHUB_OUTPUT` analog.
- **Whole-case judge → Harbor mapping** — emit as a single final-step verifier
  (`multi_step_reward_strategy: final`) vs a task-level `tests/`. Final-step verifier is
  simpler and keeps one test.sh per step.
- **`min_reward` source** — derive from a step-scoped judge's threshold, or add an
  explicit per-step `min_reward` field for Harbor gating parity?
- **Per-step budget vs run-level budget** — precedence and how the report's cost
  accounting attributes per-step spend.

## Future Extensions

- **Harbor emission of per-step hooks** — serialize `before_step` `command` hooks into
  each step's `workdir/setup.sh` for round-trip (local per-step hooks land in v1 per §4;
  only their Harbor emission is deferred).
- **Conversation resume** (`resume: true` on a step) mapping to Harbor
  `agent.resume_trajectory` for agents that support it.
- **Retry/backpressure** as a per-step policy (`retries`, `retry_if`) rather than a step
  type, so it can degrade to Harbor verifier + `min_reward` gating.
- **`mode: batch` + steps**, if a batched multi-step need appears.
