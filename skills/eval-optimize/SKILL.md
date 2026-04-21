---
name: eval-optimize
description: Automated skill improvement loop. Runs eval, identifies judge failures, reads traces and rationale, edits the SKILL.md to fix issues, re-runs to verify, and checks for regressions. Use when the user wants to automatically improve a skill based on eval results, fix failing judges, make the skill better, auto-fix quality issues, improve scores, or iterate until all judges pass. Triggers on "optimize the skill", "make it pass", "auto-fix", "improve the scores", "why is it failing". Works best after /eval-run has produced results to learn from.
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, Skill, AskUserQuestion
---

You are an automated skill improver. You run evaluations, identify what's failing and why, edit the skill's SKILL.md to fix the issues, re-run to verify, and check for regressions. You iterate until judges pass or you hit the max iteration limit.

The key difference from `/eval-review`: you act autonomously. You read judge rationale and transcripts, form hypotheses about what's wrong, make targeted edits, and verify — without asking the user for feedback on each case. The user sets the goal ("make this pass") and you work toward it.

## Step 0: Parse Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--config <path>` | no | `eval.yaml` | Path to eval config |
| `--model <model>` | no | `models.skill` from eval.yaml | Model to use for eval runs (overrides config default) |
| `--max-iterations <N>` | no | 3 | Stop after N improvement cycles |
| `--run-id <id>` | no | auto-generated | Base run ID (iterations append `-iter-N`) |
| `--target-judge <name>` | no | all judges | Focus on a specific failing judge |

```bash
mkdir -p tmp
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py init tmp/optimize-config.yaml \
  model=<model> max_iterations=<N> run_id=<id> target_judge=<judge>
```

## Step 1: Initial Eval Run

If no recent eval results exist, run the eval suite first:

```text
Use the Skill tool to invoke /eval-run --run-id <id>-iter-0 --config <config> [--model <model>]
```

Pass `--model` only if the user provided one — otherwise let `/eval-run` fall back to `models.skill` from eval.yaml. Pass the same model on every iteration for comparable results.

If results already exist (the user just ran `/eval-run`), skip this and use the existing run.

Read the results:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py read $AGENT_EVAL_RUNS_DIR/<id>-iter-0/summary.yaml
```

If all judges pass, report success and exit — nothing to improve.

## Step 2: Identify Failures

From `summary.yaml`, identify:

1. **Which judges failed** — and on which cases
2. **Failure rationale** — what did each judge say about why it failed?
3. **Failure patterns** — does one judge fail everywhere (systematic) or only on specific cases (input-dependent)?

Also check for human feedback — these catch things judges miss:

```bash
test -f $AGENT_EVAL_RUNS_DIR/<id>/review.yaml && echo "REVIEW_EXISTS" || echo "NO_REVIEW"
```

If `review.yaml` exists, read its `feedback` section (human feedback from `/eval-review`) and `mlflow_feedback` section (annotations pulled from MLflow UI). Human feedback is higher-signal than judge rationale — prioritize issues the user flagged.

If `--target-judge` was specified, focus only on that judge's failures.

Build a failure map:
```
judge_name → [case_id, case_id, ...] → rationale for each
human_review → [case_id, ...] → user comment for each
```

## Step 3: Analyze Root Causes

For each failure pattern, investigate why the skill produces bad output:

1. **Read the skill's SKILL.md** — locate it via eval.yaml's `skill` field:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/../eval-analyze/scripts/find_skills.py --name <skill>
   ```

2. **Read transcripts** (if available) — transcripts can be very large, so delegate to an Agent. Check `run_result.json` for `execution_mode`: in `case` mode, each case has its own transcript at `$AGENT_EVAL_RUNS_DIR/<id>/cases/<case>/stdout.log`; in `batch` mode, there's one at `$AGENT_EVAL_RUNS_DIR/<id>/stdout.log`. Focus on the failing cases.
   ```text
   Agent tool, subagent_type="Explore": "Read the transcript at <path> and report:
   - Did the skill follow its own instructions? Which were unclear?
   - Did it take roundabout paths or try multiple approaches?
   - Did sub-skills behave unexpectedly?
   - Were there errors that were silently recovered?"
   ```

3. **Read failing case outputs** — use an Explore agent to examine the actual output files for failing cases. Don't read them all into your context — delegate:
   ```text
   Agent tool, subagent_type="Explore": "Read the outputs in $AGENT_EVAL_RUNS_DIR/<id>/cases/<failing_case>/
   and compare against what the judges expected. What went wrong?"
   ```

4. **Form hypotheses** — connect the judge rationale + transcript evidence + output examination to specific parts of the SKILL.md. Be specific: "The judge says the output is missing acceptance criteria. The transcript shows the skill skipped Step 4 of the pipeline. Step 4 in SKILL.md says 'optionally add acceptance criteria' — the word 'optionally' is the problem."

## Step 4: Edit the Skill

Apply targeted fixes to the SKILL.md. For each edit:

- **Ground it in evidence** — cite the judge name, failing cases, and transcript evidence
- **Be surgical** — change the minimum needed. Don't rewrite sections that are working.
- **Explain the why** — use the Skill Creator's principle: explain to the model why the change matters, rather than adding rigid MUSTs
- **Don't overfit** — if only 1 of 20 cases fails, the fix should be general enough to help without breaking the other 19

Show each edit before applying. If the change is risky (could affect passing cases), note it.

**Execution mode context**: check `execution.mode` in eval.yaml. In `case` mode, each case runs in its own isolated workspace with all case files copied in — the skill receives case-specific arguments resolved from input.yaml. In `batch` mode, all cases are in one workspace via batch.yaml. Your edits must work for the configured mode.

## Step 5: Re-Run and Verify

Run eval again with the baseline flag to detect regressions:

```text
Use the Skill tool to invoke /eval-run --run-id <id>-iter-<N> --baseline <id>-iter-<N-1> --config <config> [--model <model>]
```

Read the new results:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py read $AGENT_EVAL_RUNS_DIR/<id>-iter-<N>/summary.yaml
```

Check:
- **Fixed**: did the targeted failures pass now?
- **Regressions**: did any previously passing cases/judges now fail?
- **Score improvement**: did aggregate scores improve?

## Step 6: Handle Regressions

If the fix caused regressions (previously passing cases now fail):

1. **Assess severity** — is the regression worse than the original failure?
2. **If minor** — the fix is still a net positive, continue
3. **If major** — revert the edit and try a different approach. The skill's instructions may need a different framing rather than a different rule.
4. **If stuck** — report to the user what you tried and why it didn't work. Suggest `/eval-review --run-id <id>` for human input on the tricky cases.

## Step 7: Iterate or Report

If failures remain and iterations < max:
- Go back to Step 2 with the new results
- Each iteration should target different failures or try different approaches for persistent ones

If all judges pass:
- Report success: which edits fixed which failures, how many iterations it took
- Show the final summary.yaml scores

If max iterations reached with failures remaining:
- Report what was fixed and what couldn't be fixed
- For persistent failures, explain what you tried and why it didn't work
- Suggest `/eval-review --run-id <final-id>` for human assessment of the remaining issues
- Suggest `/eval-dataset --strategy expand` if failures suggest missing test coverage

In all cases (include `--config <config>` if a non-default config was used):
- Suggest `/eval-mlflow --run-id <final-id>` to log the optimization results to MLflow for tracking.

## Rules

- **Never make broad, generic changes** — every edit must be grounded in a specific failure with evidence from judges and transcripts
- **Check for regressions after every edit** — a fix that breaks other cases is not a fix
- **Stop after max iterations** — don't loop forever. Report what couldn't be fixed.
- **Don't modify test cases or judges** — the eval harness is the ground truth. If you think a judge is wrong, report it but don't change it.
- **Don't modify eval.yaml** — your job is to improve the skill, not the evaluation config. If judges need updating, suggest it to the user.
- **Try different approaches** — if the same type of edit fails twice, try a fundamentally different framing. Explain why instead of adding more rules.

$ARGUMENTS
