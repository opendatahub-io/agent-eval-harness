# Success Minibatch Analysis

You are analyzing a minibatch of **passing** test cases to identify what the skill does well and what should be preserved during optimization.

## Input

You will receive:
- The skill's SKILL.md
- Transcripts or outputs from several passing cases (the minibatch)
- Judge names and scores for each case

## Analysis Framework

For EACH passing case, identify:
1. Which SKILL.md instructions did the model follow effectively?
2. Were there any close calls — steps where the model almost went wrong but recovered?
3. What about the skill's framing or structure made it work well for this case?

Then, across ALL cases, identify:

### Working Patterns
- Which sections of the SKILL.md are consistently followed?
- Which instructions produce the highest-quality output?
- Are there patterns in how the model interprets the skill that should be reinforced?

### Fragile Successes
- Cases that passed but barely — the model struggled before arriving at the right answer
- Instructions that the model followed correctly but seemed to misunderstand initially

## Output Format

```yaml
preserve:
  - section: "Step 3 (validation)"
    reason: "All 6 cases followed this step correctly — clear, unambiguous instructions"
    strength: "high"

  - section: "Output format template"
    reason: "5/6 cases produced correctly formatted output matching the template"
    strength: "high"

fragile:
  - section: "Step 5 (error handling)"
    reason: "2 cases initially skipped error handling, then self-corrected on re-read"
    risk: "If Step 5 is modified by failure fixes, these cases might break"

reinforcement:
  - "The 'why' explanation in Step 4 is effective — cases that followed it produced better output"
  - "The example in the Output section is referenced in 4/6 transcripts — it drives correct behavior"
```

## Rules

- Your job is PRESERVATION, not correction — identify what works and flag it
- "Preserve" signals will be merged with failure-analysis edits, with failure taking priority — but your signals help prevent regressions
- Focus on the SKILL.md sections, not the individual cases — what about the instructions makes them work?
- If a section is strong, say so — this prevents the failure analyst from accidentally weakening it
