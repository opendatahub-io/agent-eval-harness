# Skill Rewrite from Proposals

You are rewriting a complete SKILL.md based on accumulated edit proposals from multiple optimization iterations. This is a **last resort** — used when surgical patch edits have plateaued and the skill needs structural changes.

## Input

You will receive:
- The current SKILL.md (with all accumulated patches)
- Ranked edit proposals from the merge step (with support counts)
- Success-preservation signals (what works and must be kept)
- The optimization log (what was tried across iterations)
- The meta-skill (which edit patterns work for this skill)

## Rewrite Principles

### 1. Preserve what works

Read the success-analysis signals carefully. Sections flagged as "high strength" must survive the rewrite intact — reword if needed for flow, but don't change the substance. If a section is marked `<!-- SLOW_UPDATE_START -->`, copy it verbatim.

### 2. Integrate proposals structurally

Don't just apply the proposals as patches to the old text. Rethink the skill's organization to naturally accommodate the needed changes. For example, if multiple proposals say "add output format guidance," don't add it as an afterthought — integrate it into the relevant workflow step.

### 3. Explain the why

The most common reason patch edits plateau is that they add rules ("MUST do X") without explaining why. The rewrite is an opportunity to replace accumulated rules with explanations that help the model understand the intent.

### 4. Keep it lean

A rewrite should be shorter than or equal to the original — not longer. The current skill may have accumulated redundant patches. The rewrite consolidates them.

### 5. Maintain the voice

The rewrite should feel like the same skill, not a different one. Preserve the overall structure (step numbering, argument format, tool references), the terminology, and the tone.

## Output

Write the complete new SKILL.md. Include:
- The same YAML frontmatter (name, description, allowed-tools)
- All steps, renumbered if needed
- All script/prompt/reference file references (paths must stay valid)
- The slow-update protected region (copied verbatim)
- The $ARGUMENTS placeholder at the end

## Validation checklist

Before finalizing, verify:
- [ ] Every script reference path is still valid
- [ ] Every prompt file reference is still valid
- [ ] The slow-update region is preserved verbatim
- [ ] Success-preservation sections are intact
- [ ] The skill is shorter than or equal to the original
- [ ] All proposals with support count > 1 are addressed
