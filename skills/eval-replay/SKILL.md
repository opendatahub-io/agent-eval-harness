---
name: eval-replay
description: Generate evaluation cases from real merged pull requests for ground-truth skill testing. Fetches PR diffs, review comments, and verdicts from GitHub, creates contamination-safe repo snapshots, and produces eval.yaml with an LLM alignment judge that scores skill output against actual PR outcomes. Use when you want to test a skill against real-world outcomes, verify a code-review skill catches what humans caught, or benchmark a fix skill against accepted patches. Triggers on "replay PRs", "test against real PRs", "ground truth eval", "historical PR test", "outcome-anchored eval".
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, AskUserQuestion, Skill
---

You generate evaluation cases from historical GitHub PRs. Real merged PRs provide ground truth — review comments, accepted diffs, verdicts — that an LLM judge scores skill output against. The judge answers: "would acting on this skill's output have converged toward what was actually merged?"

## Step 0: Parse Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--repo <org/name>` | **yes** | — | GitHub repository |
| `--pr <N>` | **yes** | — | PR number (repeatable) |
| `--skill <name>` | **yes** | — | Skill under test |
| `--strategy <type>` | no | `review` | Evaluation strategy: `review`, `fix`, or `scan` |
| `--output-dir <path>` | **yes** | — | Case output directory (**outside** the project, e.g. `../eval-replay-output/cases`) |
| `--config-output <path>` | **yes** | — | Generated eval.yaml (alongside cases, e.g. `../eval-replay-output/eval.yaml`) |
| `--skip-clone` | no | false | Skip repo cloning (for testing) |

## Step 1: Validate Prerequisites

Check that `gh` CLI is authenticated:

```bash
gh auth status
```

If not authenticated, tell the user to run `gh auth login` first.

## Step 2: Generate Cases

Run `from_pr.py` for each PR. This fetches metadata, diffs, and review comments, then creates a contamination-safe shallow clone at the merge-base.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/from_pr.py \
    --repo <repo> --pr <N> [--pr <M>] \
    --strategy <strategy> \
    --output-dir <output-dir>
```

Each case directory (`<output-dir>/pr-<N>/`) contains:
- `input.yaml` — PR context visible to the skill (title, body, changed files, repo path)
- `annotations.yaml` — ground truth for judges only (verdict, review comments, expected files)
- `reference.patch` — the accepted diff
- `repo/` — shallow clone at merge-base (no post-merge objects, no remote)

Verify at least one case was created:

```bash
ls <output-dir>/pr-*/input.yaml
```

## Step 3: Generate eval.yaml

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/generate_eval.py \
    --skill <skill> \
    --strategy <strategy> \
    --dataset-path <output-dir> \
    --output <config-output>
```

This produces a complete harness-compliant config with:

- **`outcome_alignment`** (LLM judge) — the primary evaluator. Given the accepted diff, reviewer comments, and the skill's output, scores 1-5 how well the skill's work aligns with what was actually needed. Strategy-specific prompts in `prompts/`.
- **`has_output`** (deterministic) — sanity check that the skill produced non-trivial output.
- **`references_diff_files`** (deterministic) — sanity check that the skill referenced at least one file from the PR diff.

## Step 4: Run Evaluation

Hand off to `/eval-run`:

```text
Use the Skill tool to invoke /eval-run --config <config-output> --model <model>
```

## Step 5: Report

After `/eval-run` completes, present the results with context:

1. **Per-judge pass rates** — which judges passed/failed across cases
2. **Per-case breakdown** — which PRs the skill handled well vs poorly
3. **Ground truth comparison** — what reviewers found vs what the skill found

Suggest next steps:
- `/eval-review --run-id <id>` to inspect individual cases
- `/eval-optimize --model <model>` to iterate on the skill
- Add more PRs with `--pr` to expand the dataset

## Strategies

### review (default)
Tests code-review skills. Ground truth: reviewer comments, verdict, and accepted diff.
LLM judge scores whether the skill's review would have guided the author toward the accepted outcome.

### fix
Tests bug-fix skills. Ground truth: the accepted patch.
LLM judge scores whether the skill's fix addresses the same root cause in a structurally similar way.

### scan
Tests security scan skills. Ground truth: the vulnerability fix.
LLM judge scores whether the skill identified the vulnerability the accepted PR fixes.

## Rules

- **Never put ground truth in the agent workspace** — `annotations.yaml` stays in the dataset dir; only `input.yaml` reaches the skill
- **The LLM judge has ground truth** — unlike typical LLM judges that assess "quality," `outcome_alignment` compares against the actual accepted diff and reviewer comments. It answers: "would acting on this skill's output have converged toward what was actually merged?"
- **Contamination is prevented** — shallow clones have no remote, no refs, no post-merge objects
- **Don't modify the skill** — this skill generates cases and config; `/eval-optimize` handles skill changes

$ARGUMENTS
