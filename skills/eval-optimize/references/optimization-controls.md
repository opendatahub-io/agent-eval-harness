# Optimization Controls Reference

Controls that prevent overfitting, over-editing, or retrying failed approaches.

## 1. Data Splitting

| Split | Default % | Purpose | When used |
|-------|-----------|---------|-----------|
| Train | 40% | Generate edit candidates | Steps 2-4 |
| Selection | 20% | Validation gate | Step 5 |
| Test | 40% | Final measurement only | Step 8 |

Splitting is deterministic (seed 42). Use `scripts/split_dataset.py`. Tiny datasets (< 3 cases): all cases used in all splits. Small datasets (3-9 cases): train and selection overlap.

## 2. Edit Budget (Adaptive)

The `--edit-budget` argument (default 4) is a **ceiling**, not a fixed count. The LLM chooses how many edits to apply each iteration based on evidence:

| Signal | More edits | Fewer edits |
|--------|-----------|-------------|
| Support counts | High (3/3 minibatches) | Low (1/3) |
| Conflict with success signals | No conflicts | Conflicts flagged |
| Previous rejections | Few or none | Many in recent iterations |
| Failure pattern | Systematic (many cases) | Sporadic (1-2 cases) |
| Proven edits to protect | Few | Many |

The LLM must state its reasoning when choosing the count: which signals it considered and why it chose N edits. This makes the decision auditable and helps future iterations calibrate.

Minimum: 1 edit per iteration (otherwise the iteration is wasted). Maximum: `--edit-budget` ceiling.

## 3. Validation Gate

Selection-split score must **strictly improve**. Ties rejected. On rejection: record edit + delta in `tmp/rejected-edits.yaml`, revert, return to Step 3. Second rejection reduces budget by 1.

## 4. Rejected-Edit Buffer

File: `tmp/rejected-edits.yaml`. Records rejected edits with `category`, `score_before`, `score_after`, `reason`. The optimizer reads this before proposing edits — if the same `category` + `location` was rejected, try a different category.

## 5. Minibatch Reflection

Groups of 5-8 cases analyzed in parallel. **Failure and success analyzed separately** with different prompts:

- `prompts/failure-analysis.md` — identifies failure patterns, proposes corrective edits with categories
- `prompts/success-analysis.md` — identifies working patterns, flags fragile sections

Why separate? Failure analysis needs hypothesis formation ("why did this break?"). Success analysis needs preservation reasoning ("why does this work?"). Combining them produces muddled output.

## 6. Hierarchical Merge

After minibatch analysis, proposals are merged in stages — not flat dedup:

1. **Merge failure proposals** — track support count (how many minibatches independently proposed each edit)
2. **Merge success proposals** — consolidate preservation signals
3. **Final merge with failure priority** — failure edits take precedence over preservation, but conflicts are flagged
4. **Filter** — discard support-1 edits and edits matching rejected-edit buffer

See `prompts/merge-proposals.md` for the full framework.

## 7. Protected Slow-Update Region

The skill document can contain a protected region:

```markdown
<!-- SLOW_UPDATE_START -->
[Longitudinal guidance from cross-iteration consolidation]
<!-- SLOW_UPDATE_END -->
```

**Step-level edits (Step 4) must NOT modify content between these markers.** Only the consolidation step (Step 7) can update this region. This separates fast intra-iteration learning from slower cross-iteration consolidation.

The slow-update captures stable procedural lessons that have proven effective across multiple iterations — the "long-term memory" of the optimization process.

## 8. Meta-Skill (Optimizer-Side Learning)

File: `tmp/meta-skill.md`. Captures what edit patterns work best for THIS specific skill — meta-level learning about the optimization process itself.

**Key difference from the optimization log**: the log records events (what happened). The meta-skill extracts strategies (what to do next time). The meta-skill is injected into analyst prompts so they can make better proposals.

**Not shipped with the skill** — it's optimizer-side context only. Keeps the deployed skill compact.

Updated at each consolidation step (Step 7).

## 9. Update Modes

| Mode | When to use | How it works |
|------|-------------|--------------|
| **patch** (default) | Most iterations | Surgical edits: append, insert, replace, delete. Respects protected regions. |
| **rewrite** | Persistent failures after 3+ iterations | Full skill rewrite conditioned on accumulated proposals. Risks undoing proven edits — use as last resort. |

Controlled by `--update-mode`. If patch mode plateaus (persistent failures not improving), suggest switching to rewrite for the next iteration.

## 10. Cost Tracking

After each iteration, report cumulative cost from `run_result.json`. If cumulative exceeds 5× single-run cost, warn the user.

```
Iteration 1: $2.30 (cumulative: $2.30)
Iteration 2: $2.15 (cumulative: $4.45)
Iteration 3: $1.90 (cumulative: $6.35) ⚠️ 2.8× single-run cost
```

## 11. Edit Ranking Criteria

When ranking edits for clipping to the budget, score each on:

1. **Systematic impact** — support count from minibatches + number of affected cases
2. **Complementarity** — does this edit reinforce or conflict with other selected edits?
3. **Generality** — will this help unseen cases? (edits affecting diverse case types rank higher)
4. **Actionability** — is the edit concrete, unambiguous, and testable?
