# Contract: Migration API

Internal Python API for migrating root-level eval configs to a layout convention.

## `migrate_root_config(project_root: Path, convention: str, skill_name: str) -> MigrationResult`

Moves a root-level `eval.yaml` and its companion artifacts to the target convention layout.

### Parameters

- `project_root`: project root directory (where root-level `eval.yaml` lives)
- `convention`: target convention (`"nested"` or `"flat"`)
- `skill_name`: skill name (read from `eval.yaml`'s `skill` field)

### Behavior

1. Computes target paths based on convention:
   - **nested**: config to `eval/<skill>/eval.yaml`, dataset to `eval/<skill>/cases/`
   - **flat**: config to `eval/<skill>.yaml`, dataset to `eval/cases/<skill>/`

2. Moves files:
   - `eval.yaml` to target config path
   - `eval.md` to alongside new config path
   - `eval/cases/` (or whatever `dataset.path` pointed to) to target dataset path
   - Run history: not moved (runs stay under `$AGENT_EVAL_RUNS_DIR` which already uses `<skill>/` namespacing)

3. Updates internal paths in the moved eval.yaml:
   - `dataset.path`: rewritten relative to new config location
   - `outputs[].path`: rewritten relative to new config location

4. Returns a `MigrationResult` with moved files and any warnings.

### Error Handling

- If target path already exists: abort with error (don't overwrite)
- If source files are missing (e.g., no eval.md): warn but continue with what exists
- If eval.yaml has no `skill` field: abort (can't determine target path)
