# Implementation Plan: Flexible Eval Directory Layout

**Branch**: `005-eval-directory-layout`
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

Nine work areas, ordered by dependency:

### Area 1: Path Resolution Foundation (FR-011)

**Files**: `agent_eval/config.py`

Add `config_dir: Optional[Path]` field to `EvalConfig`. Set it in `from_yaml()` from the config file's parent directory. Used as base for resolving `dataset.path`. `project_root` remains unchanged (returns `Path.cwd()`) since it serves repo-level concerns (symlinks, judge modules, settings).

This is the foundation: all other changes depend on `dataset.path` resolving correctly relative to the config file.

### Area 2: Auto-Discovery (FR-007 to FR-010)

**Files**: `agent_eval/config.py` (new `discover_configs` function), `scripts/ensure_deps.py` (update `_find_eval_yaml`)

Implement `discover_configs(project_root)` per [discovery-api.md](contracts/discovery-api.md). Scans `eval/*/eval.yaml`, `eval/*.yaml`, root `eval.yaml`. Returns list of `DiscoveryResult` with path, eval name, root flag.

Update `_find_eval_yaml()` in `ensure_deps.py` to use the same discovery logic.

### Area 3: Layout Inference (FR-006)

Layout is inferred from the discovery results, not persisted. If `discover_configs()` finds configs under `eval/*/eval.yaml`, the project uses nested layout. No separate persistence file needed.

### Area 4: Smart Scaffolding (FR-001 to FR-005)

**Files**: `skills/eval-analyze/SKILL.md`

Update the skill instructions to:
1. Check how many eval configs exist via `discover_configs()`
2. If no configs exist: scaffold `eval.yaml` at project root (simple default)
3. If root config exists and a new eval target is requested: offer to reorganize into `eval/` layout
4. Support `--config <path>` to bypass layout selection
5. Create `eval.md` alongside the config

This is primarily SKILL.md instruction changes, not Python code. The LLM follows the instructions to create directories and files.

### Area 5: Eval-Run Discovery Integration (FR-007 to FR-010)

**Files**: `skills/eval-run/SKILL.md`, `skills/eval-run/scripts/workspace.py`, `skills/eval-run/scripts/preflight.py`

Update eval-run SKILL.md to:
1. If `--config` not provided: call `discover_configs()` via a helper script
2. Auto-select or prompt based on result count
3. Pass resolved config path to downstream scripts

Update `workspace.py` default: remove `default="eval.yaml"` from `--config` argparse, require it explicitly (the SKILL.md will always pass it after discovery).

Update `preflight.py` to resolve runs directory using `AGENT_EVAL_RUNS_DIR/<eval-name>/`.

### Area 6: Run Isolation (FR-012)

**Files**: `skills/eval-run/scripts/score.py`, `skills/eval-run/scripts/report.py`, `skills/eval-mlflow/scripts/log_results.py`, `skills/eval-mlflow/scripts/attach_feedback.py`

Update all scripts that read `AGENT_EVAL_RUNS_DIR` to append the eval name: `runs_dir / config.skill`. The eval name comes from the `EvalConfig.skill` field loaded from eval.yaml.

### Area 7: Other Eval Commands (FR-007, SC-007)

**Files**: `skills/eval-dataset/SKILL.md`, `skills/eval-optimize/SKILL.md`, `skills/eval-review/SKILL.md`, `skills/eval-mlflow/SKILL.md`

Update each SKILL.md to use the same discovery pattern as eval-run: if `--config` not provided, discover and select. These are lighter touches since they share the same discovery function.

### Area 8: Reorganization (FR-014 to FR-017)

**Files**: New `agent_eval/reorganize.py`

Implement reorganization logic per [migration-api.md](contracts/migration-api.md). Called from eval-analyze SKILL.md when a root-level config exists and a second eval is being added. Updates `dataset.path` references, moves companion files. `outputs[].path` is NOT rewritten (workspace-relative).

### Area 9: Housekeeping

**Files**: `.gitignore`

Add `eval/runs/` pattern.

## File Structure Map

| File | Action | Responsibility |
|------|--------|----------------|
| `agent_eval/config.py` | Modify | Add `config_dir`, `DiscoveryResult`, `discover_configs()` |
| `agent_eval/reorganize.py` | Create | `reorganize_root_config()` for root-level config reorganization |
| `scripts/discover.py` | Create | CLI wrapper for `discover_configs()`, shared across all skills |
| `scripts/ensure_deps.py` | Modify | Update `_find_eval_yaml()` to use `discover_configs()` |
| `skills/eval-analyze/SKILL.md` | Modify | Smart scaffolding, reorganization offer |
| `skills/eval-analyze/scripts/reorganize.py` | Create | CLI wrapper for `reorganize_root_config()` |
| `skills/eval-run/SKILL.md` | Modify | Auto-discovery integration |
| `skills/eval-run/scripts/workspace.py` | Modify | Require `--config` explicitly |
| `skills/eval-run/scripts/preflight.py` | Modify | Require `--config`, per-eval runs dir |
| `skills/eval-run/scripts/score.py` | Modify | Per-eval runs dir |
| `skills/eval-run/scripts/report.py` | Modify | Per-eval runs dir |
| `skills/eval-run/scripts/execute.py` | Modify | Require `--config` |
| `skills/eval-run/scripts/collect.py` | Modify | Require `--config` (outputs[].path stays workspace-relative) |
| `skills/eval-dataset/SKILL.md` | Modify | Auto-discovery integration |
| `skills/eval-optimize/SKILL.md` | Modify | Auto-discovery integration |
| `skills/eval-review/SKILL.md` | Modify | Auto-discovery integration |
| `skills/eval-mlflow/SKILL.md` | Modify | Auto-discovery integration |
| `skills/eval-mlflow/scripts/log_results.py` | Modify | Per-eval runs dir |
| `skills/eval-mlflow/scripts/attach_feedback.py` | Modify | Per-eval runs dir |
| `skills/eval-setup/scripts/check_env.py` | Modify | Report per-eval run directories |
| `.gitignore` | Modify | Add `eval/runs/` |
| `tests/test_config.py` | Modify | Path resolution tests |
| `tests/test_discovery.py` | Create | Discovery unit tests |
| `tests/test_layout.py` | Create | Layout inference tests |
| `tests/test_run_isolation.py` | Create | Run isolation tests |
| `tests/test_reorganization.py` | Create | Reorganization tests |

## Dependency Order

```
Area 1 (path resolution)
  +-- Area 2 (discovery)
  |    +-- Area 5 (eval-run integration)
  |    +-- Area 7 (other commands)
  |    +-- Area 3 (layout inference)
  |         +-- Area 4 (smart scaffolding)
  |              +-- Area 8 (reorganization)
  +-- Area 6 (run isolation)
Area 9 (gitignore) -- independent
```

## Test Strategy

### Unit Tests

- `test_config_dir_resolution`: verify `config_dir` is set and `dataset.path` resolves relative to it
- `test_config_dir_none_fallback`: verify `Path.cwd()` fallback when `config_dir` is `None`
- `test_discover_configs_nested`: place configs in `eval/*/eval.yaml`, verify discovery
- `test_discover_configs_flat`: place configs as `eval/*.yaml`, verify discovery
- `test_discover_configs_root`: root config found with `is_root=True`
- `test_discover_configs_mixed`: mixed layouts discovered together
- `test_discover_configs_empty`: no configs returns empty list
- `test_layout_inference`: infer layout from existing file structure
- `test_run_dir_with_eval_name`: verify runs go to `$AGENT_EVAL_RUNS_DIR/<eval-name>/`
- `test_reorganize_nested`: reorganize root config into nested layout
- `test_reorganize_path_fixup`: verify `dataset.path` is rewritten and `outputs[].path` is preserved
- `test_shared_dataset`: two configs pointing to same dataset directory

### E2E Tests

- Run `/eval-analyze` in a fresh project, verify root-level config creation (no layout prompt)
- Run `/eval-analyze` again for a different target, verify reorganization offer
- Run `/eval-run` without `--config` in a single-config project, verify auto-select
- Run `/eval-run` without `--config` in a multi-config project, verify prompt

## Risks

| Risk | Mitigation |
|------|------------|
| Breaking existing root-level configs | FR-013/FR-017: root configs are first-class, no deprecation |
| SKILL.md changes misinterpreted by LLM | Test via e2e runs; SKILL.md instructions are explicit with code snippets |
| Path resolution change breaks existing scripts | `config_dir` falls back to `Path.cwd()` when unset, preserving current behavior |
| Layout inference wrong for edge cases | Discovery scans concrete patterns; ambiguity only in mixed layouts (acceptable) |
