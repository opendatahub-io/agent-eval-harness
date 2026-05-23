# Brainstorm: Builtin Field and LLM Judge Support

**Date:** 2026-05-19
**Status:** active
**Origin:** Antonin's review comments on PR #66 (spec for 004-reusable-judges-library)

## Problem Framing

The initial spec for the reusable judges library (004) used `type: builtin` as the discriminator for built-in judges and assumed all builtins are Python functions. Antonin's review raised two interconnected design concerns:

1. **`type` field breaks existing patterns.** All other judge types are inferred from which field is set (`check`, `prompt`, `module`). Adding an explicit `type` field is inconsistent. A `builtin` field as the discriminator follows the existing inference pattern and keeps `name` user-defined.

2. **Python-only builtins are limiting.** Some evaluation patterns (content quality, relevance, completeness) are better expressed as LLM prompts than Python functions. The library should support both.

Additionally, the spec used `condition` in YAML examples where the actual field name is `if` (mapped internally to `condition` in the dataclass).

## Approaches Considered

### A: `builtin` field with dual-type registry (chosen)

Replace `type: builtin` with a `builtin` field. The registry auto-detects judge type from file extension (`.py` = code judge, `.md` = LLM prompt judge).

```yaml
judges:
  - name: safety_check
    builtin: no_harmful_content          # Python judge

  - name: quality_check
    builtin: output_completeness         # LLM prompt judge
    model: claude-sonnet-4-6
    config:
      strictness: high
```

- Pros: Follows existing field-based inference, `name` stays user-defined, clean vendoring path, LLM and Python builtins share the same resolution mechanism
- Cons: Breaking change from current spec draft, registry has two resolution paths

### B: Keep `type: builtin` and extend for LLM support

Keep the current design but add LLM support to the registry internally.

- Pros: Smaller spec delta
- Cons: `name` does double duty (identity + resolution), `type` field is inconsistent with existing patterns, can't rename builtins for thresholds/reports

## Decision

**Approach A: `builtin` field with dual-type registry.** Since no implementation code exists yet, the spec revision has zero migration cost. The design is cleaner and directly addresses the reviewer's concerns.

## Key Requirements

### YAML Schema Changes

- **New field:** `builtin: <judge_name>` on `JudgeConfig` (replaces `type: builtin` + name-as-key)
- **Remove:** `type` field entirely
- **Fix:** All YAML examples use `if:` instead of `condition:` (matches actual parsing at `config.py:351`)
- **Existing fields preserved:** `name`, `description`, `if`, `config`, `model`, `check`, `prompt`, `prompt_file`, `module`, `function`, `context`, `feedback_type`

### Type Determination Logic (updated)

1. If `builtin` is set: resolve via builtin registry. All other type-discriminating fields (`check`, `prompt`, `prompt_file`, `module`, `function`) MUST be empty.
2. If `check` is set: inline check
3. If `prompt` or `prompt_file` is set: LLM judge
4. If `module` and `function` are set: external code judge
5. Otherwise: skip with warning

### Dual-Type Registry

- `.py` files: Python function judges, `(outputs, config) -> (bool, str)` contract (unchanged)
- `.md` files: LLM prompt judges, Jinja-style templates with `{{ config.key }}` and `{{ outputs }}` variables
- Auto-detection: registry scans subdirectories, determines type from file extension
- Name derived from filename without extension (same as before)

### LLM Prompt Template Contract

- Jinja2 templating with `config` and `outputs` as template variables
- `model` field on `JudgeConfig` works for builtin LLM judges (overrides `models.judge`)
- Template rendering happens before LLM call
- LLM output parsed into `(bool, str)` (exact parsing mechanism to be specified in spec phase)

### Initial Judge Set

| Name | Category | Type | Description |
|------|----------|------|-------------|
| no_harmful_content | safety | Python | Scans conversation and files for harmful content |
| tool_call_validation | process | Python | Verifies tool calls complete successfully |
| cost_budget | efficiency | Python | Checks cost_usd against configurable threshold |
| output_completeness | quality | LLM | Evaluates output completeness via LLM prompt |

### Directory Structure

Keep subdirectories for browsability. Users reference judges by flat name. Category derived from parent directory.

```
agent_eval/judges/
  safety/
    no_harmful_content.py
  process/
    tool_call_validation.py
  efficiency/
    cost_budget.py
  quality/
    output_completeness.md
```

### Vendoring Path

Drop `builtin`, add the appropriate field for the judge type:
- Python builtin -> copy `.py`, use `module`/`function`
- LLM builtin -> copy `.md`, use `prompt_file`

## Open Questions

- How should LLM judge output be parsed into `(bool, str)`? Options: structured output, JSON block in response, regex extraction
- Should builtin LLM judges support the `context` field for supplementary files?
- What Jinja filters/functions should be available beyond defaults? (`tojson` is essential)
- Should the prompt template include a standard preamble (output format instructions) or leave that to each template?
