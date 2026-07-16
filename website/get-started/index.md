# Get Started

The harness ships as a **Claude Code plugin**. Once it's installed, you drive it
entirely through slash commands (`/eval-*`). This section takes you from zero to a
scored HTML report.

## The shortest path

```bash
/eval-analyze --skill my-skill   # writes eval.yaml by reading your skill
/eval-dataset                    # generates test cases
/eval-run --model opus           # executes, scores, and builds the report
```

That's it — `/eval-setup` and `/eval-mlflow` are optional (dependencies auto-install,
and MLflow logging is opt-in).

## How the pieces fit

``` mermaid
graph TD
    subgraph required ["Required for a first run"]
        A["/eval-analyze<br/>writes eval.yaml"] --> D["/eval-dataset<br/>writes test cases"]
        D --> R["/eval-run<br/>execute + score + report"]
    end
    subgraph optional ["Optional"]
        S["/eval-setup<br/>env + MLflow"] -.-> A
        R -.-> V["/eval-review<br/>human feedback"]
        R -.-> O["/eval-optimize<br/>auto-refine"]
        R -.-> M["/eval-mlflow<br/>log + trace"]
    end
```

## In this section

<div class="grid cards" markdown>

-   :material-download: **[Installation & setup](installation.md)**

    ---

    Add the plugin, install dependencies, and configure API keys and MLflow.

-   :material-play: **[Your first eval (skill mode)](first-eval.md)**

    ---

    Analyze a skill, generate a dataset, run it, and read the report.

-   :material-file-document-multiple: **[Your first agentic-docs eval](agentic-docs.md)**

    ---

    Test whether an agent can navigate and correctly use your documentation.

-   :material-chart-box: **[Reading the report](reading-the-report.md)**

    ---

    Understand scores, per-case detail, diffs, and cost.

</div>

!!! tip "New to the terminology?"
    Two words are worth pinning down before you start: a **runner** is the *agent
    runtime* (`claude-code`, `cli`, `responses-api`), while an **execution backend**
    is *where* it runs (Local, Harbor, EvalHub). See the
    [Glossary](../reference/glossary.md).
