# Data Model: Reusable Judges Library

**Date**: 2026-05-17 | **Feature**: 004-reusable-judges-library

## Entities

### JudgeConfig (extended)

Existing dataclass in `agent_eval/config.py`. New fields marked with `[NEW]`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| name | str | "" | Judge identifier (must be unique across all judges in eval.yaml) |
| description | str | "" | What this judge checks |
| condition | str | "" | Python expression for conditional execution |
| check | str | "" | Inline Python snippet |
| prompt | str | "" | LLM judge prompt |
| prompt_file | str | "" | Path to LLM judge prompt file |
| context | list | [] | Supplementary context file paths |
| feedback_type | str | "" | Output type hint |
| model | str | "" | Per-judge model override |
| module | str | "" | External code judge module path |
| function | str | "" | External code judge function name |
| **type** | str | "" | **[NEW]** Judge type discriminator. Values: "", "builtin". Empty string preserves current inference behavior. |
| **config** | dict | {} | **[NEW]** Arbitrary config dict passed to judge function as second argument. |

**Type determination logic** (updated):
1. If `type == "builtin"`: resolve via builtin registry using `name`
2. If `check` is set: inline check
3. If `prompt` or `prompt_file` is set: LLM judge
4. If `module` and `function` are set: external code judge
5. Otherwise: skip with warning

### BuiltinJudgeRegistry

Runtime-only object (not persisted). Built by scanning `agent_eval/judges/` package.

| Field | Type | Description |
|-------|------|-------------|
| _judges | dict[str, tuple[ModuleType, str]] | Map of judge name to (module, function_name) |

**Operations**:
- `discover()`: Scan category subdirectories, import modules, extract judge functions, detect name collisions
- `get(name)`: Return judge callable or raise error listing all available names
- `list_names()`: Return sorted list of all available judge names

### Judge Module (file convention)

Each file in `agent_eval/judges/<category>/` follows this convention:

| Attribute | Type | Description |
|-----------|------|-------------|
| `__version__` | str | Version string for documentation (e.g., "1.0") |
| `judge(outputs, config=None)` | callable | Scoring function returning `(bool, str)` |
| module docstring | str | Describes the check, required fields, and failure meaning |

**Naming**: The judge name is derived from the module filename (without `.py` extension). E.g., `no_harmful_content.py` registers as `no_harmful_content`.

## Relationships

```
eval.yaml judges[] ──> JudgeConfig (parsed)
                           │
                           ├─ type: "builtin" ──> BuiltinJudgeRegistry.get(name)
                           │                          │
                           │                          └─> agent_eval/judges/<category>/<name>.py
                           │
                           ├─ check: set ──> _make_inline_check()
                           ├─ prompt/prompt_file: set ──> _load_llm_judge()
                           └─ module/function: set ──> _load_code_judge()
```

## Package Structure

```
agent_eval/
├── judges/
│   ├── __init__.py          # BuiltinJudgeRegistry class
│   ├── safety/
│   │   ├── __init__.py
│   │   └── no_harmful_content.py
│   ├── process/
│   │   ├── __init__.py
│   │   └── tool_call_validation.py
│   └── efficiency/
│       ├── __init__.py
│       └── cost_budget.py
└── config.py                # JudgeConfig extended with type + config fields
```

## Validation Rules

1. Judge names MUST be unique across all judges in a single eval.yaml (enforced in `load_judges`)
2. Builtin judge names MUST be unique across all category subdirectories (enforced in `BuiltinJudgeRegistry.discover()`)
3. `type: builtin` requires `name` to match a registered builtin judge (error lists available names)
4. `config` dict is optional for all judge types but only meaningful for builtin and code judges
