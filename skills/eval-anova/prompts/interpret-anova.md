# Interpreting ANOVA Results

## Key Values

- **F-statistic**: Ratio of between-group variance to within-group variance. Higher = larger effect.
- **p-value**: Probability of observing this F-statistic under the null hypothesis (no real difference). Below alpha (typically 0.05) = statistically significant.
- **Effect size (eta-squared)**: Proportion of total variance explained by the factor. Small (<0.06), medium (0.06-0.14), large (>0.14).

## Interpreting Results

### Significant result (p < 0.05)

The factor (e.g., model choice) has a statistically significant effect on composite scores. Check:

1. **Effect size**: Is it practically meaningful, or just statistically detectable?
2. **Direction**: Which level performs better? Look at condition means.
3. **Pareto frontier**: Which conditions offer the best cost/quality trade-off?

### Non-significant result (p >= 0.05)

The data does not provide sufficient evidence that the factor affects scores. This does NOT mean there is no effect — consider:

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
- **Interactions**: Does the effect of one factor depend on the level of another?
- **Random effects**: How much variance is attributable to case difficulty?

Check interaction terms before interpreting main effects — a significant interaction means the main effect story is incomplete.
