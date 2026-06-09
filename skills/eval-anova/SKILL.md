---
name: eval-anova
description: Run Design-of-Experiments (DoE) evaluations with ANOVA statistical analysis. Compares agent configurations across factorial experiment designs with repeated-measures statistics that account for case difficulty.
---

# eval-anova

Run a full-factorial experiment comparing agent configurations (models, effort levels, prompts) across shared test cases, then analyze results with repeated-measures ANOVA.

## Usage

```
/eval-anova                    # interactive: design → run → analyze → report
/eval-anova --dry-run          # validate config + estimate cost, no execution
/eval-anova --analyze-only     # re-analyze existing results + re-render reports
```

New to this skill? See [QUICKSTART.md](QUICKSTART.md) for from-scratch setup and run steps.

## Prerequisites

Install ANOVA dependencies:

```bash
pip install -e ".[anova]"
```

Set the results archival repo:

```bash
export RHAI_RESULTS_REPO=/path/to/rhai-results
```

## Workflow

1. **Design**: Define factors and levels in your eval YAML's `matrix:` section
2. **Preflight**: Validate archive repo, estimate cost
3. **Execute**: Run each condition × case × replication cell
4. **Score**: Composite scoring with bool/int separation and gate logic
5. **Analyze**: Repeated-measures ANOVA + Pareto frontier → `analysis.json`
6. **Report**: Render `report.html` per run + a pooled `anova-summary.html` model comparison
7. **Archive**: Results saved to git-backed repo (or local fallback)

## Reports

After analysis, generate reports from the `analysis.json` files (no re-run needed):

```bash
python3 skills/eval-anova/scripts/report.py [RUNS_DIR]   # default: $AGENT_EVAL_RUNS_DIR or eval/runs
```

This writes, into the runs directory:

- **`<run>/report.html`** + **`<run>/report.md`** — per-run ANOVA detail: condition means
  (ranked, with bars), F / p / η² tiles, a significance badge, and a per-case pass/fail matrix.
- **`anova-summary.html`** — the **model comparison**: an overall leaderboard pooled across all
  runs and cases (mean score + pass rate), a model × task heatmap (best per task highlighted),
  and a secondary per-run table linking to each run report.

`report.py` reads only `analysis.json` (and `all_results.json`); it never re-runs the experiment,
so it is safe to re-render at any time. The `/eval-anova` workflow invokes it automatically as
the Report step; `--analyze-only` re-renders too.

## Matrix Configuration

Add a `matrix:` key to your eval YAML:

```yaml
matrix:
  factors:
    model:
      - claude-sonnet-4-20250514
      - claude-haiku-4-5-20251001
    effort:
      - low
      - high
  replications: 3
```

See `references/matrix-schema.md` for the full schema.

## Statistical Methods

- **Repeated-measures ANOVA** (default): Accounts for case difficulty as a blocking factor. Correct when the same cases are evaluated under all conditions.
- **Mixed-effects model**: For multi-factor designs with crossed random effects.
- **One-way ANOVA**: Only for independent samples (cases NOT reused). Rarely appropriate.

See `prompts/interpret-anova.md` for guidance on interpreting results.
