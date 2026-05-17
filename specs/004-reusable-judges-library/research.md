# Research: Reusable Judges Library

**Date**: 2026-05-17 | **Feature**: 004-reusable-judges-library

## 1. Existing Judge Infrastructure

### Decision: Extend `JudgeConfig` and `load_judges` in `score.py`

**Rationale**: The current scoring pipeline in `skills/eval-run/scripts/score.py` already supports three judge types (inline check, LLM prompt, external code) via a routing pattern in `load_judges()`. Adding `type: builtin` as a fourth route keeps the architecture consistent. The `JudgeConfig` dataclass in `agent_eval/config.py` needs a new `type` field (currently the type is inferred from which fields are set).

**Alternatives considered**:
- New scoring module: Rejected because splitting judge resolution across files adds complexity with no benefit. The existing `load_judges` function is the single entry point.
- Separate builtin judge runner: Rejected because builtin judges use the same `(outputs, config) -> (bool, str)` contract as code judges. A separate runner would duplicate the result normalization logic.

## 2. Judge Discovery and Registry

### Decision: Auto-discover at scoring time via `importlib.resources` / package scanning

**Rationale**: The `agent_eval/judges/` directory is a Python package. At scoring time, `load_judges` scans all category subdirectories, imports each module, and builds a `{name: module}` flat registry. This avoids a static manifest file that would fall out of sync.

**Alternatives considered**:
- Static `__init__.py` registry: Rejected because adding a new judge would require editing both the judge file and the registry. Auto-discovery is self-maintaining.
- Entry points / plugin registry: Over-engineered for an internal package directory. Entry points are for third-party plugins.

## 3. Config Parameter Passing

### Decision: Add optional `config` dict to eval.yaml judge entries, passed as second argument

**Rationale**: The existing code judge path (`_load_code_judge`) calls `scorer(outputs=record)`. For builtin judges, the call becomes `scorer(outputs=record, config=judge_config.config)`. Judges that don't need config use `config=None` default. This requires adding a `config` field to `JudgeConfig`.

**Alternatives considered**:
- Environment variables: Rejected because config is per-judge, and env vars are global. Multiple judges would collide.
- Embed config in outputs: Rejected because outputs represent case data, not judge configuration. Mixing concerns.

## 4. Report Differentiation

### Decision: Add `source` metadata to judge results, render as label in report

**Rationale**: The report's `_render_scoring_summary` already shows a "Type" column (check, llm, code). For builtin judges, this becomes "builtin" with the category shown. The per-case detail view remains unchanged since the result structure is identical.

**Alternatives considered**:
- Separate report section for builtin judges: Rejected because it breaks the single scoring summary view. A label is sufficient for differentiation.
- Color coding only: Rejected because it's not accessible and doesn't convey the category.

## 5. Initial Judge Implementations

### Decision: Three judges covering the declared categories

| Judge | Category | What it checks | Key outputs fields |
|-------|----------|----------------|--------------------|
| `no_harmful_content` | safety | Agent output contains no harmful, dangerous, or inappropriate content | `conversation`, `files` |
| `tool_call_validation` | process | Tool calls complete successfully, no errors in tool results | `tool_calls`, `events` |
| `cost_budget` | efficiency | Execution cost stays within configurable threshold | `cost_usd` |

**Rationale**: These three map directly to the most common evaluation needs surfaced across existing eval.yaml files in the project. Each requires different record fields, demonstrating the pattern for future judges.

## 6. Duplicate Name Detection

### Decision: Validate at judge loading time in `load_judges`

**Rationale**: The `load_judges` function already iterates all judges. Adding a set-based name check at the start costs one pass through the config list. This catches both builtin-builtin and builtin-custom collisions before any judge executes.

**Alternatives considered**:
- Validate in config parsing: Too early, since builtin names aren't resolved until scoring. Config parsing doesn't know what builtin judges exist.
- Validate at YAML load: Same problem as above.
