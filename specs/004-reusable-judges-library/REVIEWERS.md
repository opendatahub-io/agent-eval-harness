# Review Guide: Reusable Judges Library

**Generated**: 2026-05-17 (revised 2026-05-19) | **Spec**: [spec.md](spec.md)

## Why This Change

Skill authors evaluating their Claude Code skills currently write every judge from scratch, even for common patterns like safety checks, cost budgets, or tool call validation. There are no reusable, skill-agnostic judges that ship with the harness. This means every project independently re-invents the same guardrail checks, leading to inconsistent implementations and wasted setup time.

## What Changes

The harness gains a built-in judges library: an `agent_eval/judges/` package with categorized judge files (Python functions and LLM prompt templates) that skill authors reference via a `builtin` field in eval.yaml. The `name` field stays user-defined for thresholds and reports. Authors add two lines of config (name and builtin) to get a working judge. The scoring pipeline and report rendering are extended to support the new judge type alongside existing inline check, LLM, and external code judges. No breaking changes to existing eval.yaml configurations.

## How It Works

A new `agent_eval/judges/` Python package contains category subdirectories (`safety/`, `process/`, `efficiency/`, `quality/`), with standalone judge files. A `BuiltinJudgeRegistry` class in the package `__init__.py` auto-discovers judges by scanning subdirectories at scoring time, auto-detecting type from file extension: `.py` files become Python judge entries, `.md` files become LLM prompt judge entries. The registry builds a flat `{name: entry}` map. The existing `JudgeConfig` dataclass gets two new fields: `builtin` (resolves to a registered judge name) and `config` (optional dict). The `load_judges()` function in `score.py` gains a new routing branch for `builtin` that resolves the judge via the registry and creates the appropriate scorer (direct callable for Python, template-render-then-LLM for prompts).

Four initial judges ship: `no_harmful_content` (Python, scans output for harmful content), `tool_call_validation` (Python, checks tool calls completed without errors), `cost_budget` (Python, verifies cost against configurable threshold), and `output_completeness` (LLM, evaluates output completeness via Jinja2 prompt template).

## When It Applies

**Applies when**:
- Configuring judges in eval.yaml for any skill evaluation
- Wanting common guardrail checks without writing custom code or prompts
- Needing to customize a built-in judge's behavior (vendor and modify the file)

**Does not apply when**:
- Using judge presets or curated bundles (out of scope, future feature)
- Comparing event patterns between runs for regression fingerprinting (out of scope)

## Key Decisions

1. **`builtin` field over `type: builtin`**: The `builtin` field follows the existing field-based type inference pattern (like `check`, `prompt`, `module`). This keeps `name` user-defined for thresholds and reports, and avoids introducing an explicit type discriminator that's inconsistent with existing judge types. Decided based on reviewer feedback (PR #66, @astefanutti).

2. **Dual-type registry (Python + LLM)**: The registry supports both `.py` (Python function) and `.md` (LLM prompt template) judge files, auto-detected by extension. This allows the library to ship judges using whichever approach best fits the evaluation pattern.

3. **Flat name resolution over qualified paths**: Judges are referenced by simple name (e.g., `builtin: no_harmful_content`) rather than category-qualified paths. The registry auto-discovers across all category directories and detects name collisions at startup.

4. **Jinja2 for LLM prompt templates**: LLM judge files use Jinja2 templating with `config` and `outputs` as template variables. Provides conditionals, loops, and filters (especially `tojson`) for flexible prompt construction.

5. **Optional config dict over environment variables**: Built-in judges accept an optional `config` parameter from eval.yaml rather than using environment variables. For Python judges, passed as the second function argument. For LLM judges, available as a Jinja template variable.

6. **Lazy registry instantiation**: The registry is only created on first encounter of a `builtin` field, avoiding filesystem overhead for configs that don't use builtins.

7. **Documentation-only versioning**: Each judge file defines a `__version__` string. No runtime pinning mechanism. Authors who need old behavior can vendor the judge file.

## Areas Needing Attention

- The `no_harmful_content` judge uses pattern-based detection without an LLM. Its effectiveness depends on the quality of the pattern list. The `output_completeness` LLM judge serves as the LLM-based evaluation pattern.
- The `config` parameter changes the judge function signature from `(outputs)` to `(outputs, config=None)`. The implementation must only pass `config` for builtin judges, not existing external code judges.
- The 4-tuple change to `load_judges()` return format touches the `score_cases()` function and potentially other callers. All call sites must be updated.
- Jinja2 becomes a new dependency. Verify it's acceptable for the project's dependency policy.
- LLM judge response parsing (extracting JSON from LLM output) needs robust error handling for malformed responses.

## Open Questions

No open questions identified. All ambiguities were resolved during clarification sessions (see spec.md Clarifications section).

## Review Checklist

- [ ] Key decisions are justified
- [ ] Breaking changes are documented with migration guidance
- [ ] Scope matches the stated boundaries
- [ ] Success criteria are achievable
- [ ] No unstated assumptions
- [ ] Python judge function signature is consistent across all three Python judge modules
- [ ] LLM judge prompt template follows the documented contract
- [ ] `BuiltinJudgeRegistry` handles edge cases (empty directories, non-Python/non-Markdown files, missing `judge` function)
- [ ] Duplicate name detection covers all combinations (builtin-builtin, builtin-custom, custom-custom)
- [ ] Vendoring path works for both Python (module/function) and LLM (prompt_file) judges

---

## Revision History

### Rev 1 (2026-05-19) - PR #66 review feedback from @astefanutti

**Trigger**: PR review feedback addressing 6 comments on API design and judge type support

**Spec changes**:
- Replaced `type: builtin` with `builtin` field as type discriminator (follows existing field inference pattern)
- Removed `type` field from `JudgeConfig`, added `builtin` field
- Extended registry to support both Python (`.py`) and LLM prompt (`.md`) judge files
- Added Jinja2 templating for LLM judge prompt rendering
- Added fourth initial judge: `output_completeness` (LLM, quality category)
- Fixed all YAML examples: `condition` -> `if` (matches actual field name)
- Added FR-012 (mutual exclusivity validation) and FR-013 (LLM model override)
- Added vendoring path documentation for LLM judges (use `prompt_file`)

**Quality gates**:
- review-spec: pending (revision in progress)
- review-plan: pending (revision in progress)

**Cascade impact**:
- plan.md: regenerated (new LLM judge infrastructure, Jinja2 dependency, dual-type registry)
- tasks.md: regenerated (20 tasks, was 18; added T006 for Jinja rendering, T010 for LLM judge)
- quickstart.md: regenerated (new syntax, added LLM judge example)
- REVIEWERS.md: Key Decisions, What Changes, How It Works, Areas Needing Attention sections updated

### Rev 2 (2026-05-20) - PR #66 review round 2 from @astefanutti

**Trigger**: PR review feedback on brainstorm and contracts, 7 comments on parameter naming, judge type choices, and API symmetry

**Spec changes**:
- Renamed `config` to `arguments` throughout (eval.yaml field, dataclass field, all docs)
- Changed Python judge signature from `judge(outputs, config=None)` to `judge(outputs, **kwargs)` where kwargs come from `arguments`
- Made `arguments` work for ALL judge types: `**kwargs` for Python (builtin, module/function), Jinja variable for LLM (builtin, prompt_file), local variable for inline `check`
- Moved `no_harmful_content` from Python to LLM judge (harmful content detection benefits from nuanced LLM evaluation over pattern matching)
- Added FQN reference support: `builtin: safety/no_harmful_content` alongside flat `builtin: no_harmful_content`
- Fixed `model` field description: applies to all LLM judges, not just builtins
- Unified LLM judge contract: Jinja2 rendering with `arguments` applies to both builtin `.md` and inline `prompt_file` judges
- Added FR-014 (arguments for all judge types)
- Initial judge set now 2 Python + 2 LLM (was 3 + 1)

**Quality gates**:
- review-spec: pending
- review-plan: pending

**Cascade impact**:
- plan.md: updated (arguments, FQN, no_harmful_content type change)
- tasks.md: updated (arguments, **kwargs, LLM judge changes)
- quickstart.md: regenerated (arguments examples for all judge types)
- REVIEWERS.md: revision history appended

---

<!-- Code phase sections are appended below this line by the phase-manager command -->
