# Installation & setup

The harness is a **Claude Code plugin**. Installing it makes all eight skills
available: `/eval-setup`, `/eval-analyze`, `/eval-dataset`, `/eval-run`,
`/eval-review`, `/eval-mlflow`, `/eval-optimize`, and `/eval-check`.

## Requirements

- **Python 3.11+** (3.12+ if you plan to use the [Harbor](../guides/harbor.md) backend)
- **Claude Code**
- An **Anthropic API key** *or* **Google Vertex** credentials

## 1. Install the plugin

=== "From the registry"

    ```bash
    claude plugin install agent-eval-harness@opendatahub-skills
    ```

=== "As a local plugin"

    ```bash
    git clone https://github.com/opendatahub-io/agent-eval-harness
    pip install -e ./agent-eval-harness
    claude --plugin-dir ./agent-eval-harness
    ```

!!! info "Dependencies auto-install"
    The plugin's `SessionStart` hook installs the Python dependencies into an
    isolated virtual environment the first time you open a session — you don't need
    to `pip install` anything for local runs. The optional extras below are only for
    specific backends.

Optional backend extras (installed into the same environment):

| Extra | Enables | Command |
| --- | --- | --- |
| `mlflow` | Experiment tracking, datasets, traces | `pip install -e '.[mlflow]'` |
| `harbor` | Containerized execution (Podman/Kubernetes) | `pip install -e '.[harbor]'` |
| `evalhub` | The EvalHub platform adapter | `pip install -e '.[evalhub]'` |
| `openai` | The OpenAI Responses API runner | `pip install -e '.[openai]'` |
| `all` | Everything above | `pip install -e '.[all]'` |

## 2. Provide model credentials

The harness reads credentials from the environment. Use **one** of:

=== "Anthropic API"

    ```bash
    export ANTHROPIC_API_KEY=sk-ant-...
    ```

=== "Google Vertex"

    ```bash
    export ANTHROPIC_VERTEX_PROJECT_ID=my-gcp-project
    export ANTHROPIC_VERTEX_REGION=us-east5
    ```

See the [environment variables reference](../reference/environment-variables.md) for
the full list.

## 3. (Optional) Run `/eval-setup`

`/eval-setup` is a convenience command that verifies dependencies, checks your API
keys, configures MLflow, and sets the runs directory. **You can skip it** — but it's
the easiest way to stand up MLflow and confirm your environment is healthy.

```bash
/eval-setup
```

Useful flags:

| Flag | Purpose |
| --- | --- |
| `--tracking-uri <uri>` | Point MLflow at a specific server or store |
| `--skip-mlflow` | Configure everything except MLflow |
| `--runs-dir <path>` | Set where run artifacts are written |
| `--harbor` | Also install Harbor + the Kubernetes client |

### MLflow tracking (optional)

MLflow logging is **opt-in** — it only happens when your `eval.yaml` has an
[`mlflow:` block](../reference/config/mlflow.md). Pick a store:

=== "Local server"

    ```bash
    mlflow server --port 5000
    export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
    ```

=== "SQLite (self-contained)"

    ```yaml
    # in eval.yaml
    mlflow:
      experiment: my-skill-eval
      tracking_uri: sqlite:///mlflow.db
    ```

=== "Remote"

    ```bash
    export MLFLOW_TRACKING_URI=https://mlflow.example.com
    ```

!!! note "Where runs are stored"
    Run artifacts land under `$AGENT_EVAL_RUNS_DIR` (default: `eval/runs`),
    independently of MLflow. See
    [Runs directory & artifacts](../reference/runs-directory.md).

## Next step

You're ready to evaluate something.

[Run your first eval :material-arrow-right:](first-eval.md){ .md-button .md-button--primary }
