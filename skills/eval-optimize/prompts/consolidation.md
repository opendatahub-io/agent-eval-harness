# Cross-Iteration Consolidation

You are performing a longitudinal review after iteration N of skill optimization. This is a two-phase process — do it in two separate analyses to keep each focused.

## Phase 1: Cross-Iteration Comparison

Compare results across ALL iterations to identify patterns.

### Input

- Summary results from every iteration (iter-0 through iter-N)
- The optimization log (what was tried, what worked, what failed)
- The rejected-edit buffer

### Categorize each edit

For every edit applied across all iterations, classify it:

1. **Stable improvement**: improved scores when introduced AND held across all subsequent iterations. These are proven.
2. **Regression**: initially helped but scores degraded in later iterations (a later edit may have conflicted).
3. **Neutral**: no measurable effect on scores. May be dead weight — consider removing.

### Categorize persistent failures

Failures surviving 2+ iterations of attempted fixes:
- **Structural**: the skill's architecture can't address this — needs rewrite mode
- **Input-dependent**: specific case types the skill isn't designed for — needs dataset expansion
- **Judge mismatch**: the judge may be testing something unreasonable — flag for user review

### Analyze edit patterns

Which edit categories have been effective for THIS skill?
- Track: "removed_ambiguity", "added_example", "clarified_instruction", "added_constraint", "changed_framing", "bundled_script"
- Note which categories were accepted vs rejected, and for which skill sections

### Output (Phase 1)

```yaml
comparison:
  iteration: <N>
  stable_improvements:
    - edit: "description"
      introduced: iter-1
      judges_improved: [list]
  regressions:
    - edit: "description"
      introduced: iter-2
      regressed_in: iter-3
  persistent_failures:
    - judge: name
      type: structural | input-dependent | judge-mismatch
      attempts: N
  edit_patterns:
    effective: [categories]
    ineffective: [categories]
    untried: [categories]
```

## Phase 2: Write Consolidation Artifacts

Using the Phase 1 analysis, produce three artifacts:

### Slow-update guidance (goes into the skill)

Write concise longitudinal guidance (5-15 lines) capturing stable procedural lessons. This is injected into the skill document between `<!-- SLOW_UPDATE_START/END -->` markers and is immune to step-level edits.

Focus on:
- Stable patterns that should be preserved across future iterations
- Warnings about fragile sections (sections where edits have regressed)
- Procedural insights that are more durable than specific rules

### Meta-skill update (optimizer-side only, NOT shipped)

Write updated optimizer-side guidance about what works for this skill:
- Which edit categories to prefer and avoid
- Which skill sections are resilient vs fragile
- Strategic recommendations for the next iteration

### Optimization log entry

Record the consolidation findings for the iteration history:

```
### Iteration <N> Consolidation
**Proven edits**: [list]
**Persistent failures**: [list with type classification]
**Effective patterns**: [categories]
**Failed patterns**: [categories]
**Strategy next**: [what to try differently]
```
