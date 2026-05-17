# Implementation Plan: Reusable Judges Library

**Branch**: `004-reusable-judges-library` | **Date**: 2026-05-17 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/004-reusable-judges-library/spec.md`

## Summary

Add a reusable judges library to the agent eval harness: a `agent_eval/judges/` package with categorized, skill-agnostic judge modules (safety, process, efficiency) that skill authors reference via `type: builtin` in eval.yaml. The harness auto-discovers judges by scanning category subdirectories, builds a flat name registry, and resolves them at scoring time using the same result normalization pipeline as existing code judges.

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: None beyond stdlib (judges use only `agent_eval` internals)
**Storage**: N/A (file-based package, no persistence)
**Testing**: pytest (existing test suite in `tests/`)
**Target Platform**: Cross-platform (CLI tool)
**Project Type**: Library (Python package extension)
**Performance Goals**: N/A (judges run in existing thread pool)
**Constraints**: Must not break existing eval.yaml configurations or scoring behavior
**Scale/Scope**: 3 initial judges, extensible pattern for future additions

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution is a blank template (no project-specific principles defined). No gate violations.

**Post-Phase 1 re-check**: Design adds one new package (`agent_eval/judges/`) with three leaf modules. No architectural violations against project conventions. Extends existing `JudgeConfig` and `load_judges` rather than replacing them.

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
├── config.py                        # Extended: JudgeConfig + type/config fields
├── judges/                          # NEW: Reusable judges package
│   ├── __init__.py                  # BuiltinJudgeRegistry class
│   ├── safety/
│   │   ├── __init__.py
│   │   └── no_harmful_content.py    # Safety judge
│   ├── process/
│   │   ├── __init__.py
│   │   └── tool_call_validation.py  # Process quality judge
│   └── efficiency/
│       ├── __init__.py
│       └── cost_budget.py           # Efficiency judge

skills/eval-run/scripts/
├── score.py                         # Extended: builtin judge resolution in load_judges
└── report.py                        # Extended: "builtin" type label in scoring summary

tests/
├── test_builtin_judges.py           # Unit tests for judge modules
├── test_judge_registry.py           # Unit tests for discovery/resolution
└── test_score_builtin.py            # Integration tests for scoring pipeline
```

**Structure Decision**: Extends existing `agent_eval/` package with a new `judges/` subpackage. No new top-level directories. Tests follow existing `tests/` convention.

## Implementation Approach

### Phase 1: Judge Package and Registry

1. **Create `agent_eval/judges/` package** with `__init__.py` containing `BuiltinJudgeRegistry`
2. **Registry implementation**:
   - `discover()`: Walk category subdirectories, import modules, extract `judge` function from each, build `{name: callable}` map
   - Detect name collisions across categories at discovery time
   - `get(name)`: Return callable or raise `ValueError` listing available names
   - `list_names()`: Return sorted list for error messages and documentation
3. **Create category subdirectories**: `safety/`, `process/`, `efficiency/` with `__init__.py` files

### Phase 2: Extend JudgeConfig and Scoring Pipeline

1. **Add fields to `JudgeConfig`** in `agent_eval/config.py`:
   - `type: str = ""` (values: "", "builtin")
   - `config: dict = field(default_factory=dict)`
2. **Extend `load_judges()` in `score.py`**:
   - Add `type == "builtin"` branch before existing type inference
   - Instantiate `BuiltinJudgeRegistry`, call `discover()`, call `get(name)`
   - Wrap callable to pass `config` from `JudgeConfig.config`
3. **Add duplicate name validation** at start of `load_judges()`
4. **Update `_load_code_judge`** to also pass `config` if present (backward-compatible since existing code judges don't use it)

### Phase 3: Implement Three Initial Judges

1. **`safety/no_harmful_content.py`**: LLM-free check scanning `conversation` and `files` for harmful content patterns. Returns `(False, reason)` if flagged content detected.
2. **`process/tool_call_validation.py`**: Checks `tool_calls` and `events` for tool execution errors. Returns `(False, reason)` if any tool call has error results.
3. **`efficiency/cost_budget.py`**: Checks `cost_usd` against `config.get("max_cost_usd", 1.0)` default threshold. Configurable via eval.yaml `config` dict.

### Phase 4: Report Labeling

1. **Update `_render_scoring_summary` in `report.py`**: Add "builtin" to the type detection logic, display category alongside type label
2. **Pass judge type metadata** through the scoring results so the report can distinguish builtin from code judges

### Phase 5: Tests

1. **Unit tests for each judge module**: Test pass/fail with synthetic case records, test missing data handling, test config parameter behavior
2. **Unit tests for registry**: Test discovery, name collision detection, unknown name error
3. **Integration tests**: Test `load_judges` with `type: builtin` config, test full scoring pipeline with mixed judge types

## Complexity Tracking

No complexity violations. The feature adds one new package with three leaf modules and extends two existing files (`config.py`, `score.py`).
