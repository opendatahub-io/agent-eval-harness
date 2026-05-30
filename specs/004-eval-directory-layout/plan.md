# Implementation Plan: Flexible Eval Directory Layout

**Branch**: `004-eval-directory-layout`
**Spec**: [spec.md](spec.md)
**Research**: [research.md](research.md)
**Data Model**: [data-model.md](data-model.md)

## Technical Context

- **Language**: Python 3
- **Core module**: `agent_eval/config.py` (EvalConfig dataclass, YAML parsing)
- **Skill definitions**: `skills/eval-analyze/SKILL.md`, `skills/eval-run/SKILL.md` (LLM-executed skill instructions)
- **Scripts**: `skills/eval-run/scripts/workspace.py`, `score.py`, `report.py`, `preflight.py` (use `AGENT_EVAL_RUNS_DIR` and `EvalConfig`)
- **Dependencies**: `scripts/ensure_deps.py` (has basic `_find_eval_yaml()`)
- **Tests**: `tests/` (unit), `tests/e2e/` (end-to-end with real API calls)

## Change Summary

Seven work areas, ordered by dependency:

### Area 1: Path Resolution Foundation (FR-011)

**Files**: `agent_eval/config.py`

Add `config_dir: Path` field to `EvalConfig`. Set it in `from_yaml()` from the config file's parent directory. Update the `project_root` property to return `config_dir` when available, falling back to `Path.cwd()`.

This is the foundation: all other changes depend on paths resolving correctly relative to the config file.

### Area 2: Auto-Discovery (FR-007 to FR-010)

**Files**: `agent_eval/config.py` (new `discover_configs` function), `scripts/ensure_deps.py` (update `_find_eval_yaml`)

Implement `discover_configs(project_root)` per [discovery-api.md](contracts/discovery-api.md). Scans `eval/*/eval.yaml`, `eval/*.yaml`, root `eval.yaml`. Returns list of `DiscoveryResult` with path, skill name, deprecation flag.

Update `_find_eval_yaml()` in `ensure_deps.py` to use the same discovery logic.

### Area 3: Convention Persistence (FR-006)

**Files**: `agent_eval/config.py` (new `resolve_convention`, `save_convention` functions)

Simple read/write of `eval/.eval-convention` text file. Called by eval-analyze SKILL.md during scaffolding.

### Area 4: Eval-Analyze Scaffolding Updates (FR-001 to FR-005)

**Files**: `skills/eval-analyze/SKILL.md`

Update the skill instructions to:
1. Check for existing convention via `resolve_convention()`
2. If no convention set: present layout options (nested as recommended default), save choice
3. Scaffold eval.yaml, eval.md, and cases directory at the convention-appropriate path
4. Support `--config <path>` to bypass convention selection
5. Detect root-level `eval.yaml` and offer migration (FR-013, FR-014)

This is primarily SKILL.md instruction changes, not Python code. The LLM follows the instructions to create directories and files.

### Area 5: Eval-Run Discovery Integration (FR-007 to FR-010)

**Files**: `skills/eval-run/SKILL.md`, `skills/eval-run/scripts/workspace.py`, `skills/eval-run/scripts/preflight.py`

Update eval-run SKILL.md to:
1. If `--config` not provided: call `discover_configs()` via a helper script
2. Auto-select or prompt based on result count
3. Pass resolved config path to downstream scripts

Update `workspace.py` default: remove `default="eval.yaml"` from `--config` argparse, require it explicitly (the SKILL.md will always pass it after discovery).

Update `preflight.py` to resolve runs directory using `config.config_dir` and `AGENT_EVAL_RUNS_DIR/<skill>/`.

### Area 6: Run Isolation (FR-012)

**Files**: `skills/eval-run/scripts/score.py`, `skills/eval-run/scripts/report.py`, `skills/eval-mlflow/scripts/log_results.py`, `skills/eval-mlflow/scripts/attach_feedback.py`

Update all scripts that read `AGENT_EVAL_RUNS_DIR` to append the skill name: `runs_dir / config.skill`. The skill name comes from the `EvalConfig.skill` field loaded from eval.yaml.

### Area 7: Other Eval Commands (FR-007, SC-007)

**Files**: `skills/eval-dataset/SKILL.md`, `skills/eval-optimize/SKILL.md`, `skills/eval-review/SKILL.md`, `skills/eval-mlflow/SKILL.md`

Update each SKILL.md to use the same discovery pattern as eval-run: if `--config` not provided, discover and select. These are lighter touches since they share the same discovery function.

### Area 8: Migration (FR-013 to FR-017)

**Files**: New `agent_eval/migrate.py` or inline in eval-analyze SKILL.md

Implement migration logic per [migration-api.md](contracts/migration-api.md). Called from eval-analyze SKILL.md when a root-level config is detected. Updates `dataset.path` and `outputs[].path` references, moves companion files.

### Area 9: Housekeeping

**Files**: `.gitignore`

Add `eval/*/runs/` pattern for the default nested convention.

## File Structure Map

| File | Action | Responsibility |
|------|--------|----------------|
| `agent_eval/config.py` | Modify | Add `config_dir`, `DiscoveryResult`, `discover_configs()`, `resolve_convention()`, `save_convention()` |
| `agent_eval/migrate.py` | Create | `migrate_root_config()` for root-level config migration |
| `scripts/discover.py` | Create | CLI wrapper for `discover_configs()`, shared across all skills |
| `scripts/ensure_deps.py` | Modify | Update `_find_eval_yaml()` to use `discover_configs()` |
| `skills/eval-analyze/SKILL.md` | Modify | Convention selection, per-skill scaffolding, migration offer |
| `skills/eval-analyze/scripts/migrate.py` | Create | CLI wrapper for `migrate_root_config()` |
| `skills/eval-run/SKILL.md` | Modify | Auto-discovery integration, deprecation notices |
| `skills/eval-run/scripts/workspace.py` | Modify | Path resolution via `config_dir`, require `--config` |
| `skills/eval-run/scripts/preflight.py` | Modify | Require `--config`, per-skill runs dir |
| `skills/eval-run/scripts/score.py` | Modify | Path resolution, per-skill runs dir |
| `skills/eval-run/scripts/report.py` | Modify | Per-skill runs dir |
| `skills/eval-run/scripts/execute.py` | Modify | Require `--config` |
| `skills/eval-run/scripts/collect.py` | Modify | Path resolution via `config_dir`, require `--config` |
| `skills/eval-dataset/SKILL.md` | Modify | Auto-discovery integration |
| `skills/eval-optimize/SKILL.md` | Modify | Auto-discovery integration |
| `skills/eval-review/SKILL.md` | Modify | Auto-discovery integration |
| `skills/eval-mlflow/SKILL.md` | Modify | Auto-discovery integration |
| `skills/eval-mlflow/scripts/log_results.py` | Modify | Per-skill runs dir |
| `skills/eval-mlflow/scripts/attach_feedback.py` | Modify | Per-skill runs dir |
| `skills/eval-setup/scripts/check_env.py` | Modify | Report per-skill run directories |
| `.gitignore` | Modify | Add `eval/*/runs/`, `eval/.eval-convention` |
| `tests/test_config.py` | Modify | Path resolution tests |
| `tests/test_discovery.py` | Create | Discovery unit tests |
| `tests/test_convention.py` | Create | Convention persistence tests |
| `tests/test_run_isolation.py` | Create | Run isolation tests |
| `tests/test_migration.py` | Create | Migration tests |

## Dependency Order

```
Area 1 (path resolution)
  └── Area 2 (discovery)
       ├── Area 5 (eval-run integration)
       ├── Area 7 (other commands)
       └── Area 3 (convention persistence)
            └── Area 4 (eval-analyze scaffolding)
                 └── Area 8 (migration)
Area 6 (run isolation) ← depends on Area 1
Area 9 (gitignore) ← independent
```

## Test Strategy

### Unit Tests

- `test_config_path_resolution`: verify `config_dir` is set and paths resolve relative to it
- `test_discover_configs_nested`: place configs in `eval/*/eval.yaml`, verify discovery
- `test_discover_configs_flat`: place configs as `eval/*.yaml`, verify discovery
- `test_discover_configs_mixed`: mixed conventions discovered together
- `test_discover_configs_root_deprecated`: root config found with `is_deprecated=True`
- `test_discover_configs_empty`: no configs returns empty list
- `test_convention_persistence`: write and read back convention
- `test_run_dir_with_skill_name`: verify runs go to `$AGENT_EVAL_RUNS_DIR/<skill>/`
- `test_migration_nested`: migrate root config to nested convention
- `test_migration_flat`: migrate root config to flat convention
- `test_migration_path_fixup`: verify dataset.path and outputs[].path are rewritten

### E2E Tests

- Run `/eval-analyze --skill <test-skill>` in a multi-skill fixture, verify convention prompt and correct directory creation
- Run `/eval-run` without `--config` in a single-config project, verify auto-select
- Run `/eval-run` without `--config` in a multi-config project, verify prompt

## Risks

| Risk | Mitigation |
|------|------------|
| Breaking existing root-level configs | FR-017 ensures root configs keep working; deprecation is a warning, not an error |
| SKILL.md changes misinterpreted by LLM | Test via e2e runs; SKILL.md instructions are explicit with code snippets |
| Path resolution change breaks existing scripts | `config_dir` falls back to `Path.cwd()` when unset, preserving current behavior |
| Convention file committed accidentally | Add `eval/.eval-convention` to `.gitignore` (it's a local preference) |
