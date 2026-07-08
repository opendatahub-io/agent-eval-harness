# harbor-maas-v1

SWE-bench-style ANOVA benchmark: 4 real PR tasks from [opendatahub-io/models-as-a-service](https://github.com/opendatahub-io/models-as-a-service), scored by an LLM judge against oracle patches.

## What this tests

An agent is given a PR description and asked to implement the fix in a checked-out repo. Two judges score the result:

- **has_code_changes** (gate) — did the agent produce non-trivial edits?
- **solution_quality** (1–5) — LLM judge compares the agent's diff against the oracle patch

The ANOVA matrix crosses 3 models (opus, sonnet, haiku) × 4 tasks × 1 replication to detect model-level performance differences.

## Dataset

| Task | PR | Description |
|---|---|---|
| task-0031 | RHOAIENG-63297 | ExternalModel name as model ID in GET /v1/models |
| task-0034 | (maas-api) | MaaSModelRef test coverage |
| task-0008 | RHOAIENG-52336 | Endpoint selection with multiple gateways |
| task-0010 | (maas-controller) | Provider LLMISVC test scenarios |

Each task directory contains:
- `input.yaml` — PR description (the agent's prompt)
- `instruction.txt` — same text, plain format
- `oracle.diff` — the merged PR patch (ground truth)
- `annotations.yaml` — pointers for judge context

## Prerequisites

Clone the target repo at the post-merge commit:

```bash
git clone https://github.com/opendatahub-io/models-as-a-service /tmp/maas
git -C /tmp/maas checkout a24c8c8
```

The driver reverse-applies each oracle patch at runtime to recreate the pre-PR state.

## Usage

```bash
# Set up the repo clone
export HARBOR_REPO_CLONE=/tmp/maas

# Run via the eval-anova skill
/eval-anova
```

The skill reads the `matrix:` section from `eval.yaml` and runs all condition × case cells automatically. Run outputs go to `eval/runs/` (gitignored).

## Phase 2 results (2026-06-03)

| Model | Mean quality | Notes |
|---|---|---|
| claude-opus-4-6 | 0.812 | Solved all 4 tasks |
| claude-sonnet-4-6 | 0.812 | Solved all 4 tasks |
| claude-haiku-4-5 | 0.625 | Failed task-0008 (multi-gateway) |

ANOVA: F=1.0, p=0.42, η²=0.125 — not significant at n=4, 1 rep (expected; the benchmark is a validation fixture, not a powered study).
