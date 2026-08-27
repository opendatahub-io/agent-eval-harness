You are analyzing evaluation results combined with human feedback to identify actionable skill improvements.

## Input

You will be given:
1. Per-case judge scores (pass/fail with rationale)
2. Per-case human feedback (qualitative comments from the user)
3. Transcript analysis (if available — process issues, tool usage patterns)
4. The skill's SKILL.md content

## Analysis Framework

### 1. Judge-Human Alignment

For each case with human feedback:
- Did judges flag the same issues? (alignment — judges are working)
- Did the user flag issues judges missed? (gap — need new judges or better prompts)
- Did judges fail but the user said it's fine? (false positive — judge may be too strict)

On any judge-human disagreement, triage in this order — most disagreements
are not judge bugs:

1. **Underspecified skill prompt/spec** — is the skill's prompt (or spec)
   silent about the thing being disputed? If the expectation was never
   written down, fix the prompt first and keep the judge only as a
   regression guard for the now-explicit rule. Rewriting the judge to
   encode an unwritten expectation hides the real gap.
2. **Bad case** — is the test case ambiguous, self-contradictory, or testing
   something the skill was never asked to do? Fix or drop the case.
3. **Miscalibrated judge** — only after prompt and case are sound: tighten
   the judge's PASS/FAIL definitions and add the disputed case as a labeled
   borderline example in the judge prompt.

Downgrade a judge's model for cost only after alignment is confirmed on the
current model — a cheaper judge that was never aligned just disagrees more
quietly.

### 2. Pattern Detection

Across all feedback:
- **Systematic issues**: Same complaint across multiple cases → skill-level fix needed
- **Edge cases**: One-off issues → may not warrant a skill change
- **Process issues**: Transcript shows the skill working inefficiently even when output is OK → instructions need clarifying

### 3. Improvement Suggestions

For each pattern, propose:
- **What to change**: Specific lines or sections in SKILL.md
- **Why**: Which cases and feedback support this change
- **Risk**: Could this change cause regressions on other cases?
- **New judges**: If the user consistently flagged something, should a judge check for it?

## Output Format

Present as a structured report:

1. **Summary**: N cases reviewed, M had feedback, K patterns identified
2. **Patterns**: Each pattern with supporting evidence (case IDs + quotes)
3. **Proposed changes**: Ranked by impact, each with before/after and reasoning
4. **Judge gaps**: Things the user caught that no judge checks for
