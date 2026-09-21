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
  analysis:            # optional
    correction: holm   # holm (default) | fdr_bh | none
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

### `analysis.correction` (optional, default: `holm`)

The multiple-comparison correction applied across the ANOVA's family of term
tests (all main effects plus interactions from the one fitted model):

- `holm` (default) — Holm step-down; controls the family-wise error rate.
- `fdr_bh` (alias `bh`) — Benjamini–Hochberg; controls the false discovery
  rate. Less conservative, appropriate for screening many factors.
- `none` — no correction; significance is judged on raw p-values.

Both raw and adjusted p-values are always reported in `anova.json`;
`significant` is computed on the adjusted value (on raw when `none`). Terms
whose test is degenerate (no finite p) are excluded from the family and listed
under `excluded_terms`. The `--correction` CLI flag on `orchestrate.py`
overrides this key.

The same method also corrects the **post-hoc pairwise level contrasts** that
`anova.json` carries under a top-level `contrasts` key — one block per factor
with ≥2 levels, each pair reporting `estimate` (composite-scale difference,
`a − b`), `se`, `p_raw`, `p_adjusted`, and `significant`. The correction
family is the pairwise contrasts *within that factor* (never pooled across
factors), and each block carries the factor's omnibus adjusted p for context —
contrasts are computed regardless of the omnibus outcome. They come from the
already-fitted model (no refitting): pingouin paired tests for single-factor
designs, coefficient contrasts on the mixed model otherwise — flagged
`contrast_type: reference-cell` when the model includes interactions (level
differences at the other factors' reference levels, not marginal means).
Degenerate pairs (no finite p, e.g. zero-variance paired differences) are
excluded from the family with a `reason`, never given a fabricated p-value.

## Full Factorial Expansion

All combinations of factor levels are generated. For N factors with levels L1, L2, ..., LN, the total number of conditions is L1 × L2 × ... × LN.

Total runs = conditions × cases × replications.

## Cost Estimation

```
total_runs = n_conditions × n_cases × replications
estimated_cost = total_runs × avg_cost_per_run
```

Use `--dry-run` to see the cost estimate before executing.
