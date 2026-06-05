# Hierarchical Proposal Merge

You are merging edit proposals from multiple minibatch analyses into a single ranked list. This is NOT a simple dedup — it's a structured merge that resolves conflicts, tracks support counts, and applies failure priority.

## Input

You will receive:
- Proposals from failure minibatches (each with `common_patterns`, `case_specific`, `preserve`)
- Proposals from the success minibatch (`preserve`, `fragile`, `reinforcement`)
- The rejected-edit buffer (edits to avoid re-proposing)

## Merge Process

### Stage 1: Merge failure proposals

For each pair of proposals from different failure minibatches:

1. **Identical edits** (same location + same change): merge into one, sum the `affected_cases`, increment **support count**
2. **Similar edits** (same location, different change): keep both as alternatives, note the conflict
3. **Complementary edits** (different locations, both corrective): keep both, check they don't contradict
4. **Contradictory edits** (one says add X, another says remove X): keep the one with higher support count, flag the other as conflicted

### Stage 2: Merge success proposals

Consolidate all "preserve" and "reinforcement" signals into a protection list — sections of the skill that are working well.

### Stage 3: Final merge (failure priority)

Combine failure edits and success preservation:

1. **No conflict**: failure edit targets a section not flagged by success → keep the edit
2. **Conflict**: failure edit modifies a section that success flagged as "strong" → keep the edit BUT flag it as **high-risk for regression**. The validation gate will catch regressions, but flagging helps the optimizer be cautious.
3. **Fragile sections**: if a failure edit targets a section marked "fragile" by success analysis → apply with extra care; consider testing this edit in isolation

### Stage 4: Filter

- Discard edits with support count of 1 AND fewer than 3 affected cases — likely case-specific. Exception: if there is only 1 failure minibatch (small dataset), skip this filter — all edits have support count 1 by definition.
- Discard edits whose `category` + `location` match a rejected edit in the buffer
- Discard `case_specific` entries (these are for logging, not for edits)

## Output Format

```yaml
merged_edits:
  - edit:
      location: "Step 4, line 'optionally add acceptance criteria'"
      change: "Remove 'optionally' — acceptance criteria are required"
      category: "removed_ambiguity"
    support_count: 3
    affected_cases: [case-003, case-007, case-012, case-015, case-018, case-020]
    judges: [content_quality]
    risk: "low"  # no conflict with success signals

  - edit:
      location: "Output section"
      change: "Add explicit format template with required headers"
      category: "added_example"
    support_count: 2
    affected_cases: [case-003, case-012, case-018]
    judges: [format_check]
    risk: "medium"  # conflicts with success signal on output section (fragile)

protection_list:
  - section: "Step 3 (validation)"
    strength: "high"
    reason: "All success cases follow this correctly"

conflicts:
  - location: "Step 5"
    failure_proposal: "Add explicit error format requirement"
    success_signal: "Step 5 error handling works well as-is"
    resolution: "Apply failure edit, monitor for regression"
```
