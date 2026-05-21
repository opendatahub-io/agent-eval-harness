# Implementation Plan: Reusable Judges Library

**Branch**: `004-reusable-judges-library` | **Date**: 2026-05-17 (revised 2026-05-19) | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/004-reusable-judges-library/spec.md`

## Summary

Add a reusable judges library to the agent eval harness: an `agent_eval/judges/` package with categorized, skill-agnostic judge files (Python functions and LLM prompt templates) that skill authors reference via a `builtin` field in eval.yaml. The `name` field stays user-defined for thresholds and reports. The harness auto-discovers judges by scanning category subdirectories, auto-detects type from file extension (`.py` or `.md`), builds a flat name registry, and resolves them at scoring time.

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: Jinja2 (for LLM prompt template rendering, applies to all LLM judges)
**Storage**: N/A (file-based package, no persistence)
**Testing**: pytest (existing test suite in `tests/`)
**Target Platform**: Cross-platform (CLI tool)
**Project Type**: Library (Python package extension)
**Performance Goals**: N/A (judges run in existing thread pool)
**Constraints**: Must not break existing eval.yaml configurations or scoring behavior
**Scale/Scope**: 4 initial judges (2 Python + 2 LLM), extensible pattern for future additions

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution is a blank template (no project-specific principles defined). No gate violations.

**Post-Phase 1 re-check**: Design adds one new package (`agent_eval/judges/`) with four leaf files. No architectural violations against project conventions. Extends existing `JudgeConfig` and `load_judges` rather than replacing them.

## Project Structure

### Documentation (this feature)

```text
specs/004-reusable-judges-library/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── eval-yaml-builtin-judge.md
└── tasks.md             # Phase 2 output (created by /speckit-tasks)
```

### Source Code (repository root)

```text
agent_eval/
├── config.py                          # Extended: JudgeConfig + builtin/config fields
├── judges/                            # NEW: Reusable judges package
│   ├── __init__.py                    # BuiltinJudgeRegistry class
│   ├── safety/
│   │   ├── __init__.py
│   │   └── no_harmful_content.md      # LLM: safety judge
│   ├── process/
│   │   ├── __init__.py
│   │   └── tool_call_validation.py    # Python: process quality judge
│   ├── efficiency/
│   │   ├── __init__.py
│   │   └── cost_budget.py             # Python: efficiency judge
│   └── quality/
│       ├── __init__.py
│       └── output_completeness.md     # LLM: output completeness judge

skills/eval-run/scripts/
├── score.py                           # Extended: builtin judge resolution in load_judges
└── report.py                          # Extended: "builtin" type label in scoring summary

tests/
├── test_builtin_judges.py             # Unit tests for judge modules (Python + LLM)
├── test_judge_registry.py             # Unit tests for discovery/resolution
└── test_score_builtin.py              # Integration tests for scoring pipeline
```

**Structure Decision**: Extends existing `agent_eval/` package with a new `judges/` subpackage. No new top-level directories. Tests follow existing `tests/` convention.

## Implementation Approach

### Phase 1: Judge Package and Dual-Type Registry

1. **Create `agent_eval/judges/` package** with `__init__.py` containing `BuiltinJudgeRegistry`
2. **Registry implementation**:
   - `discover()`: Walk category subdirectories. For `.py` files: import module, extract `judge` function, store as Python entry. For `.md` files: record prompt path, store as LLM entry. Detect name collisions across categories.
   - `get(name)`: Return `BuiltinJudgeEntry` or raise `ValueError` listing available names
   - `list_names()`: Return sorted list for error messages and documentation
   - Entry type: `BuiltinJudgeEntry` with `kind` ("python"/"llm"), module/function_name for Python, prompt_path for LLM, category from parent dir
3. **Create category subdirectories**: `safety/`, `process/`, `efficiency/`, `quality/` with `__init__.py` files

### Phase 2: Extend JudgeConfig and Scoring Pipeline

1. **Add fields to `JudgeConfig`** in `agent_eval/config.py`:
   - `builtin: str = ""` (resolves to a registered builtin name, supports flat or FQN)
   - `arguments: dict = field(default_factory=dict)` (passed as `**kwargs` to Python, Jinja var to LLM, local to check)
2. **Extend `load_judges()` in `score.py`**:
   - Add `builtin` branch before existing type inference
   - Validate mutual exclusivity: `builtin` set alongside `check`/`prompt`/`prompt_file`/`module`/`function` raises error
   - Instantiate `BuiltinJudgeRegistry` lazily (only on first `builtin` encounter)
   - For Python entries: wrap callable to pass `arguments` as `**kwargs`
   - For LLM entries: render Jinja2 template with `arguments` and `outputs`, send to LLM, parse JSON response into `(bool, str)`
   - For existing `module`/`function` judges: pass `arguments` as `**kwargs` when present (backward-compatible)
   - For `prompt_file` judges: apply Jinja2 rendering with `arguments` when present
   - For `check` judges: inject `arguments` dict into eval locals
3. **Add duplicate name validation** at start of `load_judges()`
4. **Jinja2 rendering**: Add template rendering utility that takes a `.md` path, renders with `arguments` and `outputs` variables, returns the prompt string. Use Jinja2's `Environment` with `tojson` filter available. Apply to both builtin `.md` files and inline `prompt_file` judges when `arguments` is present.

### Phase 3: Implement Four Initial Judges

1. **`safety/no_harmful_content.md`** (LLM): Jinja2 prompt template that evaluates conversation and file contents for harmful or dangerous content via nuanced LLM evaluation. Supports `{{ arguments.categories }}` for customizable content categories to check. Responds with JSON `{"passed": bool, "rationale": str}`.
2. **`process/tool_call_validation.py`** (Python): Checks `tool_calls` and `events` for tool execution errors. Returns `(False, reason)` if any tool call has error results.
3. **`efficiency/cost_budget.py`** (Python): Checks `cost_usd` against `kwargs.get("max_cost_usd", 1.0)` default threshold. Configurable via eval.yaml `arguments` dict.
4. **`quality/output_completeness.md`** (LLM): Jinja2 prompt template that evaluates output completeness. Supports `{{ arguments.strictness }}` and `{{ arguments.criteria }}` for customization. Responds with JSON `{"passed": bool, "rationale": str}`.

### Phase 4: Report Labeling

1. **Update `_render_scoring_summary` in `report.py`**: Add "builtin" to the type detection logic, display category alongside type label
2. **Pass judge type metadata** through the scoring results so the report can distinguish builtin from code/llm judges

### Phase 5: Tests

1. **Unit tests for each judge module**: Test pass/fail with synthetic case records, test missing data handling, test config parameter behavior. For LLM judge: test template rendering (mock LLM call).
2. **Unit tests for registry**: Test discovery of both `.py` and `.md` files, name collision detection, unknown name error, `kind` auto-detection
3. **Integration tests**: Test `load_judges` with `builtin` config (both Python and LLM types), test full scoring pipeline with mixed judge types

## Key Design Decisions

1. **`builtin` field as discriminator** (not `type: builtin`): Follows the existing field-based type inference pattern. `name` stays user-defined. Decided based on reviewer feedback (PR #66, @astefanutti).
2. **Dual-type registry**: Auto-detects judge type from file extension rather than requiring explicit type markers. Supports flat names and FQN (`category/name`).
3. **`arguments` as `**kwargs`**: Renamed from `config` to `arguments`. Passed as `**kwargs` to Python judges, as Jinja template variable to LLM judges, as local variable to `check` judges. Works uniformly across all judge types.
4. **Jinja2 for all LLM judges**: Provides conditionals, loops, and filters (especially `tojson`) for flexible prompt construction. Applies to both builtin `.md` files and inline `prompt_file` judges when `arguments` is present.
5. **Lazy registry instantiation**: Only scan `agent_eval/judges/` on first encounter of a `builtin` field in the judge list. Avoids filesystem overhead for configs that don't use builtins.

## Complexity Tracking

No complexity violations. The feature adds one new package with four leaf files, a Jinja2 dependency for template rendering, and extends two existing files (`config.py`, `score.py`).
