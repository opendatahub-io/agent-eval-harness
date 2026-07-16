# Guides

Task-oriented how-tos for the harness. Start with the pipeline overview, then dive
into the skill that matches your step. Every guide uses the same `eval.yaml` — only
the CLI flags change between local, containerized, and platform runs.

!!! tip "New here?"
    Read [Skill mode vs prompt mode](skill-vs-prompt.md) first — it decides which
    `eval.yaml` shape (and which guides) apply to you.

## Orientation

<div class="grid cards" markdown>

-   :material-map: **The eval pipeline**

    ---

    How setup, analyze, dataset, run, review, optimize, and mlflow fit together.

    [:octicons-arrow-right-24: Pipeline at a glance](pipeline.md)

-   :material-source-branch: **Skill vs prompt mode**

    ---

    Test a predefined skill (`execution.skill`) or an agent capability directly
    (`execution.prompt`).

    [:octicons-arrow-right-24: Choose a mode](skill-vs-prompt.md)

</div>

## The eval-* skills

The `/eval-*` slash commands drive the workflow in order. Each maps to a skill under
[`skills/`](https://github.com/opendatahub-io/agent-eval-harness/tree/main/skills).

<div class="grid cards" markdown>

-   :material-file-cog: **/eval-analyze**

    ---

    Understand a skill or docs and generate an `eval.yaml` (+ `eval.md`).

    [:octicons-arrow-right-24: Generate a config](eval-analyze.md)

-   :material-database-plus: **/eval-dataset**

    ---

    Build test cases by skill authoring, synthetic generation, or from traces.

    [:octicons-arrow-right-24: Build a dataset](eval-dataset.md)

-   :material-play-circle: **/eval-run**

    ---

    Execute the suite, collect artifacts, score with judges, build the HTML report.

    [:octicons-arrow-right-24: Run an eval](eval-run.md)

-   :material-account-check: **/eval-review**

    ---

    Present results, collect human feedback, and propose targeted changes.

    [:octicons-arrow-right-24: Review results](eval-review.md)

-   :material-tune: **/eval-optimize**

    ---

    Run the automated refinement loop (composes with `/eval-run`) until judges pass.

    [:octicons-arrow-right-24: Optimize a skill](eval-optimize.md)

-   :material-chart-line: **/eval-mlflow**

    ---

    Sync datasets, log run results, and push/pull trace feedback.

    [:octicons-arrow-right-24: Log to MLflow](eval-mlflow.md)

-   :material-stethoscope: **/eval-check**

    ---

    Scan the whole harness for skill/command overlap and configuration issues.

    [:octicons-arrow-right-24: Health-check](eval-check.md)

</div>

## Running headless & at scale

The same `eval.yaml` runs unchanged across execution backends — the backend is a
`--runner` CLI flag, never a config key.

<div class="grid cards" markdown>

-   :material-robot: **Running headless**

    ---

    Tool interception auto-answers `AskUserQuestion` and gates external services so
    skills run unattended.

    [:octicons-arrow-right-24: Headless execution](headless.md)

-   :material-ship-wheel: **Harbor (containers)**

    ---

    Run in containers via Podman (local) or Kubernetes/OpenShift.

    [:octicons-arrow-right-24: Run on Harbor](harbor.md)

-   :material-cloud-cog: **EvalHub**

    ---

    Run the eval in-process inside an EvalHub Job pod.

    [:octicons-arrow-right-24: Run on EvalHub](evalhub.md)

</div>

## Continuous integration

<div class="grid cards" markdown>

-   :material-source-pull: **CI & regression gating**

    ---

    Wire evals into CI with [thresholds](../concepts/thresholds.md) as the gate.

    [:octicons-arrow-right-24: CI integration](ci.md)

</div>

!!! note "Looking for field-by-field details?"
    These guides are how-tos. For the exhaustive config reference, see the
    [eval.yaml schema](../reference/eval-yaml.md); for the underlying ideas, see
    [Concepts](../concepts/index.md).
