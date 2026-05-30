# Contract: Config Discovery API

Internal Python API for auto-discovering eval configs across layout conventions.

## `discover_configs(project_root: Path) -> list[DiscoveryResult]`

Scans the project for eval.yaml files in this order:
1. `eval/*/eval.yaml` (nested convention)
2. `eval/*.yaml` (flat convention, excluding non-eval YAML files)
3. `eval.yaml` at project root (deprecated)

Returns a list of `DiscoveryResult` objects sorted by path. Each result includes the config path, the skill name (read from the `skill` field inside the YAML), and whether the config is at a deprecated location.

### Filtering

- Files that fail YAML parsing are skipped with a warning to stderr
- Files without a `skill` field are skipped (not valid eval configs)
- Non-eval YAML files in `eval/` (e.g., `eval/.eval-convention`) are excluded by checking for the `skill` field

### Usage Pattern

```python
from agent_eval.config import discover_configs

configs = discover_configs(Path.cwd())

if len(configs) == 0:
    # No configs found, suggest /eval-analyze
elif len(configs) == 1:
    # Auto-select
    config = EvalConfig.from_yaml(configs[0].path)
else:
    # Prompt user to select
    for i, c in enumerate(configs):
        print(f"  {i+1}. {c.skill_name} ({c.path})")
```

## `resolve_convention(project_root: Path) -> str | None`

Reads `eval/.eval-convention` if it exists. Returns `"nested"`, `"flat"`, or `None`.

## `save_convention(project_root: Path, convention: str) -> None`

Writes the convention name to `eval/.eval-convention`.
