# Review Guide: Reusable Judges Library

**Generated**: 2026-05-17 | **Spec**: [spec.md](spec.md)

## Why This Change

Skill authors evaluating their Claude Code skills currently write every judge from scratch, even for common patterns like safety checks, cost budgets, or tool call validation. There are no reusable, skill-agnostic judges that ship with the harness. This means every project independently re-invents the same guardrail checks, leading to inconsistent implementations and wasted setup time.

## What Changes

The harness gains a built-in judges library: a `agent_eval/judges/` package with categorized judge modules (safety, process, efficiency) that skill authors reference via `type: builtin` in eval.yaml. Authors add two lines of config (name and type) to get a working judge, no Python required. The scoring pipeline and report rendering are extended to support the new judge type alongside existing inline check, LLM, and external code judges. No breaking changes to existing eval.yaml configurations.

## How It Works

A new `agent_eval/judges/` Python package contains category subdirectories (`safety/`, `process/`, `efficiency/`), each with standalone judge modules. A `BuiltinJudgeRegistry` class in the package `__init__.py` auto-discovers judges by scanning subdirectories at scoring time, building a flat `{name: callable}` map. The existing `JudgeConfig` dataclass gets two new fields: `type` (discriminator, value `"builtin"`) and `config` (optional dict passed to the judge function). The `load_judges()` function in `score.py` gains a new routing branch for `type == "builtin"` that resolves the judge via the registry, wraps it to pass the config dict, and returns it in the same `(name, scorer, condition, judge_type)` tuple format. Judge functions follow the signature `(outputs: dict, config: dict | None = None) -> tuple[bool, str]`.

Three initial judges ship: `no_harmful_content` (scans output for harmful content), `tool_call_validation` (checks tool calls completed without errors), and `cost_budget` (verifies execution cost against a configurable threshold).

## When It Applies

**Applies when**:
- Configuring judges in eval.yaml for any skill evaluation
- Wanting common guardrail checks without writing custom Python
- Needing to customize a built-in judge's behavior (vendor and modify the file)

**Does not apply when**:
- Using judge presets or curated bundles (out of scope, future feature)
- Comparing event patterns between runs for regression fingerprinting (out of scope)
- Writing LLM-based or inline check judges (existing judge types, unchanged)

## Key Decisions

1. **Flat name resolution over qualified paths**: Judges are referenced by simple name (e.g., `no_harmful_content`) rather than category-qualified paths (`safety/no_harmful_content`). The registry auto-discovers across all category directories and detects name collisions at startup. Simpler for authors, and collisions are caught early with clear errors.

2. **Optional config dict over environment variables**: Built-in judges accept an optional `config` parameter from eval.yaml rather than using environment variables. This keeps configuration per-judge (env vars are global and would collide with multiple judges).

3. **Auto-discovery over static registry**: The `BuiltinJudgeRegistry` scans subdirectories dynamically rather than maintaining a static manifest in `__init__.py`. Adding a new judge only requires dropping a file in the right category directory.

4. **Documentation-only versioning**: Each judge module defines a `__version__` string for changelog purposes. No runtime pinning mechanism in eval.yaml. Authors who need old behavior can vendor the judge file.

5. **Extending load_judges over new scoring module**: The existing `load_judges()` function gets a new routing branch rather than a separate builtin judge runner. This avoids duplicating the result normalization logic.

## Areas Needing Attention

- The `no_harmful_content` judge uses pattern-based detection without an LLM. Its effectiveness depends on the quality of the pattern list. Consider whether this is sufficient or if an LLM-based safety judge should be offered as an alternative.
- The `config` parameter changes the judge function signature from `(outputs)` to `(outputs, config=None)`. While backward-compatible via the default, existing external code judges that don't accept `**kwargs` could break if `config` is ever passed to them unintentionally. The implementation must only pass `config` for builtin judges.
- The 4-tuple change to `load_judges()` return format touches the `score_cases()` function and potentially other callers. All call sites must be updated.

## Open Questions

No open questions identified. All ambiguities were resolved during the clarification session (see spec.md Clarifications section).

## Review Checklist

- [ ] Key decisions are justified
- [ ] Breaking changes are documented with migration guidance
- [ ] Scope matches the stated boundaries
- [ ] Success criteria are achievable
- [ ] No unstated assumptions
- [ ] Judge function signature is consistent across all three judge modules
- [ ] `BuiltinJudgeRegistry` handles edge cases (empty directories, non-Python files, missing `judge` function)
- [ ] Duplicate name detection covers all combinations (builtin-builtin, builtin-custom, custom-custom)

---

<!-- Code phase sections are appended below this line by the phase-manager command -->
