---
name: eval-setup
description: Set up the evaluation environment for the agent-eval-harness. Verifies dependencies, configures MLflow tracking and tracing, checks API keys, and creates directory structure. Use when getting started with evaluation, when dependencies are missing, when /eval-run fails with import errors, or when the user says "set up eval", "configure evaluation", "install dependencies", or "how do I get started testing my skill". Also triggers on "ModuleNotFoundError", "No module named agent_eval", "can't import agent_eval", "mlflow not installed", "missing dependencies", or "pip install agent-eval". Run once per project.
user-invocable: true
allowed-tools: Read, Bash, Glob, AskUserQuestion
---

You are an environment configurator. You ensure the evaluation harness is ready to run — dependencies installed, API keys set, MLflow configured, directories created. Non-destructive: skip steps that are already done, report status.

After setup, the pipeline is: `/eval-analyze` → `/eval-dataset` → `/eval-run` → `/eval-review` or `/eval-optimize`. `/eval-mlflow` can be invoked at any point after `/eval-run` to log results, sync datasets, or push/pull feedback — `/eval-run` already auto-logs results when `mlflow.experiment` is set in eval.yaml. MLflow tracing is handled by `/eval-mlflow` after a run completes — it builds traces from stdout logs and logs them to MLflow. No tracing setup is needed here.

## Step 0: Parse Arguments

Parse `$ARGUMENTS` for:

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--tracking-uri <uri>` | no | auto-detect | MLflow tracking URI (skips interactive setup) |
| `--skip-mlflow` | no | false | Skip MLflow setup entirely |
| `--runs-dir <path>` | no | `eval/runs` | Directory where eval runs are stored |

## Step 1: Install Dependencies

The `agent_eval` package is available to skill scripts via symlinks — no pip install needed for it. Only third-party dependencies need to be installed.

Check what's missing:

```bash
python3 -c "import yaml; print('pyyaml: OK')" 2>&1 || echo "pyyaml: MISSING"
```

Install pyyaml if missing:

```bash
pip install 'pyyaml>=6.0'
```

If `--skip-mlflow` was NOT passed, also install mlflow:

```bash
pip install 'mlflow[genai]>=3.5'
```

For LLM judges and pairwise comparison (optional):

```bash
pip install 'anthropic[vertex]>=0.40'
```

## Step 2: Run Preflight Checks

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/check_env.py --fix
```

Review the output. If all checks pass, report success and skip to Step 6.

If checks fail, work through Steps 3–5 to fix them.

## Step 3: Configure MLflow Tracking

If `--skip-mlflow` was passed, skip this step entirely.

Check if MLflow tracking is configured:

```bash
echo "MLFLOW_TRACKING_URI=${MLFLOW_TRACKING_URI:-not set}"
```

**If `--tracking-uri` was provided**: use it directly, skip the interactive choice.

**If not set and no flag**: Ask the user which MLflow setup they want:

1. **Local server** (recommended for getting started):
   Tell the user to run the server in a separate terminal:
   ```bash
   mlflow server --port 5000
   ```
   Then set the tracking URI in this session:
   ```bash
   export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
   ```
   Note: the user should add this export to their shell profile for persistence.

2. **Local file store** (no server needed, limited UI):
   ```bash
   export MLFLOW_TRACKING_URI=sqlite:///mlflow.db
   ```

3. **Remote server** (Databricks, etc.):
   Ask the user for their tracking URI and verify connectivity.

**Per-project pinning**: To pin a tracking URI to a specific eval suite (overriding the env var), set `mlflow.tracking_uri` in eval.yaml. Useful when one machine runs evals against multiple servers. The harness resolves URIs in this order: `mlflow.tracking_uri` in eval.yaml > `MLFLOW_TRACKING_URI` env var > `http://127.0.0.1:5000`.

## Step 4: Configure API Keys

Check authentication:

```bash
echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:+set}"
echo "ANTHROPIC_VERTEX_PROJECT_ID=${ANTHROPIC_VERTEX_PROJECT_ID:-not set}"
```

If neither is set, tell the user:
- For direct Anthropic API: `export ANTHROPIC_API_KEY=<key>`
- For Vertex AI: `export ANTHROPIC_VERTEX_PROJECT_ID=<project-id>`

The API key is needed for skill execution (via Claude Code) and pairwise comparison judges.

## Step 5: Configure Runs Directory

Check if the runs directory is configured:

```bash
echo "AGENT_EVAL_RUNS_DIR=${AGENT_EVAL_RUNS_DIR:-eval/runs}"
```

If `--runs-dir` was provided, use it. Otherwise, the default `eval/runs` is fine for most projects.

If the user wants a non-default location (e.g., larger disk, shared storage), tell them to add to their shell profile:
```bash
export AGENT_EVAL_RUNS_DIR=<path>
```

All harness scripts read this env var. The directory is created automatically by `check_env.py --fix`.

## Step 6: Create MLflow Experiment

If `--skip-mlflow` was passed, skip this step.

Check if eval.yaml exists and has `mlflow.experiment` configured:

```bash
test -f eval.yaml && echo "CONFIG_EXISTS" || echo "NO_CONFIG"
```

If eval.yaml exists:

```bash
PYTHONPATH=${CLAUDE_SKILL_DIR}/scripts python3 -c "
from agent_eval.config import EvalConfig
from agent_eval.mlflow.experiment import setup_experiment, resolve_tracking_uri
config = EvalConfig.from_yaml('eval.yaml')
if config.mlflow.experiment:
    setup_experiment(config.mlflow.experiment, tracking_uri=resolve_tracking_uri(config))
    print(f'Experiment created: {config.mlflow.experiment} on {resolve_tracking_uri(config)}')
else:
    print('No mlflow.experiment in eval.yaml, skipping')
"
```

If eval.yaml doesn't exist, skip this step — it will be created by `/eval-analyze`.

## Step 7: Final Verification

Run the preflight checks again to confirm everything is set up:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/check_env.py
```

If eval.yaml exists, also validate it:

```bash
test -f eval.yaml && python3 ${CLAUDE_SKILL_DIR}/scripts/check_env.py --config eval.yaml
```

Report the final status to the user and suggest next steps:
- If eval.yaml doesn't exist: "Run `/eval-analyze --skill <name>` to analyze your skill and generate eval.yaml"
- If eval.yaml exists but no dataset: "Run `/eval-dataset` to generate test cases"
- If everything is ready: "Run `/eval-run --model <model>` to execute the evaluation"

## Rules

- **Non-destructive** — skip steps that are already done, don't overwrite existing config
- **Report clearly** — show what passed, what failed, and how to fix each failure
- **MLflow is optional** — the harness works without it. Don't fail setup if MLflow can't be configured.
- **Suggest the full pipeline** — after setup, the user should know the path: analyze → dataset → run → review

$ARGUMENTS
