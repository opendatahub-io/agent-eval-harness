# anova-example — a worked `/eval-anova` example

A real Design-of-Experiments example over **4 bugfix PRs from
[opendatahub-io/models-as-a-service](https://github.com/opendatahub-io/models-as-a-service)**.
It runs a **context A/B** — one model, with vs without a
[cognee](https://github.com/topoteretes/cognee) knowledge-graph MCP — then runs
repeated-measures ANOVA over the results. (Add more entries to the `model`
factor to turn it into a model × context grid.)

Each cell is scored by two judges, mirroring the original benchmark:

- **`tests_pass`** — an objective boolean gate: do the touched Go module's tests
  pass on the agent's fix? (`solve.sh` runs them and writes `tests.json`.)
- **`solution_quality`** — an LLM rubric (1–5) comparing the diff to the oracle.

The composite gates on `tests_pass` (a failing fix scores 0) and otherwise uses
the normalized `solution_quality` — so a change must both pass tests and be
judged sound.

It stays entirely on the **generic path**: a `cli` runner does a local `git`
checkout (no Harbor, no OpenShift, no committed credentials), and the `context`
factor reaches the command through the orchestrator's `--input-override` (#17).

## Files

- `eval.yaml` — the cli-runner eval + a `matrix:` (model × context) + both judges.
- `solve.sh` — per cell: checks out models-as-a-service at the base commit, runs
  the agent (with the cognee MCP when `context=cognee`), captures
  `output/solution.diff`, and runs the touched module's tests into `output/tests.json`.
- `mcp-cognee.json` / `mcp-none.json` — the two `context` levels. Point
  `mcp-cognee.json` at your own cognee MCP endpoint (`COGNEE_MCP_URL`).
- `dataset/task-*/` — the four tasks: `input.yaml` (PR prompt), `instruction.txt`,
  and `oracle.diff` (the merged PR the judge scores against).
- `sample-runs/` — committed results so the analysis + report reproduce offline.

## Run it (generic path — cli + local checkout)

```bash
pip install -e ".[anova,anthropic]"
export ANTHROPIC_API_KEY=sk-...            # or the Vertex env (see QUICKSTART)
export COGNEE_MCP_URL=http://<your-cognee-mcp>/mcp   # only needed for context=cognee

python3 skills/eval-anova/scripts/orchestrate.py --config eval/anova-example/eval.yaml --dry-run
python3 skills/eval-anova/scripts/orchestrate.py --config eval/anova-example/eval.yaml
```

Each cell runs as a **standard eval-run** under
`$AGENT_EVAL_RUNS_DIR/anova-example/<run-id>/` with a `condition.json`; the
orchestrator then writes `anova.json` and renders the `/eval-compare` report
(including the ANOVA/Pareto statistics section). `solve.sh` needs the Go
toolchain for the `tests_pass` gate (override the test step with `TEST_CMD`).

## Reproduce the analysis offline (no API key, no checkout)

```bash
AGENT_EVAL_RUNS_DIR=eval/anova-example/sample-runs \
  python3 skills/eval-anova/scripts/orchestrate.py \
  --config eval/anova-example/eval.yaml --analyze-only
open eval/anova-example/sample-runs/anova-example/comparison-report/index.html
```

## Running on Harbor (containerized)

The generic cli path above runs the checkout + tests on your machine. For
isolated, parallel, or at-scale execution, run the same tasks in **Harbor**
containers instead — the harness has built-in Harbor support that packages this
eval.yaml + dataset into Harbor trials:

```bash
pip install -e ".[anova,harbor]"     # Python >= 3.12
PLUGIN_ROOT="$(pwd)"
PYTHONPATH="$PLUGIN_ROOT" python3 -m agent_eval.harbor.run \
  --config eval/anova-example/eval.yaml --model claude-sonnet-4-6 \
  --output "$AGENT_EVAL_RUNS_DIR/anova-example/2026-07-30-sonnet-context-cognee"
```

Or point the eval's `runner.command` at `harbor run` directly (this is what the
original benchmark did). Pass any cloud/MCP settings via env or `--input-override`
rather than committing them — e.g.:

```yaml
runner:
  type: cli
  command: >-
    harbor run --path {workspace} --agent claude-code --model {model}
    -e openshift --mcp-config {config_dir}/mcp-{context}.json
    --jobs-dir {output_dir} --yes --delete
    --ae CLAUDE_CODE_USE_VERTEX=1 --ae CLOUD_ML_REGION=global
    --ae ANTHROPIC_VERTEX_PROJECT_ID=${VERTEX_PROJECT_ID}
```

Either way the matrix, `--input-override` factor delivery, `analyze_runs`, and
the eval-compare report work identically over the standard runs produced.

## How the matrix reaches the run

- `model` maps to the runner's `--model`.
- `context` is a **non-model factor**. The orchestrator passes it via
  `execute.py --input-override context=<level>`, which merges it into the case's
  `input.yaml`, so `{context}` resolves in the cli command and selects
  `{config_dir}/mcp-{context}.json`. `{config_dir}` is the eval's directory, so
  the command finds `solve.sh` and the MCP configs regardless of cwd.

## Notes

- The live run needs network + the models-as-a-service repo + the Go toolchain +
  an API key, so it is not exercised in CI; the tests cover config/matrix
  validity, `--dry-run`, `solve.sh`'s checkout/diff/tests capture (stub agent +
  `TEST_CMD`, no network), and the offline `--analyze-only` + report path.
- `MAAS_REPO_URL` / `MAAS_BASE_COMMIT` override the repo and base commit;
  `TEST_CMD` overrides the test step.
