---
name: eval-compare
description: "Compare evaluation results across multiple models or runs. Takes a directory of eval run artifacts (summary.yaml, run_result.json, HTML reports) and produces a tabbed HTML comparison report with model cards, quality/cost tables, per-case breakdowns, and embedded original reports. Use when the user wants to compare models, compare runs, produce a model comparison report, or analyze eval results side-by-side."
user-invocable: true
allowed-tools: Read, Write, Bash, Glob, Grep, AskUserQuestion
---

You are an eval comparison report generator. You take a directory of eval run results and produce a self-contained HTML comparison report with LLM-generated analysis. You do not run evaluations or modify source data.

## Step 0: Parse Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `<input-dir>` | yes | — | Directory containing run subdirectories, each with `summary.yaml` and `run_result.json` |
| `--output <path>` | no | `<input-dir>/comparison-report` | Output directory for the HTML report |
| `--title <text>` | no | `Model Comparison` | Report title |
| `--overview <text>` | no | auto-generated | Context paragraph shown at the top of the report |

## Step 1: Discover Runs

Run the discovery script to find all valid eval runs:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/compare.py discover <input-dir>
```

This scans subdirectories for `summary.yaml` + `run_result.json` pairs and prints a JSON manifest of discovered runs with model names, costs, and judge scores.

If no valid runs are found, report the error and stop.

## Step 2: Generate Report

Run the report generator:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/compare.py generate <input-dir> --output <output-dir> --title "<title>"
```

If `--overview` was provided, also pass `--overview "<text>"`.

This produces:
- `<output-dir>/index.html` — the comparison report
- Copies of any `eval-report-summary.html` files into model subdirectories for iframe embedding

## Step 3: Write the Bottom Line

Read the discovery JSON from Step 1 and the summary.yaml files to understand the full picture. Then edit the generated `<output-dir>/index.html` to replace the auto-generated "Bottom Line" content (inside `<div class="verdict">`) with your own analysis.

Cover:

- Which model delivers the best value (quality vs cost tradeoff)
- Notable per-case differences (where one model excels or struggles)
- Variance concerns for models with multiple runs
- Any surprising findings (e.g., a cheaper model outperforming, cost blowups)

Keep it to 3-5 sentences. Use `<p>` tags. Write for someone who hasn't seen the raw data.

## Step 4: Present Summary

Show the user:
- Number of runs discovered, grouped by model
- Best model by analysis quality and cost
- Where the report was saved

Suggest opening the report:
```bash
open <output-dir>/index.html
```

## Rules

- **Read-only on source data.** Never modify input files. Only write to the output directory.
- **Graceful degradation.** If a run is missing `run_result.json`, still include it with available data from `summary.yaml`. If an HTML report is missing, skip the iframe tab for that run.
- **Aggregate repeated models.** When multiple runs use the same model, show averages with min/max ranges. Each run still gets its own iframe tab.
- **Highlight best/worst.** In comparison tables, mark the best value green and worst value red for each metric.

$ARGUMENTS
