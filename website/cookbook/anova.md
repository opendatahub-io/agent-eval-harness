# Comparing runs with ANOVA

This recipe runs a Design-of-Experiments eval over four **real bugfix PRs** and
measures whether giving the agent a knowledge-graph MCP server
(`context=cognee`) actually beats a bare agent (`context=none`) at repo-editing
quality. It uses the generic [`cli` runner](../concepts/runners.md) — no Harbor,
no committed credentials — and the [`/eval-anova`](../guides/eval-anova.md)
orchestrator to fan the eval out, run the ANOVA, and render the comparison.

!!! info "Concept vs. cookbook"
    This page is task-oriented. For the statistics behind it — factorial design,
    repeated-measures vs mixed-effects ANOVA, the Pareto frontier — see
    [Analysis of variance](../concepts/anova.md).

The complete, runnable example lives at
[`eval/anova-example/`](https://github.com/opendatahub-io/agent-eval-harness/tree/main/eval/anova-example)
(README, `eval.yaml`, `solve.sh`, MCP configs, dataset, and committed
`sample-runs/`).

## Recipe 1 — a context A/B on one model

The `matrix:` block varies one non-model factor (`context`) on a single model:

```yaml title="eval.yaml (excerpt)"
matrix:
  factors:
    model:
      - claude-sonnet-4-6
    context:
      - cognee
      - none
  replications: 1
```

`model` maps to the runner's `--model`. `context` is a **non-model factor**: the
orchestrator injects it with `execute.py --input-override context=<level>`,
merging it into each case's `input.yaml`, so `{context}` resolves in the `cli`
command and selects the right MCP config file.

```yaml title="eval.yaml (excerpt)"
execution:
  mode: case
  arguments: "{prompt}"        # unused by solve.sh (it reads the prompt from input.yaml)
  timeout: 1800

runner:
  type: cli
  command: >-
    bash {config_dir}/solve.sh {workspace} {output_dir} {model} {config_dir}/mcp-{context}.json

models:
  judge: claude-sonnet-4-6

dataset:
  path: dataset

outputs:
  - path: output
    schema: "solution.diff — the agent's changes captured by solve.sh"
```

The `{config_dir}` placeholder points at the eval directory, so `solve.sh` and
the MCP configs resolve no matter where you run from. The two `context` levels
are just two MCP files:

```json title="mcp-cognee.json"
{ "mcpServers": { "cognee": { "type": "streamable-http",
    "url": "${COGNEE_MCP_URL:-http://localhost:8321/mcp}" } } }
```

```json title="mcp-none.json"
{ "mcpServers": {} }
```

Two judges score each run — an objective gate plus a quality rubric:

```yaml title="eval.yaml (excerpt)"
judges:
  - name: tests_pass           # objective boolean gate — reads the collected tests.json
    check: |
      import json
      files = outputs.get("files", {})
      for path, content in files.items():
          if path.endswith("tests.json") and isinstance(content, str):
              d = json.loads(content)
              if d.get("passed") is True:
                  return True, "module tests passed"
              if d.get("passed") is False:
                  return False, "module tests failed"
              return False, d.get("skipped", "tests not run")
      return False, "no tests.json in outputs"

  - name: solution_quality     # LLM rubric, 1-5, vs the merged-PR oracle
    feedback_type: int
    score_range: [1, 5]
    prompt: |
      Score the agent's attempt at a models-as-a-service coding task.

      ## Task instruction
      {{ outputs.annotation_instruction_content }}
      ## Agent's changes (including output/solution.diff)
      {{ outputs.files }}
      ## Reference oracle patch (the merged PR)
      {{ outputs.annotation_oracle_content }}

      Score 1-5 (1=no meaningful attempt, 3=addresses it with gaps,
      5=comparable to the oracle). Respond with JSON:
      {"score": <int 1-5>, "rationale": "<one sentence>"}

thresholds:
  tests_pass:
    min_pass_rate: 0.5
  solution_quality:
    min_mean: 3.0
```

The composite gates on `tests_pass` (a failing fix scores `0`), and otherwise
uses the normalized `solution_quality` — exactly the metric ANOVA runs on.

Run it locally (the generic path — needs an API key and the Go toolchain for the
`tests_pass` gate):

```bash
pip install -e ".[anova,anthropic]"
export ANTHROPIC_API_KEY=sk-...
export COGNEE_MCP_URL=http://<your-cognee-mcp>/mcp   # only for context=cognee

# See the design + cost before committing:
python3 skills/eval-anova/scripts/orchestrate.py --config eval/anova-example/eval.yaml --dry-run

# Fan out, analyze, and render:
python3 skills/eval-anova/scripts/orchestrate.py --config eval/anova-example/eval.yaml
```

`solve.sh` does a fresh `git` checkout of `opendatahub-io/models-as-a-service` at
a fixed base commit, runs the agent, captures the diff to `output/solution.diff`,
and runs `go test ./...` per touched module into `output/tests.json`. Override
`MAAS_BASE_COMMIT`, `AGENT_CMD`, or `TEST_CMD` to adapt it.

## Recipe 2 — scale to a model × context grid

To ask "does cognee help *every* model, or only some?", add levels under
`matrix.factors.model` — nothing else changes. The design becomes full-factorial
and the ANOVA gains a second factor (and their interaction):

```yaml title="eval.yaml (excerpt)"
matrix:
  factors:
    model:
      - claude-opus-4-8
      - claude-sonnet-4-6
    context:
      - cognee
      - none
  replications: 3
```

That's `2 × 2 = 4` conditions; with 3 replications over 4 cases → `4 × 4 × 3 =
48` runs. Use `--dry-run` first to see the cost.

## Reproduce the analysis offline

The example ships committed `sample-runs/`, so you can exercise the analysis and
report path with **no API key and no checkout** — point `AGENT_EVAL_RUNS_DIR` at
them and run `--analyze-only`:

```bash
AGENT_EVAL_RUNS_DIR=eval/anova-example/sample-runs \
  python3 skills/eval-anova/scripts/orchestrate.py \
  --config eval/anova-example/eval.yaml --analyze-only

open eval/anova-example/sample-runs/anova-example/comparison-report/index.html
```

This recomputes `anova.json` from the recorded `summary.yaml` files and
regenerates the `/eval-compare` report — including the Statistical Significance
section — entirely offline.

!!! tip "Containerize it with Harbor"
    To run the trials in containers instead, point `runner.command` at
    `harbor run` and pass cloud/MCP settings via env or `--input-override`
    (never commit credentials). Install the Harbor extra with
    `pip install -e ".[anova,harbor]"` (Python ≥ 3.12). See
    [Running on Harbor](../guides/harbor.md).

## Related

<div class="grid cards" markdown>

- [**Analysis of variance**](../concepts/anova.md) — the statistics behind this recipe
- [**/eval-anova guide**](../guides/eval-anova.md) — every flag and the fan-out flow
- [**/eval-compare**](../guides/eval-compare.md) — the report that surfaces the stats
- [**Writing custom judges**](custom-judges.md) — the `check` and rubric judges used here

</div>
