# Failure Minibatch Analysis

You are analyzing a minibatch of **failing** test cases to identify **common patterns** — not individual quirks.

## Input

You will receive:
- The skill's SKILL.md (what the skill is supposed to do)
- Transcripts or outputs from several failing cases (the minibatch)
- Judge names and rationale for each failure
- Step context: rejected-edit buffer (what was already tried and failed) + failure patterns from previous steps
- Meta-skill context: guidance about which edit patterns have historically worked or failed for this skill

## Analysis Framework

For EACH case in the minibatch, answer:
1. Did the skill follow its own instructions? Which steps were unclear or skipped?
2. Did it take roundabout paths or try multiple approaches before succeeding/failing?
3. Did sub-skills or sub-agents behave unexpectedly?
4. Were there errors that were silently recovered but led to degraded output?

Then, across ALL cases, identify:

### Common Patterns (most valuable)
- What failure mode appears in 3+ cases? This is a systematic skill issue.
- Is there a specific SKILL.md instruction that was misinterpreted consistently?
- Is there a missing instruction that would have prevented the failures?

### Case-Specific Issues (lower value)
- Failures that appear in only 1 case — these are likely input-dependent, not skill issues.
- Mark these as "case-specific" so the optimizer can filter them out.

## Output Format

```yaml
common_patterns:
  - pattern: "Step 4 skipped because 'optionally' was interpreted as 'skip'"
    affected_cases: [case-003, case-007, case-012, case-015]
    judges: [content_quality]
    proposed_edit:
      location: "Step 4, line 'optionally add acceptance criteria'"
      change: "Remove 'optionally' — acceptance criteria are required"
      rationale: "6/8 cases skipped this step; the word 'optionally' signals it's unimportant"
    category: "removed_ambiguity"

  - pattern: "Output format missing headers"
    affected_cases: [case-003, case-012]
    judges: [format_check]
    proposed_edit:
      location: "Output section"
      change: "Add explicit format template with required headers"
      rationale: "Skill describes format in prose but never shows an example"
    category: "added_example"

case_specific:
  - case: case-007
    issue: "Input had unusual encoding that confused the parser"
    recommendation: "Not a skill issue — may need test case fix"

preserve:
  - "Step 2 (validation) works well — all cases passed validation judges"
  - "The error handling in Step 5 correctly caught and reported issues"
```

## Rules

- Focus on COMMON patterns, not individual edge cases
- Every proposed edit must include a `category` (e.g., "removed_ambiguity", "added_example", "clarified_instruction", "added_constraint", "changed_framing")
- The category is used to match against the rejected-edit buffer — if a "added_constraint" edit to the same section was already rejected, try "changed_framing" instead
- Propose edits that are general enough to help unseen cases, not just the minibatch
