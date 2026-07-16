# Your first eval (skill mode)

This walkthrough evaluates a predefined skill end to end: **analyze → dataset → run →
report**. It assumes you've [installed the plugin](installation.md) and have a skill
you want to test (say, `my-skill`).

!!! abstract "What you'll produce"
    An `eval.yaml` config, a small dataset of test cases, and a scored HTML report
    under `eval/runs/<run-id>/`.

## Step 1 — Analyze the skill

`/eval-analyze` reads your skill's `SKILL.md` (following any sub-skills it calls) and
writes an `eval.yaml` describing how to run and score it.

```bash
/eval-analyze --skill my-skill
```

It generates, among other things:

- an `execution` block (`mode: case` or `batch`, and the `arguments` template),
- a natural-language `dataset.schema`,
- suggested [judges](../concepts/judges.md), and
- regression [thresholds](../concepts/thresholds.md).

The result is a plain YAML file you can edit. A minimal skill-mode config looks like:

```yaml title="eval.yaml"
name: my-skill-eval

execution:
  mode: case              # one invocation per test case
  skill: my-skill         # the skill under test
  arguments: "{prompt}"   # resolved per case from input.yaml

models:
  skill: claude-opus-4-6  # or pass --model on the CLI
  judge: claude-opus-4-6

dataset:
  path: eval/dataset/cases
  schema: |
    Each case has an input.yaml with a 'prompt' field.

judges:
  - name: has_content
    check: |
      content = outputs.get("main_content", "")
      if len(content.strip()) < 100:
          return False, f"Output too short ({len(content.strip())} chars)"
      return True, "OK"

  - name: output_quality
    prompt: |
      Score the output 1-5 for completeness, clarity, and accuracy.

thresholds:
  has_content: { min_pass_rate: 1.0 }
  output_quality: { min_mean: 3.5 }
```

!!! tip "Reference"
    Every key above is documented in the [eval.yaml reference](../reference/eval-yaml.md).

## Step 2 — Generate a dataset

`/eval-dataset` populates `dataset.path` with test cases that match your `schema`. By
default it authors a small, coverage-oriented set (a simple case, a complex one, an
edge case, plus one per judge requirement).

```bash
/eval-dataset
```

Each case is a directory:

```text
eval/dataset/cases/
├── case-001-simple/
│   └── input.yaml          # what the agent sees (e.g. a 'prompt' field)
├── case-002-complex/
│   └── input.yaml
└── case-003-edge/
    ├── input.yaml
    └── annotations.yaml    # optional metadata judges can read
```

!!! warning "External systems"
    If your schema references an external system, generated cases use
    `TODO_<SYSTEM>_<FIELD>` placeholders. **Replace them with real values before you
    run**, or the skill will query nothing and fail silently.

## Step 3 — Run the evaluation

`/eval-run` prepares an isolated workspace per case, executes the skill headlessly,
collects its outputs, scores them with your judges, and writes an HTML report.

```bash
/eval-run --model opus
```

Common flags:

| Flag | Effect |
| --- | --- |
| `--model <name>` | Model for the skill under test (overrides `models.skill`) |
| `--cases <ids>` | Run only specific cases |
| `--baseline <run-id>` | Add a pairwise A/B comparison against a prior run |
| `--no-llm-judges` | Skip LLM judges (fast, cheap dry run) |

## Step 4 — Read the report

Open the generated report:

```text
eval/runs/<run-id>/report.html
```

It shows per-judge pass rates and mean scores, a per-case breakdown with each judge's
rationale, captured artifacts, and cost/token metrics.

[Understand the report :material-arrow-right:](reading-the-report.md){ .md-button }

## Where to go next

<div class="grid cards" markdown>

-   :material-tune: **Improve the skill**

    ---

    Feed results into the human review or automated optimization loop.

    [:octicons-arrow-right-24: /eval-review](../guides/eval-review.md) ·
    [/eval-optimize](../guides/eval-optimize.md)

-   :material-file-document: **Test docs instead of a skill**

    ---

    Use prompt mode to evaluate whether agents can use your documentation.

    [:octicons-arrow-right-24: Agentic-docs eval](agentic-docs.md)

-   :material-server: **Scale it out**

    ---

    Run the same config in containers or on the platform.

    [:octicons-arrow-right-24: Harbor](../guides/harbor.md) ·
    [EvalHub](../guides/evalhub.md)

</div>
