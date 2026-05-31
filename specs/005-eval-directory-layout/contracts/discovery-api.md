# Contract: Config Discovery API

Internal Python API for auto-discovering eval configs across layouts.

## `discover_configs(project_root: Path) -> list[DiscoveryResult]`

Scans the project for eval.yaml files in this order:
1. `eval/*/eval.yaml` (nested layout)
2. `eval/*.yaml` (flat layout, excluding non-eval YAML files)
3. `eval.yaml` at project root

Returns a list of `DiscoveryResult` objects sorted by path. Each result includes the config path, the eval name (read from the `skill` field inside the YAML), and whether the config is at the project root.

### Filtering

- Files that fail YAML parsing are skipped with a warning to stderr
- Files without a `skill` field are skipped (not valid eval configs)
- Non-eval YAML files in `eval/` are excluded by checking for the `skill` field

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
        print(f"  {i+1}. {c.eval_name} ({c.path})")
```

Layout is inferred from the discovery results (no persistence file). If `discover_configs()` finds configs under `eval/*/eval.yaml`, the project uses nested layout. If only a root `eval.yaml` exists, the project uses root layout.
