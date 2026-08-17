# models

The `models` block sets a default model for each of the four **roles** the harness
invokes: the skill under test, its subagents, the LLM judges, and the tool-interception
hook. Each role has its own precedence chain — a CLI flag or config field usually wins
over the block, and two roles fall back to environment variables.

```yaml title="eval.yaml"
models:
  skill: claude-opus-4-6      # the skill/prompt under test
  subagent: claude-sonnet-4-5 # subagents the skill spawns (optional)
  judge: claude-opus-4-6      # LLM and pairwise judges
  judge_effort: medium        # reasoning effort for LLM judges (optional)
  hook: claude-haiku-4-5      # AskUserQuestion auto-answering (optional)
```

All fields are optional (`ModelsConfig` defaults them to `None`). Omitting the whole
block is valid — as long as each role you actually exercise resolves to a non-empty model
through one of the fallbacks below.

## The four roles

| Role | Field | Drives | Resolution (high → low) |
| --- | --- | --- | --- |
| **skill** | `models.skill` | The skill or prompt being evaluated | `--model` → `models.skill` |
| **subagent** | `models.subagent` | Subagents the skill spawns | `--subagent-model` → `models.subagent` → *skill model* |
| **judge** | `models.judge` | LLM `prompt`/`llm_rubric` and pairwise judges | per-judge `model:` → `models.judge` → `EVAL_JUDGE_MODEL` |
| **hook** | `models.hook` | LLM answering of `AskUserQuestion` during interception | `models.hook` → built-in default (`claude-haiku-4-5`) |

The judge role also takes a reasoning-effort setting, `models.judge_effort` — see
[judge_effort](#judge_effort) below.

```mermaid
flowchart TD
    subgraph skill["skill role"]
      A["--model"] --> B["models.skill"]
    end
    subgraph subagent["subagent role"]
      C["--subagent-model"] --> D["models.subagent"] --> E["resolved skill model"]
    end
    subgraph judge["judge role"]
      F["per-judge model:"] --> G["models.judge"] --> H["EVAL_JUDGE_MODEL"]
    end
    subgraph hook["hook role"]
      I["models.hook"] --> J["built-in default (haiku)"]
    end
```

## skill

The model for the target under evaluation (both skill mode and prompt mode).

- **Precedence:** `--model` (on `/eval-run` / `execute.py`) → `models.skill`.
- **Required.** If neither resolves to a value, `eval-run` aborts:
  `ERROR: no model specified. Set --model or models.skill in eval.yaml.`

```bash
/eval-run --model opus          # overrides models.skill for this run
```

!!! tip "Model aliases"
    `--model` accepts whatever the runner CLI accepts — short aliases like `opus` or
    `sonnet` as well as pinned IDs like `claude-opus-4-6`. Pin an exact ID in
    `models.skill` for reproducible runs.

## subagent

The model used by any subagents the skill spawns. Resolved in `execute.py` as
`--subagent-model` → `models.subagent` → the resolved **skill** model, so it is never
empty. The Claude Code runner exports the resolved value as the
`CLAUDE_CODE_SUBAGENT_MODEL` environment variable into the agent subprocess.

```bash
/eval-run --model opus --subagent-model sonnet   # cheaper subagents
```

!!! warning "`CLAUDE_CODE_SUBAGENT_MODEL` from your shell is overridden"
    Because the harness always resolves a subagent model (falling back to the skill
    model) and *sets* `CLAUDE_CODE_SUBAGENT_MODEL` on the subprocess, a value you export
    in your own shell does not take effect for local runs — use `--subagent-model` or
    `models.subagent` instead.

## judge

The model for LLM judges (`prompt`, `prompt_file`, `llm_rubric`) and the pairwise
comparison judge. There is **no CLI flag** for the judge model. Resolution order:

1. the individual judge's `model:` field ([judges](../../reference/config/judges.md)),
2. `models.judge`,
3. the `EVAL_JUDGE_MODEL` environment variable.

If none resolves, LLM and pairwise judges error out asking you to set one of the three.
Deterministic judges (`check`, `builtin`, external `module`/`function`) never consume a
model, so a config with only those judges needs no judge model at all.

```yaml
models:
  judge: claude-opus-4-6

judges:
  - name: output_quality
    prompt: "Score the output 1-5 for completeness and accuracy."
    score_range: [1, 5]      # declare the scale — omitting it warns at config load
  - name: strict_rubric
    model: claude-opus-4-6   # per-judge override wins over models.judge
    llm_rubric: "Response cites a relevant source."
    feedback_type: bool      # pass/fail verdict — no scale to declare
```

```bash
export EVAL_JUDGE_MODEL=claude-opus-4-6   # last-resort default across runs
```

## judge_effort

Reasoning effort for the **single-call** LLM judges, sent as `output_config.effort` on
the Messages API request. Agent judges already carry their own knob
(`agent.runner.effort`); `judge_effort` closes the same gap for the LLM ones. Accepted
values are `low`, `medium`, `high`, `xhigh`, `max` — anything else is rejected at config
load. Resolution order:

1. the individual judge's `effort:` field,
2. `models.judge_effort`,
3. **unset** — the parameter is omitted from the request entirely.

```yaml
models:
  judge: claude-opus-4-6
  judge_effort: medium       # default for every LLM judge in this config

judges:
  - name: quick_gate
    llm_rubric: "Response cites a relevant source."
    feedback_type: bool
    effort: low              # cheap, high-volume gate
  - name: deep_quality
    prompt_file: prompts/quality.md
    score_range: [1, 5]
    effort: xhigh            # per-judge override wins
```

!!! warning "Effort is not accepted by every model"
    `output_config.effort` is rejected by some models (Sonnet 4.5 and Haiku 4.5 among
    them), and the ladder is model-dependent — Opus 4.5 supports only `low`/`medium`/`high`.
    That is why the default is *unset* rather than a value: an unconfigured judge sends
    exactly the request it sent before this field existed. If you set an effort your judge
    model does not accept, the request fails and the case is recorded as an **error
    sample** — visible in the report, and excluded from the mean rather than counted as a
    failure.

!!! tip "Effort, not temperature"
    There is deliberately no `temperature` field. Anthropic **removed** the sampling
    parameters (`temperature`, `top_p`, `top_k`) on Opus 4.7 and later — sending one
    returns a 400 — and temperature `0` never guaranteed reproducible output anyway.
    Effort is the supported knob on current models. To measure judge stability rather
    than reduce it, use [`samples:`](../../concepts/pairwise-and-sampling.md), which runs
    a judge N times per case and reports the spread.

`effort` only applies to judges that make a model call. Setting it on a `check`,
`module`/`function`, or builtin *code* judge is rejected at config load, as is setting it
alongside an `agent:` block — in both cases the value would otherwise be accepted and
silently ignored.

## hook

The model used to auto-answer `AskUserQuestion` prompts during headless
[tool interception](../../concepts/tool-interception.md). Answering is three-tier: an exact
match in `case_overrides` → an LLM call using the handler prompt plus case context
(`input.yaml` + `answers.yaml`) → the first option as a fallback. `models.hook` selects the
model for the middle (LLM) tier; when unset it defaults to a built-in Haiku model
(`claude-haiku-4-5-20251001`). The value is written into `tool_handlers.yaml` as
`hook_model`.

```yaml
models:
  hook: claude-haiku-4-5   # keep interception answering fast and cheap

inputs:
  tools:
    - match: AskUserQuestion
      prompt: "Answer as a backend engineer prioritizing correctness."
```

## Related environment variables

| Variable | Role | Notes |
| --- | --- | --- |
| `EVAL_JUDGE_MODEL` | judge | Last-resort judge model when no config/flag is set |
| `CLAUDE_CODE_SUBAGENT_MODEL` | subagent | Set *by* the runner from the resolved subagent model; forwarded to the agent subprocess |

See the full list in the [environment variables reference](../../reference/environment-variables.md).

## See also

<div class="grid cards" markdown>

- [**runner**](../../reference/config/runner.md) — the runtime that consumes these models, plus `effort`
- [**judges**](../../reference/config/judges.md) — per-judge `model:` overrides
- [**execution**](../../reference/config/execution.md) — `mode`, skill/prompt, budget, parallelism
- [**environment variables**](../../reference/environment-variables.md) — every variable the harness reads

</div>
