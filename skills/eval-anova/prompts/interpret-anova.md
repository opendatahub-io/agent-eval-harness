# Interpreting ANOVA Results

## Key Values

- **significant**: The verdict to report. It is judged on `p_adjusted` under the named `correction` (Holm by default; on raw p only when `correction` is `none`) — never call a term significant from a raw p that the correction rejected, and always name the method next to an adjusted value.
- **p-value**: Probability of observing a test statistic this extreme under the null hypothesis (no real difference). `p_values` holds the raw per-term values; `p_adjusted` the multiplicity-corrected ones. A `null` p is a degenerate term (listed in `excluded_terms`): no evidence either way — do not report it as "not significant".
- **F-statistic**: Ratio of between-group variance to within-group variance. Higher = larger effect. Single-factor designs only — the multi-factor mixed model reports one joint Wald p per term and no overall F.
- **Effect size (eta-squared)**: Proportion of total variance explained by the factor (`details[].ng2`, single-factor designs). Small (<0.06), medium (0.06-0.14), large (>0.14).
- **contrasts**: Per factor, every pair of levels with `estimate` (`a − b` on the composite scale), `se`, `p_raw`, `p_adjusted`, and `significant`, corrected within the factor by the same method. `contrast_type` is `paired` (single factor), `marginal`, or `reference-cell` (interactions present: the difference at the other factors' reference levels, not a marginal mean). A pair with `p_adjusted: null` carries a `reason` and is no evidence either way.

## Interpreting Results

### Significant result (`significant: true` — adjusted p below alpha)

The factor (e.g., model choice) has a statistically detectable effect on composite scores. Check:

1. **Which levels**: Read `contrasts.<factor>.pairs` — report the pair's estimate and its *own* adjusted p, not the omnibus p.
2. **Effect size**: Is it practically meaningful, or just statistically detectable?
3. **Direction**: Which level performs better? The sign of the contrast estimate, confirmed against the condition means.
4. **Pareto frontier**: Which conditions offer the best cost/quality trade-off?

### Non-significant result (`significant: false` — adjusted p at or above alpha)

The data does not provide sufficient evidence that the factor affects scores. This does NOT mean there is no effect — non-significance is not equivalence. Consider:

1. **Sample size**: More cases or replications may reveal a real effect.
2. **Variance**: High within-condition variance may mask real differences.
3. **Effect size**: The true effect may be too small to matter in practice.

## Why Repeated-Measures?

In agent evaluation, the same test cases are typically evaluated under all conditions. Case difficulty is a major source of variance — a hard case is hard for all models.

- **Plain one-way ANOVA** treats all observations as independent, mixing case difficulty with model effects. This can either inflate significance (Type I error) or hide real effects (Type II error).
- **Repeated-measures ANOVA** accounts for case identity, isolating the factor effect from case variance. This gives more accurate and more powerful tests.

## Multi-Factor Designs

With multiple factors (e.g., model × effort), the mixed-effects model reports:

- **Main effects**: Does each factor independently affect scores?
- **Interactions**: Does the effect of one factor depend on the level of another? Interaction terms appear as `a:b` keys in `p_values`/`p_adjusted` and count toward the correction family (`family_size`).
- **Random effects**: How much variance is attributable to case difficulty?

Check interaction terms before interpreting main effects — a significant interaction means the main effect story is incomplete, and the per-factor `contrasts` are then reference-cell (read `condition_summaries` for the per-cell direction).

## Per-Judge Screening

When `anova.json` carries a `per_judge` block (`--per-judge` / `matrix.analysis.per_judge`), each judge's rows are Benjamini-Hochberg-corrected as one family across all (judge, term) tests, separate from the composite's family. Report them as leads ("which judge moves?"), not as findings; judges under `per_judge.excluded` were not tested — quote their `reason`.
