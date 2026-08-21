# Glossary

Core terms used throughout the docs, each with a one-line definition and a link
to the page that covers it in depth. Terms are grouped roughly by the order you
meet them in the pipeline.

!!! tip "The one distinction to internalize"
    A **runner** is the agent runtime *inside* the box (which CLI drives the
    model — Claude Code, an OpenCode CLI, …). An **execution backend** is the
    box *around* it (Local process, Harbor container, EvalHub Job pod). The
    runner lives in `eval.yaml` under `runner:`; the backend is always a CLI
    flag (`--runner local|harbor`), **never** a config key — so one config runs
    unchanged everywhere.

## What you execute

| Term | Definition | More |
| --- | --- | --- |
| **Case** | One test case: a directory under `dataset.path` holding `input.yaml` (what the agent sees) and optional `annotations.yaml`. In `mode: case` the harness makes one agent invocation per case. | [Execution model](../concepts/execution-model.md) |
| **Batch** | `execution.mode: batch` — all cases handled in a *single* invocation via a generated `batch.yaml`; the skill/agent loops internally instead of the harness. | [Execution model](../concepts/execution-model.md) |
| **Skill mode** | `execution.skill` — invoke a predefined skill (`/my-skill --args`) and evaluate its correctness, quality, and cost. Mutually exclusive with prompt mode. | [Skill vs prompt](../guides/skill-vs-prompt.md) |
| **Prompt mode** | `execution.prompt` — send a prompt template directly to the agent with no skill wrapper, to test raw agent capability (e.g. agentic-docs testing). Mutually exclusive with skill mode. | [Skill vs prompt](../guides/skill-vs-prompt.md) |

!!! note "`mode` vs. `skill`/`prompt` are orthogonal"
    `execution.mode` (`case` | `batch`) controls *how many* invocations;
    `execution.skill` vs `execution.prompt` controls *what* is invoked. Any of
    the four combinations is valid.

## Where and how it runs

```mermaid
flowchart LR
    C["eval.yaml<br/>(runner: type)"] --> R["Runner<br/>(agent runtime)"]
    R -->|--runner local| L["Local process"]
    R -->|--runner harbor| H["Harbor container"]
    R -->|platform| E["EvalHub Job pod"]
```

| Term | Definition | More |
| --- | --- | --- |
| **Runner** (agent runtime) | The agent CLI/harness that drives the model, selected by `runner.type` (`claude-code`, `cli`, …) with runtime-specific knobs (`effort`, `settings`, `plugin_dirs`, `env`, `system_prompt`, `command`, `workspace_mode`). | [Runners](../concepts/runners.md) · [runner config](config/runner.md) |
| **Execution backend / substrate** | The environment the run executes in — Local, Harbor (containers), or EvalHub (platform Job pod). Chosen with a CLI flag, never in `eval.yaml`. | [Backends](../concepts/backends.md) |
| **Workspace** | The isolated per-case directory the runner executes in. `dataset.workspace.files` whitelists case files to copy in; `runner.workspace_mode: repo` runs in the real repository instead of an isolated copy. | [dataset config](config/dataset.md) · [eval-run](../guides/eval-run.md) |
| **Run** | One execution of the suite, stored under `$AGENT_EVAL_RUNS_DIR` (default `eval/runs/<run-id>/`) with artifacts, scores, and `report.html`. | [Runs directory](runs-directory.md) |

## Scoring and gating

| Term | Definition | More |
| --- | --- | --- |
| **Judge** | A scorer applied to each case. Five types by which field is set: `builtin`, inline `check` (Python), LLM (`prompt`/`prompt_file`/`llm_rubric`), tool-using `agent` (`agent:` block), or external `module`/`function`. | [Judges](../concepts/judges.md) · [judges config](config/judges.md) |
| **Threshold** | A per-judge regression gate. Seven valid keys: `min_mean`, `min_pass_rate`, `min_win_rate`, `max_error_rate` (the one *maximum* — an opt-in coverage gate), `min_alpha`, `min_human_agreement`, and `min_panel_alpha`, plus the reserved `simulator` mapping key (`max_fallback_rate`, `min_gold_agreement`, `min_cross_simulator_agreement`). | [Thresholds](../concepts/thresholds.md) · [thresholds config](config/thresholds.md) |
| **Reward** | Optional collapse of per-judge results into a single scalar in `[0, 1]` for RL training (GRPO) — either a single `judge` or a `formula` (`weighted` or a Python expression), with optional `gate`. | [Reward API](../concepts/reward-api.md) · [reward config](config/reward.md) |

## Data provenance and capture

| Term | Definition | More |
| --- | --- | --- |
| **Seed** | One entry in a synthetic `generation.seeds` list — a `category` + `count` plus exactly one prompt discriminator (`builtin`, `prompt_file`, or inline `prompt`) that generates that many cases. | [generation config](config/generation.md) |
| **Provenance** | `generation.strategy` — how `/eval-dataset` sources cases: `skill` (agent authors from skill analysis, default), `synthetic` (LLM generates from seeds), or `from-traces` (extracted from MLflow traces). | [generation config](config/generation.md) |
| **Trace** | The execution record captured per case (stdout, stderr, parsed events, metrics) per the `traces` block, made available to judges and optionally logged to MLflow. | [Tracing](../concepts/tracing.md) · [traces config](config/traces.md) |
| **Tool interception** | Headless handling of tools the agent would otherwise block on: `inputs.tools[].match` describes what to intercept, `prompt` how to answer it. | [Tool interception](../concepts/tool-interception.md) · [inputs.tools config](config/inputs-tools.md) |
| **`case_overrides`** | The first, exact-match tier of AskUserQuestion answering during tool interception (exact `case_overrides` → LLM call via `models.hook` → static fallback). | [Tool interception](../concepts/tool-interception.md) |

## Measurement validity & reliability

Seven terms that sound alike and measure different things. The narrative that
ties them together is [Measurement validity](../concepts/measurement-validity.md).

| Term | Definition | More |
| --- | --- | --- |
| **stability** | The raw within-run exact-match agreement of a sampled judge (`stable` = every sample identical, none errored). Uncorrected for chance, and blind to *how far apart* disagreeing samples are. | [Pairwise & sampling](../concepts/pairwise-and-sampling.md) |
| **`stability.irr`** | The chance-corrected coefficient over the same case × sample matrix: the **single-judge self-consistency alpha (upper bound on inter-rater reliability)** — a judge can agree perfectly with itself and still be biased. Gated by `min_alpha`. | [thresholds config](config/thresholds.md#min_alpha-the-reliability-gate) |
| **panel** | Cross-model inter-judge agreement: Krippendorff's alpha over the cases × models matrix of a [judge panel](config/judges.md#judge-panels-cross-family-ensembles) (`judges[].model` as a list), each model's reduced verdict acting as one rater. Gated by `min_panel_alpha`. | [judges config](config/judges.md#judge-panels-cross-family-ensembles) |
| **`human_agreement`** | Criterion validity against a **single human reviewer**: the judge-vs-human kappa/alpha merged by `score.py calibration` from `/eval-review` verdicts. Gated by `min_human_agreement`. | [/eval-review](../guides/eval-review.md#calibration-anchor-your-judges-to-a-human) |
| **simulator calibration** | The V2 (simulated-user) check: `inputs.tools[].calibration: true` shadow-runs the LLM answering tier **held out** on every override-answered question and compares it to the human-authored override. Aggregated into `summary['simulator']`, gated by the reserved `thresholds.simulator` key. | [inputs.tools config](config/inputs-tools.md) |
| **`cross_simulator`** | Simulator-choice sensitivity: how often [`models.hook_shadow`](config/models.md#hook_shadow) shadow models answer intercepted questions exactly like the primary hook. Gated by `min_cross_simulator_agreement`. | [models config](config/models.md#hook_shadow) |
| **clarity** | Instrument clarity (`score.py clarity`): can several rater models apply the judge's rubric consistently at all? A property of the rubric, **not** rater validity — a clear rubric can still measure the wrong thing. | [judges config](config/judges.md#instrument-clarity-diagnostic) |

!!! note "Two unrelated 'calibrations'"
    **Simulator calibration** (`inputs.tools[].calibration`, above) measures the
    simulated *user*; **judge calibration** (`score.py calibration` →
    `human_agreement`) measures the *judges* against a human reviewer. Same word,
    different layer.

## See also

<div class="grid cards" markdown>

- [**The eval.yaml schema**](eval-yaml.md) — every config key in one place
- [**Execution model**](../concepts/execution-model.md) — case/batch × skill/prompt
- [**Runners**](../concepts/runners.md) vs [**Backends**](../concepts/backends.md) — the runtime/substrate split
- [**Your first eval**](../get-started/first-eval.md) — the terms in action

</div>
