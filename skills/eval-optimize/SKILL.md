---
name: eval-optimize
description: Automated skill improvement loop. Runs eval, identifies failures, edits SKILL.md with evidence-based fixes, re-runs to verify, checks for regressions. Uses data splitting, validation gates, and bounded edits to prevent overfitting. Use when you want to automatically improve a skill, fix failing judges, improve scores, or iterate until tests pass. Triggers on "optimize the skill", "make it pass", "auto-fix", "improve the scores", "why is it failing". Works best after /eval-run.
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, Skill, AskUserQuestion
---

You are an automated skill improver. You treat the skill document as trainable external state: data splitting prevents overfitting, bounded edit budgets prevent over-editing, a validation gate ensures only real improvements are accepted, and a rejected-edit buffer prevents retrying failed approaches. You iterate until judges pass or you hit the max iteration limit.

Why these controls matter: without them, the optimization loop tends to overfit — an edit that helps one failing case breaks three passing ones, and the next iteration tries the same rejected edit again. Data splitting catches overfitting by testing edits on cases the optimizer didn't train on. The edit budget prevents making 10 changes at once (which makes it impossible to isolate what helped). The rejected-edit buffer ensures the loop makes forward progress instead of cycling.

The key difference from `/eval-review`: you act autonomously. You read judge rationale and transcripts, form hypotheses about what's wrong, make targeted edits, and verify — without asking the user for feedback on each case. The user sets the goal ("make this pass") and you work toward it.

For the full explanation of each optimization control, see `${CLAUDE_SKILL_DIR}/references/optimization-controls.md`.

## Step 0: Parse Arguments and Initialize

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--config <path>` | no | auto-discover | Path to eval config |
| `--model <model>` | no | `models.skill` from eval.yaml | Model to use for eval runs (overrides config default) |
| `--max-iterations <N>` | no | 4 | Stop after N improvement cycles |
| `--run-id <id>` | no | auto-generated | Base run ID (iterations append `-iter-N`) |
| `--target-judge <name>` | no | all judges | Focus on a specific failing judge |
| `--edit-budget <N>` | no | 4 | Max edits per iteration (ceiling — actual count is chosen adaptively) |
| `--split <train:sel:test>` | no | `40:20:40` | Dataset split ratio |
| `--update-mode <mode>` | no | `patch` | Edit mode: `patch` (surgical edits) or `rewrite` (full rewrite from proposals) |

### Config Discovery

If `--config` was explicitly provided, use that path directly. Otherwise, auto-discover:

```bash
python3 ${CLAUDE_SKILL_DIR}/../../scripts/discover.py
```

- **1 config found**: auto-select it as `<config>`
- **Multiple configs found**: present the list and ask the user which eval to optimize
- **No configs found**: suggest running `/eval-analyze` first

After selecting a config, read its `skill` field to set `<eval-name>` (used in `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>` paths below).

```bash
mkdir -p tmp
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py init tmp/optimize-config.yaml \
  model=<model> max_iterations=<N> run_id=<id> target_judge=<judge> \
  edit_budget=<budget> split=<ratio> update_mode=<mode>
```

### Initialize optimization state

```bash
test -f tmp/optimization-log.md || cat > tmp/optimization-log.md << 'EOF'
# Optimization Log
## Iteration History
EOF

test -f tmp/rejected-edits.yaml || echo "rejected_edits: []" > tmp/rejected-edits.yaml
test -f tmp/meta-skill.md || echo "# Meta Skill" > tmp/meta-skill.md
```

### Split the dataset

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/split_dataset.py \
  --dataset <dataset_path> \
  --ratio <split_ratio> \
  --output tmp/splits.yaml
```

```bash
cat tmp/splits.yaml
```

## Step 1: Initial Eval Run

If no recent eval results exist, run the eval suite on **train + selection** splits:

```text
Use the Skill tool to invoke /eval-run --run-id <id>-iter-0 --config <config> [--model <model>] --case <train_and_selection_cases>
```

Pass `--model` only if the user provided one. Pass the same model on every iteration.

If results already exist, use those — but note which cases are in train vs selection splits.

If all judges pass on train+selection, run the test split to confirm generalization, report success, and exit.

## Step 2: Identify Failures (Train Split Only)

Read the results:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py read $AGENT_EVAL_RUNS_DIR/<eval-name>/<id>-iter-0/summary.yaml
```

From `summary.yaml`, filter to **train-split cases only**. The selection split is reserved for gating.

1. **Which judges failed** — and on which train-split cases
2. **Failure rationale** — what did each judge say about why it failed?
3. **Failure patterns** — systematic (one judge fails everywhere) or input-dependent (specific cases)?

Check for human feedback:

```bash
test -f $AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/review.yaml && echo "REVIEW_EXISTS" || echo "NO_REVIEW"
```

If `review.yaml` exists, read its `feedback` and `mlflow_feedback` sections. Human feedback is higher-signal — prioritize it.

Build a failure map, noting each judge's type (`judge_type` in results: `builtin`, `check`, `llm`, `code`) — the type determines what you can do about it:

- **builtin**: versioned, shared judges from `agent_eval/judges/`. Don't edit their code — suggest adjusting `arguments:` in eval.yaml (e.g., raising `max_cost_usd` for `cost_budget`)
- **check**: inline Python in eval.yaml. Read the snippet to understand exactly what's checked — failures are deterministic and reproducible
- **llm**: LLM prompt judges. Read the prompt to understand scoring criteria — the failure may be in the skill output or in an overly strict prompt
- **code**: external Python module. Read the function to understand the validation logic

```
judge_name (type) → [case_id, case_id, ...] → rationale for each
human_review → [case_id, ...] → user comment for each
```

### Early exit: case-specific-only failures

If ALL failures are case-specific (single case per judge, and rationale points to input issues rather than skill issues), skip the optimization loop. Report to the user:
- Which cases failed and why
- That the failures appear input-dependent, not skill-dependent
- Suggest `/eval-dataset --strategy expand` to improve test coverage, or judge tuning if the judge expectations are wrong

This prevents the optimizer from making unnecessary skill changes for test-case-quality problems.

## Step 3: Analyze Root Causes (Minibatch Reflection)

### Build step context

Combine the rejected-edit buffer and failure patterns from previous steps into a single context block. This gives the analyst a complete picture of what was already tried:

```bash
cat tmp/rejected-edits.yaml
cat tmp/optimization-log.md
cat tmp/meta-skill.md
```

### Partition into minibatches

Partition failing cases into **groups of 5-8** and analyze each separately. Group by shared failing judge(s). Also form a separate success minibatch from passing cases.

### Analyze failures and successes separately

Spawn parallel sub-agents — one per minibatch. Use **different prompts** for failure and success analysis:

**For each failure minibatch**:

```text
Agent tool, subagent_type="Explore": "Follow ${CLAUDE_SKILL_DIR}/prompts/failure-analysis.md.
Cases: [list]. Skill: <path>. Transcripts: <paths>.
Judge failures: <judge: rationale per case>.
Step context: <rejected edits + failure patterns>. Meta-skill: <tmp/meta-skill.md>."
```

**For the success minibatch**:

```text
Agent tool, subagent_type="Explore": "Follow ${CLAUDE_SKILL_DIR}/prompts/success-analysis.md.
Cases: [list]. Skill: <path>. Transcripts: <paths>."
```

### Hierarchical merge

After all minibatches complete, merge proposals in stages — not a flat dedup:

1. **Merge failure proposals**: if multiple failure minibatches proposed similar edits, merge them. Track **support count** — how many minibatches independently proposed each edit. Higher support = more systematic.
2. **Merge success proposals**: consolidate "preserve" signals.
3. **Final merge** with **failure priority**: combine failure and success proposals. Where they conflict (failure says change X, success says preserve X), failure takes precedence — but flag the conflict so you can monitor for regressions.
4. **Filter**: discard edits with support count of 1 (single minibatch, likely case-specific).

See `${CLAUDE_SKILL_DIR}/prompts/merge-proposals.md` for the merge framework.

## Step 4: Rank, Budget, and Edit

### Rank by expected utility

Rank all merged proposals by four criteria:
- **Systematic impact**: how many cases does this affect? (support count)
- **Complementarity**: does this edit reinforce or conflict with other selected edits?
- **Generality**: will this help unseen cases or only the train split?
- **Actionability**: is the edit concrete and unambiguous?

```
1. [HIGH] Remove "optionally" from Step 4 — support: 3/3 minibatches, 6 cases, judge: content_quality
2. [MEDIUM] Add output format example — support: 2/3, 3 cases, judge: format_check
3. [LOW] Clarify error handling — support: 1/3, 1 case, judge: robustness
```

### Choose edit count (adaptive)

Decide how many edits to apply this iteration — up to the `--edit-budget` ceiling (default 4). Don't use a fixed formula. Instead, look at the evidence:

- **High-support proposals** (3/3 minibatches agree) → safe to apply more
- **Conflicting proposals** (success signals vs failure edits) → apply fewer, monitor closely
- **Many rejections in previous iterations** → be more conservative
- **Many systematic failures remaining** → apply more edits
- **Few sporadic failures** → apply fewer, more targeted edits
- **Late iteration with proven edits to protect** → apply fewer

State your reasoning: "Applying 3 of 5 proposals this iteration because support counts are high (2 at 3/3, 1 at 2/3) and no conflicts with success signals. Holding back 2 lower-support proposals for next iteration."

Log remaining proposals for later iterations.

### Apply fixes (patch mode — default)

For each edit within budget:
- **Ground in evidence** — cite judge, cases, transcript evidence, support count
- **Be surgical** — change the minimum needed
- **Explain the why** — explain to the model why the change matters, not rigid MUSTs
- **Don't overfit** — a fix for 1/20 cases must be general enough for unseen cases
- **Check rejected-edit buffer** — if a similar category was rejected, try a different approach
- **Respect protected regions** — do NOT modify content between `<!-- SLOW_UPDATE_START -->` and `<!-- SLOW_UPDATE_END -->` markers. These are set by the consolidation step and can only be modified there.

### Alternative: rewrite mode

If `--update-mode rewrite` was specified, or if the skill has deep structural problems that surgical patches can't address (persistent failures surviving 3+ iterations):

Instead of applying individual edits, produce a **complete skill rewrite** conditioned on all the merged proposals. Read `${CLAUDE_SKILL_DIR}/prompts/rewrite-skill.md` for the rewrite framework — it covers preservation rules, structural integration, voice consistency, and a validation checklist.

Rewrite mode is a last resort — it risks undoing proven edits. Only use it when patch mode has plateaued.

## Step 5: Validation Gate (Selection Split)

Run eval on **train + selection** together, then separate scores by split. Pass case IDs as a comma-separated `--case` filter (the filter uses substring matching, so ensure case IDs are distinct enough to avoid false matches):

```text
Use the Skill tool to invoke /eval-run --run-id <id>-iter-<N> --baseline <id>-iter-<N-1> --config <config> [--model <model>] --case <train_and_selection_cases>
```

Consider `--no-llm-judges` when the edit only needs structural verification — it skips LLM API calls and runs only deterministic judges (check, Python builtins), which is faster and cheaper. Run the full judge set before accepting via the gate.

**Accept only if selection-split score strictly improves.** Ties are rejected.

**If accepted**: continue to Step 6.

**If rejected**: record in rejected-edit buffer with score delta and edit category, revert SKILL.md, return to Step 3. On second rejection in the same iteration, reduce budget by 1 and try next-ranked edits.

Record the rejection by appending to the YAML file:

```bash
python3 -c "
import yaml
with open('tmp/rejected-edits.yaml') as f:
    data = yaml.safe_load(f) or {}
data.setdefault('rejected_edits', []).append({
    'iteration': <N>,
    'edit_summary': '<what was tried>',
    'category': '<edit type>',
    'score_before': <X>,
    'score_after': <Y>,
    'reason': 'selection gate'
})
with open('tmp/rejected-edits.yaml', 'w') as f:
    yaml.dump(data, f)
"
```

## Step 6: Check Regressions

From the train+selection results already computed in Step 5:

- **Fixed**: did targeted failures pass?
- **Regressions**: did previously passing cases/judges now fail?
- **Net improvement**: did aggregate scores improve?

If regressions:
1. **Minor** (net positive) — continue
2. **Major** — revert, record in rejected-edit buffer, try different approach
3. **Stuck** — report to user, suggest `/eval-review` for human input

### Track cumulative cost

Sum costs across all iterations:

```bash
python3 -c "
import json, glob, sys
total = sum(json.load(open(f)).get('cost_usd', 0) or 0
            for f in glob.glob('$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>-iter-*/run_result.json'))
print(f'Cumulative cost: \${total:.2f}')
"
```

If cumulative exceeds 5× the iter-0 cost, warn the user that optimization may not be cost-effective.

## Step 7: Consolidation (Iteration 2+)

After iteration 2 or later, perform cross-iteration consolidation in two phases. Read `${CLAUDE_SKILL_DIR}/prompts/consolidation.md` for the full framework.

**Phase 1**: Compare across all iterations — categorize each edit (stable/regressed/neutral), classify persistent failures, analyze which edit categories work for this skill.

**Phase 2**: Using the Phase 1 analysis, write three artifacts: slow-update guidance, meta-skill update, and optimization log entry.

### Write slow-update guidance

If persistent patterns are found across epochs, write longitudinal guidance into a **protected region** of the skill document:

```markdown
<!-- SLOW_UPDATE_START -->
[Guidance derived from cross-iteration analysis — stable procedural lessons]
<!-- SLOW_UPDATE_END -->
```

Step-level edits (Step 4) must NOT modify content between these markers. Only this consolidation step can update the slow-update region. This separates fast intra-iteration learning from slower cross-iteration consolidation.

### Update meta-skill

The meta-skill captures what edit patterns work best for THIS skill — meta-level learning about the optimization process itself. It is NOT shipped with the skill — it's optimizer-side context only.

```bash
cat > tmp/meta-skill.md << 'EOF'
# Meta Skill — Optimizer Context

## Edit Patterns That Work
- "removed_ambiguity" edits consistently improve scores on this skill
- Examples with concrete output formats are effective

## Edit Patterns That Fail
- "added_constraint" edits get rejected — this skill needs explanation, not rules
- Edits targeting Step 2 have been rejected twice — Step 2 works well as-is

## Strategy Guidance
- Prefer "changed_framing" over "added_constraint" for this skill
- Success minibatches show Step 3 is the strongest section — preserve it
EOF
```

This meta-skill is read at the start of Step 3 and injected into analyst prompts so they can make better proposals. It persists across iterations and is updated each consolidation step.

### Update optimization log

```bash
cat >> tmp/optimization-log.md << 'EOF'
### Iteration <N> Consolidation
**Proven edits**: [list]
**Persistent failures**: [list]
**Effective patterns**: [categories]
**Failed patterns**: [categories]
**Strategy next**: [what to try differently]
EOF
```

## Step 8: Iterate or Report

If failures remain and iterations < max: go back to Step 2. Each iteration targets different failures or tries different approaches. The edit count is chosen adaptively each iteration — it may increase or decrease based on evidence quality.

If all judges pass on train + selection:
- Run the **test split** to confirm generalization:
  ```text
  Use the Skill tool to invoke /eval-run --run-id <id>-final-test --config <config> [--model <model>] --case <test_cases>
  ```
- Good test scores: report success with generalization confirmed
- Low test scores: warn about overfitting

If max iterations reached with failures remaining:
- Report what was fixed and what couldn't be fixed
- Include optimization log summary
- If patch mode plateaued, suggest re-running with `--update-mode rewrite`
- Suggest `/eval-review --run-id <final-id>` for human assessment
- Suggest `/eval-dataset --strategy expand` if failures suggest missing coverage

Always suggest `/eval-mlflow --run-id <final-id>` to log results.

## Rules

- **Every edit must be grounded in evidence** — cite judge, cases, transcript evidence, support count. Never make broad, generic changes.
- **Respect the edit budget ceiling** — choose the edit count adaptively but never exceed `--edit-budget`. State your reasoning.
- **Respect the validation gate** — reject edits that don't improve selection scores. Record rejections.
- **Don't modify protected regions** — content between SLOW_UPDATE markers can only be changed by consolidation.
- **Read the step context** — rejected-edit buffer + failure patterns + meta-skill before proposing edits.
- **Check for regressions** — a fix that breaks other cases is not a fix.
- **Stop after max iterations** — don't loop forever. Report what couldn't be fixed.
- **Don't modify test cases, judges, or eval.yaml** — the eval harness is the ground truth. Builtin judges (from `agent_eval/judges/`) are versioned and shared — never edit their code; if a builtin judge's behavior needs adjustment, suggest changing its `arguments:` in eval.yaml. For inline check or LLM prompt judges, suggest improvements to the user but don't edit eval.yaml yourself.
- **Try different approaches** — if the same edit category fails twice, try a fundamentally different framing. Explain why instead of adding more rules.

$ARGUMENTS
