# Data Model: Flexible Eval Directory Layout

## Entities

### EvalConfig (modified)

Existing dataclass at `agent_eval/config.py:192`. New and modified fields:

| Field | Type | Description |
|-------|------|-------------|
| `config_dir` | `Optional[Path]` | **NEW**. Parent directory of the loaded eval.yaml. Set during `from_yaml()`. Used as base for resolving `dataset.path` only. Defaults to `None` (unset for programmatic construction). When `None`, path resolution falls back to `Path.cwd()`. |
| `project_root` | `Path` (property) | **UNCHANGED**. Returns `Path.cwd()`. Used for repo-level concerns (symlinks, judge modules, settings). NOT redefined to `config_dir`. |

No changes to existing fields. All existing fields remain backward compatible.

### EvalLayout (new)

A lightweight concept representing the directory structure for eval artifacts. Currently one supported layout:

| Layout | Config Path | Dataset Path | Description |
|--------|-------------|--------------|-------------|
| `nested` | `eval/<name>/eval.yaml` | User-specified via `dataset.path` | Each eval target gets its own subdirectory under `eval/`. |

Datasets are NOT derived from the layout. They are independently located via `dataset.path` in each eval.yaml.

Persisted as a single-line text file at `eval/.eval-layout`.

### DiscoveryResult (new, internal)

Returned by `discover_configs()`. Not persisted.

| Field | Type | Description |
|-------|------|-------------|
| `path` | `Path` | Absolute path to the eval.yaml file |
| `eval_name` | `str` | Value of the `skill` field from the eval.yaml (serves as eval identifier) |
| `is_root` | `bool` | `True` if this config is at the project root |

## Relationships

```
EvalLayout    1──*  EvalConfig    (a layout determines where configs are scaffolded in multi-eval projects)
EvalConfig    1──1  Dataset       (each config points to one dataset via dataset.path; datasets can be shared)
EvalConfig    1──*  RunOutput     (each config produces runs under AGENT_EVAL_RUNS_DIR/<eval-name>/)
DiscoveryResult   *──1  EvalConfig    (discovery finds configs, each wraps one EvalConfig)
```

## State Transitions

### Eval Config Lifecycle

```
[absent] ──(first /eval-analyze)──> [created at project root]
[root-level, single] ──(second /eval-analyze)──> [offer to reorganize into eval/]
[root-level] ──(reorganization accepted)──> [moved to eval/<name>/]
[root-level] ──(reorganization declined + --config)──> [new config at explicit path]
[any location] ──(--config explicit)──> [used directly, no layout]
```

### Layout Persistence

```
[absent] ──(reorganization into eval/)──> [written to eval/.eval-layout]
[persisted] ──(subsequent /eval-analyze)──> [read and reused, no prompt]
[persisted] ──(--config override)──> [ignored for this run]
```
