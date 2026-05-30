# Data Model: Flexible Eval Directory Layout

## Entities

### EvalConfig (modified)

Existing dataclass at `agent_eval/config.py:192`. New and modified fields:

| Field | Type | Description |
|-------|------|-------------|
| `config_dir` | `Path` | **NEW**. Parent directory of the loaded eval.yaml. Set during `from_yaml()`. Used as base for all relative path resolution. |
| `project_root` | `Path` (property) | **MODIFIED**. Currently returns `Path.cwd()`. Will return `config_dir` or fall back to `Path.cwd()` when `config_dir` is unset. |

No changes to existing fields. All existing fields remain backward compatible.

### LayoutConvention (new)

A lightweight enum-like concept (not necessarily a Python class). Two values:

| Convention | Config Path | Dataset Path | Description |
|------------|-------------|--------------|-------------|
| `nested` | `eval/<skill>/eval.yaml` | `eval/<skill>/cases/` | Default. Each skill gets its own subdirectory. |
| `flat` | `eval/<skill>.yaml` | `eval/cases/<skill>/` | Configs at eval root, data namespaced inside shared parents. |

Persisted as a single-line text file at `eval/.eval-convention`.

### DiscoveryResult (new, internal)

Returned by `discover_configs()`. Not persisted.

| Field | Type | Description |
|-------|------|-------------|
| `path` | `Path` | Absolute path to the eval.yaml file |
| `skill_name` | `str` | Value of the `skill` field from the eval.yaml |
| `is_deprecated` | `bool` | `True` if this config is at the project root |

## Relationships

```
LayoutConvention  1──*  EvalConfig    (a convention determines where configs are scaffolded)
EvalConfig        1──1  Dataset       (each config points to one dataset directory)
EvalConfig        1──*  RunOutput     (each config produces runs under AGENT_EVAL_RUNS_DIR/<skill>/)
DiscoveryResult   *──1  EvalConfig    (discovery finds configs, each wraps one EvalConfig)
```

## State Transitions

### Eval Config Lifecycle

```
[absent] ──(/eval-analyze scaffold)──> [created at convention path]
[root-level] ──(migration accepted)──> [moved to convention path]
[root-level] ──(migration declined)──> [root-level, deprecated]
[any location] ──(--config explicit)──> [used directly, no convention]
```

### Convention Persistence

```
[absent] ──(first /eval-analyze)──> [user selects, written to eval/.eval-convention]
[persisted] ──(subsequent /eval-analyze)──> [read and reused, no prompt]
[persisted] ──(--config override)──> [ignored for this run]
```
