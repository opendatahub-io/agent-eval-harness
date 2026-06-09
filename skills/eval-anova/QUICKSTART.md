# eval-anova — Quickstart

Run a full-factorial (DoE) experiment comparing agent configs (models / effort / prompts)
across shared test cases, then analyze with repeated-measures ANOVA and render a
model-comparison report.

## 0. Prerequisites
- The agent-eval-harness with the eval-anova skill installed.
- Python 3.11+ and `uv` (or venv).
- Anthropic API access (direct or Vertex AI).

## 1. Install
    uv venv && source .venv/bin/activate
    uv pip install -e ".[anova]"      # scipy, statsmodels, pandas, pingouin

## 2. Environment

**Option A: Google Cloud Vertex AI (recommended for teams)**

    export CLAUDE_CODE_USE_VERTEX=1
    export CLOUD_ML_REGION=global
    export ANTHROPIC_VERTEX_PROJECT_ID=your-project-id
    gcloud auth application-default login        # one-time auth

Both agent runs (via `claude --print`) and LLM judges (via `AnthropicVertex`) use these credentials.

**Option B: Direct Anthropic API**

    export ANTHROPIC_API_KEY=sk-...

**Common (both options)**

    export RHAI_RESULTS_REPO=/path/to/results    # optional archive; local fallback if unset
    # export AGENT_EVAL_RUNS_DIR=eval/runs       # optional, this is the default

## 3. Create an eval with a matrix
Put this at `eval/<name>/eval.yaml` (or project-root `eval.yaml`):

    skill: none                       # or the skill you're testing
    execution:
      mode: case
      arguments: "{prompt}"
      timeout: 120
    runner:
      type: claude-code
    dataset:
      path: dataset/cases             # relative to THIS eval.yaml
      schema: "each case dir has input.yaml with a 'prompt' field"
    judges:
      - name: passes
        check: "..."                  # how each cell is scored (composite pass/fail)
    matrix:
      factors:
        model:
          - claude-opus-4-8
          - claude-sonnet-4-6
          - claude-haiku-4-5-20251001
      replications: 3                 # 1 = noisy screening; 3 = decent; 5+ = high confidence

Add cases under `eval/<name>/dataset/cases/<case>/input.yaml`.
See `references/matrix-schema.md` for the full matrix schema.

## 4. Validate + estimate cost (no execution)
    /eval-anova --dry-run

## 5. Run it
    /eval-anova        # design -> run every model x case x replication -> score -> ANOVA -> report
Results land in `eval/runs/anova-<timestamp>/` (analysis.json, all_results.json, cells/),
and the report step writes `report.html` per run plus a pooled `anova-summary.html`.

## 6. Re-analyze without re-running
    /eval-anova --analyze-only

## 7. Render reports manually (optional)
The report step runs automatically, but you can re-render any time from existing data:
    python3 skills/eval-anova/scripts/report.py [RUNS_DIR]   # default: $AGENT_EVAL_RUNS_DIR or eval/runs
Open `eval/runs/anova-summary.html` for the model comparison (leaderboard + model×task heatmap),
or a run's `report.html` for its ANOVA detail.

## Notes
- Use current model IDs (Opus 4.8 `claude-opus-4-8`, Sonnet 4.6 `claude-sonnet-4-6`,
  Haiku 4.5 `claude-haiku-4-5-20251001`).
- Repeated-measures ANOVA assumes the SAME cases run under every condition (it blocks on case
  difficulty). Keep your case set fixed across conditions.
- Sanity-check scoring first: if most cells are 0.0, the grader/judge is probably misconfigured
  — fix that before trusting any ANOVA result.
