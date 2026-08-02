# Matrix Configuration Schema

## Location

Add the `matrix:` key to your eval YAML file (e.g., `eval.yaml`). The matrix config coexists with existing eval config — unknown keys are ignored by `EvalConfig.from_yaml()`.

## Schema

```yaml
matrix:
  factors:
    <factor_name>:
      - <level_1>
      - <level_2>
      # ... more levels
    <another_factor>:
      - <level_a>
      - <level_b>
  replications: <int>  # default: 1
```

## Fields

### `factors` (required)

A mapping of factor names to their levels. Each factor must have at least one level.

**How factor levels reach the run (per matrix cell):**
- `model` → the runner's `--model` (the LLM model ID)
- `effort` → the runner's `--effort` (thinking-effort level)
- `subagent` / `subagent_model` → `--subagent-model`
- any other factor → `--input-override <name>=<level>`, which merges the value
  into the case's `input.yaml`. It then reaches the run only if the runner
  consumes it — as `{name}` in a `cli` runner command, or `{{ input.name }}` in
  `execution.arguments` / `execution.prompt`. A factor that nothing consumes
  still defines conditions but won't change behaviour.

**Example:**

```yaml
factors:
  model:
    - claude-opus-4-8
    - claude-sonnet-4-6
  effort:
    - low
    - high
  temperature:
    - 0.0
    - 0.5
    - 1.0
```

This produces 2 × 2 × 3 = 12 conditions. (`temperature` here is a non-model
factor, so it only affects a run if the runner's command/prompt consumes
`{temperature}` / `{{ input.temperature }}` — see the mapping above.)

### `replications` (optional, default: 1)

Number of times to repeat each condition × case combination. More replications reduce noise but increase cost linearly.

**Guidelines:**
- 1 replication: Quick screening, high noise
- 3 replications: Good balance for most evaluations
- 5+ replications: High-confidence results, expensive

## Full Factorial Expansion

All combinations of factor levels are generated. For N factors with levels L1, L2, ..., LN, the total number of conditions is L1 × L2 × ... × LN.

Total runs = conditions × cases × replications.

## Cost Estimation

```
total_runs = n_conditions × n_cases × replications
estimated_cost = total_runs × avg_cost_per_run
```

Use `--dry-run` to see the cost estimate before executing.
