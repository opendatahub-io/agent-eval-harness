# Data Model: Reusable Judges Library

**Date**: 2026-05-17 (revised 2026-05-20) | **Feature**: 004-reusable-judges-library

## Entities

### JudgeConfig (extended)

Existing dataclass in `agent_eval/config.py`. New fields marked with `[NEW]`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| name | str | "" | Judge identifier, user-defined (must be unique across all judges in eval.yaml) |
| description | str | "" | What this judge checks |
| condition | str | "" | Python expression for conditional execution (YAML field: `if`) |
| check | str | "" | Inline Python snippet |
| prompt | str | "" | LLM judge prompt |
| prompt_file | str | "" | Path to LLM judge prompt file |
| context | list | [] | Supplementary context file paths |
| feedback_type | str | "" | Output type hint |
| model | str | "" | Per-judge model override |
| module | str | "" | External code judge module path |
| function | str | "" | External code judge function name |
| **builtin** | str | "" | **[NEW]** Builtin judge name (flat or FQN like `safety/no_harmful_content`). Resolves to a bundled judge file via the registry. Mutually exclusive with `check`, `prompt`, `prompt_file`, `module`, `function`. |
| **arguments** | dict | `field(default_factory=dict)` | **[NEW]** Arbitrary arguments dict. Passed as `**kwargs` to Python judges, or as Jinja template variable to LLM judges. Works for all judge types (builtin, module/function, check, prompt/prompt_file). Uses factory default to avoid shared mutable state. |

**Type determination logic** (updated):
1. If `builtin` is set: resolve via builtin registry. All other type-discriminating fields (`check`, `prompt`, `prompt_file`, `module`, `function`) MUST be empty, otherwise raise a validation error.
2. If `check` is set: inline check
3. If `prompt` or `prompt_file` is set: LLM judge
4. If `module` and `function` are set: external code judge
5. Otherwise: skip with warning

### BuiltinJudgeRegistry

Runtime-only object (not persisted). Built by scanning `agent_eval/judges/` package.

| Field | Type | Description |
|-------|------|-------------|
| _judges | dict[str, BuiltinJudgeEntry] | Map of judge name to entry (module + metadata) |

**BuiltinJudgeEntry** (internal):

| Field | Type | Description |
|-------|------|-------------|
| kind | str | `"python"` or `"llm"` (auto-detected from file extension) |
| module | ModuleType | Python module (for `kind == "python"`) |
| function_name | str | Function name within module (for `kind == "python"`) |
| prompt_path | Path | Path to `.md` template file (for `kind == "llm"`) |
| category | str | Parent directory name (e.g., "safety", "quality") |

**Operations**:
- `discover()`: Scan category subdirectories. For `.py` files: import module, extract judge function, store as Python entry. For `.md` files: record prompt path, store as LLM entry. Detect name collisions across categories.
- `get(name) -> BuiltinJudgeEntry`: Look up entry in `_judges` by flat name or FQN (`category/name`). Raises `ValueError` listing all available names if not found.
- `list_names()`: Return sorted list of all available judge names

### Python Judge File Convention

Each `.py` file in `agent_eval/judges/<category>/` follows this convention:

| Attribute | Type | Description |
|-----------|------|-------------|
| `__version__` | str | Version string for documentation (e.g., "1.0") |
| `judge(outputs, **kwargs)` | callable | Scoring function returning `(bool, str)`. `**kwargs` receives values from `arguments` dict in eval.yaml. |
| module docstring | str | Describes the check, required fields, and failure meaning |

### LLM Judge File Convention

Each `.md` file in `agent_eval/judges/<category>/` follows this convention:

| Element | Description |
|---------|-------------|
| YAML frontmatter | Contains `__version__` and optional metadata |
| Preamble section | Describes the check, required fields, and failure meaning |
| Jinja2 template body | Prompt template with `{{ arguments }}` and `{{ outputs }}` variables |

Example structure:
```markdown
---
__version__: "1.0"
---
<!-- Evaluates whether the agent output is complete and addresses all aspects of the input. -->
<!-- Required fields: conversation, files -->

Evaluate the following agent output for completeness.

Strictness: {{ arguments.strictness | default('medium') }}

{{ outputs | tojson }}

Respond with a JSON object: {"passed": true/false, "rationale": "..."}
```

**Naming**: The judge name is derived from the filename without extension. E.g., `no_harmful_content.py` and `output_completeness.md` register as `no_harmful_content` and `output_completeness` respectively.

## Relationships

```
eval.yaml judges[] ──> JudgeConfig (parsed)
                           │
                           ├─ builtin: set ──> BuiltinJudgeRegistry.get(name)
                           │                      │
                           │                      ├─ kind: "python" ──> agent_eval/judges/<cat>/<name>.py
                           │                      └─ kind: "llm"    ──> agent_eval/judges/<cat>/<name>.md
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
│   │   └── no_harmful_content.md
│   ├── process/
│   │   ├── __init__.py
│   │   └── tool_call_validation.py
│   ├── efficiency/
│   │   ├── __init__.py
│   │   └── cost_budget.py
│   └── quality/
│       ├── __init__.py
│       └── output_completeness.md
└── config.py                # JudgeConfig extended with builtin + arguments fields
```

## Validation Rules

1. Judge names MUST be unique across all judges in a single eval.yaml (enforced in `load_judges`)
2. Builtin judge names MUST be unique across all category subdirectories (enforced in `BuiltinJudgeRegistry.discover()`)
3. `builtin` field requires its value to match a registered builtin judge name (error lists available names)
4. `builtin` is mutually exclusive with `check`, `prompt`, `prompt_file`, `module`, `function`
5. `arguments` dict is optional and works for all judge types: `**kwargs` for Python, Jinja variable for LLM, local variable for `check`
