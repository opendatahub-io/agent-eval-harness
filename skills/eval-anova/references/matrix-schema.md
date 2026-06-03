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

**Reserved factor names:**
- `model`: Maps to `runner_kwargs["model"]` — the LLM model ID
- All other factors map to `run_skill_kwargs[factor_name]`

**Example:**

```yaml
factors:
  model:
    - claude-sonnet-4-20250514
    - claude-haiku-4-5-20251001
  effort:
    - low
    - high
  temperature:
    - 0.0
    - 0.5
    - 1.0
```

This produces 2 × 2 × 3 = 12 conditions.

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
