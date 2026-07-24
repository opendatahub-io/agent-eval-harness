# Agent Eval Harness

A generic evaluation framework for **skills** and **agent capabilities**. Analyze a
skill, generate a dataset, run it, score it with LLM and code judges, and improve it
— all driven by a single declarative `eval.yaml`.

That same `eval.yaml` runs unchanged across your laptop, containers, and a platform:
drive any agent runner (**Claude Code**, **OpenCode**, ...), execute locally or on the
**Harbor** and **EvalHub** backends, export rewards for RL training with **NVIDIA
NeMo RL**, and record traces, datasets, and reports in **MLflow**.

Finer-grained control is built in: **tool interception** to stub or assert on the
agent's tool calls, **user simulation** that auto-answers `AskUserQuestion` prompts,
**permissions** to allow/deny what the agent may run, **lifecycle hooks** around
cases and scoring, and a rich HTML **report** with scoring summaries and per-case
diffs.

```bash
# 1. Add the harness to your project (it ships as a Claude Code plugin)
claude plugin install agent-eval-harness@opendatahub-skills

# 2. Point it at a skill and let it write the config
/eval-analyze --skill my-skill

# 3. Generate test cases and run the evaluation
/eval-dataset
/eval-run --model opus
```

[Get started :material-arrow-right:](get-started/index.md){ .md-button .md-button--primary }
[Browse the eval.yaml reference :material-arrow-right:](reference/eval-yaml.md){ .md-button }

---

## What it does

<div class="grid cards" markdown>

-   :material-file-cog: **Two evaluation flavors**

    ---

    Test a predefined **skill** (`execution.skill`) for correctness, quality, and
    cost — or test an agent **capability directly** (`execution.prompt`), such as
    whether an agent can navigate your documentation.

    [:octicons-arrow-right-24: Execution model](concepts/execution-model.md)

-   :material-gavel: **LLM + code judges**

    ---

    Score every case with built-in judges, inline Python checks, LLM rubrics, or
    external functions. Add pairwise A/B comparison and N-sample stability.

    [:octicons-arrow-right-24: Judges & scoring](concepts/judges.md)

-   :material-server-network: **One config, three backends**

    ---

    Run **locally** as a subprocess, in **containers** via Harbor (Podman or
    Kubernetes/OpenShift), or on the **EvalHub** platform — the backend is a CLI
    flag, never in `eval.yaml`.

    [:octicons-arrow-right-24: Execution backends](concepts/backends.md)

-   :material-robot-happy: **Any agent runtime**

    ---

    Drive Claude Code out of the box, or bring your own agent through the opaque
    CLI runner or the OpenAI Responses API runner.

    [:octicons-arrow-right-24: Runners](concepts/runners.md)

-   :material-trophy: **Reward API for RL**

    ---

    Collapse judges into a single `[0, 1]` reward for GRPO-style training via
    Harbor / NeMo Gym / SkyRL.

    [:octicons-arrow-right-24: Reward API](concepts/reward-api.md)

-   :material-chart-timeline: **MLflow-native**

    ---

    Experiments, dataset registry, hierarchical execution traces, and feedback
    sync — all opt-in with one `mlflow:` block.

    [:octicons-arrow-right-24: Tracing](concepts/tracing.md)

</div>

---

## The pipeline

Eight skills form a pipeline. Only `/eval-analyze`, `/eval-dataset`, and `/eval-run`
are required for a first run; the rest are optional.

``` mermaid
graph LR
    S[/eval-setup/] --> A[/eval-analyze/]
    A --> D[/eval-dataset/]
    D --> R[/eval-run/]
    R --> V[/eval-review/]
    R --> O[/eval-optimize/]
    V --> O
    R -.-> M[/eval-mlflow/]
    O --> R
```

[See the full pipeline guide :material-arrow-right:](guides/pipeline.md)

---

## Pick your path

<div class="grid cards" markdown>

-   :material-school: **New here?**

    ---

    Install the harness and run your first evaluation end to end.

    [:octicons-arrow-right-24: Get Started](get-started/index.md)

-   :material-book-open-variant: **Want the how-to?**

    ---

    Task-oriented guides for every skill and every backend.

    [:octicons-arrow-right-24: Guides](guides/index.md)

-   :material-lightbulb-on: **Want the why?**

    ---

    Deep dives into the execution model, judges, rewards, and tracing.

    [:octicons-arrow-right-24: Concepts](concepts/index.md)

-   :material-chef-hat: **Learn by example?**

    ---

    Worked, runnable configs for common evaluation scenarios.

    [:octicons-arrow-right-24: Cookbook](cookbook/index.md)

</div>
