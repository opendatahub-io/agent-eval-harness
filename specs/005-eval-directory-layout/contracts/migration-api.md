# Contract: Reorganization API

Internal Python API for reorganizing root-level eval configs into an `eval/` directory layout.

## `reorganize_root_config(project_root: Path, eval_name: str) -> ReorganizationResult`

Moves a root-level `eval.yaml` and its companion artifacts into the nested layout under `eval/`.

### Parameters

- `project_root`: project root directory (where root-level `eval.yaml` lives)
- `eval_name`: eval name (read from `eval.yaml`'s `skill` field)

### Behavior

1. Computes target paths:
   - Config to `eval/<eval_name>/eval.yaml`
   - `eval.md` to `eval/<eval_name>/eval.md`

2. Moves files:
   - `eval.yaml` to target config path
   - `eval.md` to alongside new config path
   - Dataset directory (whatever `dataset.path` pointed to) to target location if co-located with config

3. Updates internal paths in the moved eval.yaml:
   - `dataset.path`: rewritten relative to new config location
   - `outputs[].path`: NOT rewritten (workspace-relative, not config-relative)

4. Saves layout persistence:
   - Writes `nested` to `eval/.eval-layout`

5. Returns a `ReorganizationResult` with moved files and any warnings.

### Error Handling

- If target path already exists: abort with error (don't overwrite)
- If source files are missing (e.g., no eval.md): warn but continue with what exists
- If eval.yaml has no `skill` field: abort (can't determine target path)
