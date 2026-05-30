# Tasks: Flexible Eval Directory Layout

**Feature Branch**: `005-eval-directory-layout`
**Plan**: [plan.md](plan.md)
**Spec**: [spec.md](spec.md)

## Phase 1: Setup

- [ ] T001 Add `eval/runs/` to `.gitignore`

## Phase 2: Foundational (Path Resolution + Discovery)

These tasks are blocking prerequisites for all user stories.

- [ ] T002 Add `config_dir: Optional[Path]` field to `EvalConfig` (default `None`) and set it from the config file's parent in `from_yaml()` in `agent_eval/config.py`
- [ ] T003 Add `resolve_path(relative: Path) -> Path` method to `EvalConfig` that resolves against `config_dir` (fallback to `Path.cwd()`), passing through absolute paths as-is, in `agent_eval/config.py`. Update `_validate_relative_path` to allow absolute `dataset.path` (FR-011 requires it). `project_root` remains unchanged.
- [ ] T004 [P] Add `DiscoveryResult` dataclass (`path`, `eval_name`, `is_root`) to `agent_eval/config.py`
- [ ] T005 Implement `discover_configs(project_root: Path) -> list[DiscoveryResult]` scanning `eval/*/eval.yaml`, `eval/*.yaml`, root `eval.yaml` in `agent_eval/config.py`
- [ ] T006 [P] Implement `infer_layout(configs: list[DiscoveryResult]) -> str` that returns `"nested"` if configs exist under `eval/`, `"root"` if only root config, `"none"` if empty, in `agent_eval/config.py`
- [ ] T008 Update `_find_eval_yaml()` in `scripts/ensure_deps.py` to use `discover_configs()` instead of hardcoded two-path check
- [ ] T009 Add unit tests for `config_dir` path resolution and `resolve_path()` in `tests/test_config.py`
- [ ] T010 [P] Add unit tests for `discover_configs()` (nested, flat, root, mixed, empty) in `tests/test_discovery.py`
- [ ] T011 [P] Add unit tests for `infer_layout()` in `tests/test_layout.py`

**Checkpoint**: Foundation ready. All user stories can now proceed.

---

## Phase 3: User Story 1 - Smart Scaffolding (Priority: P1)

**Goal**: `/eval-analyze` adapts scaffolding behavior based on project complexity. First eval goes to root, second eval triggers layout organization.

**Independent Test**: Run `/eval-analyze` in a fresh project, verify root-level config. Run again for a different target, verify reorganization offer.

- [ ] T012 [US1] Update Step 0 in `skills/eval-analyze/SKILL.md` to call `discover_configs()` before scaffolding to detect existing configs
- [ ] T013 [US1] Update scaffolding in `skills/eval-analyze/SKILL.md`: if no configs exist, create `eval.yaml` at project root (FR-001)
- [ ] T014 [US1] Update scaffolding in `skills/eval-analyze/SKILL.md`: if root config exists and new target requested, offer to organize into `eval/` layout (FR-002)
- [ ] T015 [US1] Update scaffolding in `skills/eval-analyze/SKILL.md`: when organizing, use `infer_layout()` to detect current state before placing new config (FR-006)
- [ ] T016 [US1] Update `--config` handling in `skills/eval-analyze/SKILL.md` to bypass layout selection and scaffold at the explicit path (FR-003)
- [ ] T017 [US1] Update `skills/eval-analyze/SKILL.md` to create `eval.md` alongside the config file at whichever location was chosen (FR-004)
- [ ] T018 [US1] Update Step 5b validate_eval.py invocation in `skills/eval-analyze/SKILL.md` to pass the resolved config path

**Checkpoint**: Smart scaffolding works for both single-eval and multi-eval projects.

---

## Phase 4: User Story 2 - Auto-Discover and Run Eval (Priority: P1)

**Goal**: `/eval-run` auto-discovers eval configs when `--config` is not provided.

**Independent Test**: Create eval configs in `eval/eval-a/` and `eval/eval-b/`, run `/eval-run` without `--config`, verify discovery and selection prompt.

- [ ] T019 [US2] Create shared discovery helper script `scripts/discover.py` that calls `discover_configs()` and prints results as JSON
- [ ] T020 [US2] Update `skills/eval-run/SKILL.md` to run discovery when `--config` is not provided: call `scripts/discover.py` via `${CLAUDE_SKILL_DIR}/../../scripts/discover.py`, auto-select or prompt
- [ ] T021 [US2] Remove `default="eval.yaml"` from `--config` argparse in `skills/eval-run/scripts/workspace.py`, make it required
- [ ] T022 [P] [US2] Update `--config` default in `skills/eval-run/scripts/preflight.py` to require explicit path
- [ ] T023 [P] [US2] Update `--config` default in `skills/eval-run/scripts/score.py` to require explicit path
- [ ] T024 [P] [US2] Update `--config` default in `skills/eval-run/scripts/report.py` to require explicit path
- [ ] T025 [P] [US2] Update `--config` default in `skills/eval-run/scripts/execute.py` to require explicit path
- [ ] T026 [P] [US2] Update `--config` default in `skills/eval-run/scripts/collect.py` to require explicit path

**Checkpoint**: `/eval-run` works without `--config` for single-config and multi-config projects.

---

## Phase 5: User Story 5 - Dataset Path Resolution (Priority: P1)

**Goal**: `dataset.path` in eval.yaml resolves relative to the eval.yaml location.

**Independent Test**: Create eval config at `eval/my-eval/eval.yaml` with `dataset.path: cases/`, place test cases in `eval/my-eval/cases/`, verify they are found by workspace.py.

- [ ] T027 [US5] Update `workspace.py` to resolve `config.dataset_path` using `config.resolve_path()` instead of cwd in `skills/eval-run/scripts/workspace.py`
- [ ] T028 [US5] Update `score.py` to resolve `dataset_root` using `config.resolve_path()` in `skills/eval-run/scripts/score.py`
- [ ] T029 [US5] Verify output path resolution in `collect.py` remains workspace-relative (no change needed, `outputs[].path` is already workspace-relative) in `skills/eval-run/scripts/collect.py`
- [ ] T030 [US5] Add unit test for path resolution with config in subdirectory in `tests/test_config.py`

**Checkpoint**: Dataset paths resolve correctly regardless of eval.yaml location.

---

## Phase 6: User Story 4 - Per-Eval Run Isolation (Priority: P2)

**Goal**: Each eval target's run output is stored under `$AGENT_EVAL_RUNS_DIR/<eval-name>/`.

**Independent Test**: Run evaluations for two eval targets, verify runs appear in separate directories.

- [ ] T031 [US4] Update `_get_runs_dir()` in `skills/eval-run/scripts/score.py` to append `config.skill` to the base runs directory
- [ ] T032 [P] [US4] Update runs directory resolution in `skills/eval-run/scripts/report.py` to use `$AGENT_EVAL_RUNS_DIR/<eval-name>/`
- [ ] T033 [P] [US4] Update runs directory resolution in `skills/eval-run/scripts/preflight.py` to use `$AGENT_EVAL_RUNS_DIR/<eval-name>/`
- [ ] T034 [P] [US4] Update `log_results.py` in `skills/eval-mlflow/scripts/log_results.py` to resolve runs under `$AGENT_EVAL_RUNS_DIR/<eval-name>/`
- [ ] T035 [P] [US4] Update `attach_feedback.py` in `skills/eval-mlflow/scripts/attach_feedback.py` to resolve runs under `$AGENT_EVAL_RUNS_DIR/<eval-name>/`
- [ ] T036 [US4] Add unit test verifying runs directory includes eval name in `tests/test_run_isolation.py`

**Checkpoint**: Run results are isolated per eval target.

---

## Phase 7: User Story 3 - Organize When Outgrowing Root Layout (Priority: P2)

**Goal**: Reorganize root-level config into `eval/` when adding a second eval target.

**Independent Test**: Start with root-level `eval.yaml`, run `/eval-analyze` for a second target, verify reorganization offer and correct file moves.

- [ ] T037 [US3] Implement `reorganize_root_config(project_root, eval_name)` in new `agent_eval/reorganize.py` per contracts/migration-api.md
- [ ] T038 [US3] Create CLI wrapper script `skills/eval-analyze/scripts/reorganize.py` wrapping `reorganize_root_config()`
- [ ] T039 [US3] Integrate reorganization into `skills/eval-analyze/SKILL.md`: when root config detected and new target requested, call reorganize script on acceptance
- [ ] T040 [US3] Add unit tests for `reorganize_root_config()` (path fixup, missing eval.md, dataset move) in `tests/test_reorganization.py`

**Checkpoint**: Projects can smoothly transition from single-eval root layout to multi-eval `eval/` layout.

---

## Phase 8: User Story 6 - Shared Datasets (Priority: P3)

**Goal**: Validate that datasets can be shared across multiple eval configs.

**Independent Test**: Create two eval configs each pointing `dataset.path` to a shared directory. Run both evals and verify they use the same test cases.

- [ ] T041 [US6] Add unit test verifying two configs with shared `dataset.path` resolve to the same directory in `tests/test_config.py`
- [ ] T042 [US6] Add unit test verifying absolute `dataset.path` is used as-is in `tests/test_config.py`

**Checkpoint**: Shared datasets work across eval configs.

---

## Phase 9: SC-007 - Other Eval Commands Discovery

**Goal**: All eval commands (`/eval-dataset`, `/eval-optimize`, `/eval-review`, `/eval-mlflow`) use auto-discovery.

**Independent Test**: Run `/eval-dataset` without `--config` in a single-config project, verify auto-select.

- [ ] T043 [P] Update `skills/eval-dataset/SKILL.md` to use shared `scripts/discover.py` for config discovery when `--config` not provided
- [ ] T044 [P] Update `skills/eval-optimize/SKILL.md` to use shared `scripts/discover.py` for config discovery when `--config` not provided
- [ ] T045 [P] Update `skills/eval-review/SKILL.md` to use shared `scripts/discover.py` for config discovery when `--config` not provided
- [ ] T046 [P] Update `skills/eval-mlflow/SKILL.md` to use shared `scripts/discover.py` for config discovery when `--config` not provided

---

## Phase 10: Polish & Cross-Cutting Concerns

- [ ] T047 Update `skills/eval-setup/scripts/check_env.py` to report per-eval run directories when showing `AGENT_EVAL_RUNS_DIR` status
- [ ] T048 Update `skills/eval-run/SKILL.md` default config instructions to mention auto-discovery instead of defaulting to `eval.yaml`
- [ ] T049 [P] Handle eval names with special characters in `discover_configs()`: validate as single path segment (no separators, `..`, control chars) in `agent_eval/config.py`
- [ ] T050 [P] Handle flat layout filename collision (two evals producing same `eval/<name>.yaml`) in `discover_configs()` by warning on duplicates in `agent_eval/config.py`
- [ ] T051 Add unit tests for edge cases (special chars, flat name collision) in `tests/test_discovery.py`

## Dependencies

```text
T027-T030 (US5), T031-T036 (US4) depend on T002, T003 (path resolution)
T019-T026 (US2), T043-T046 (SC-007) depend on T004, T005 (discovery)
T012-T018 (US1) depend on T006 (layout inference)
T008 (ensure_deps), T012 (US1 detection) depend on T005 (discovery)
T037-T040 (US3 reorganization) depend on T012-T018 (US1 scaffolding)
T001 (gitignore) has no dependencies
```

**User Story Independence**: US1 (scaffolding), US2 (discovery), US4 (run isolation), and US5 (path resolution) can proceed in parallel after Phase 2 completes. US3 (reorganization) depends on US1 scaffolding being done. US6 (shared datasets) depends on US5 path resolution. SC-007 (other commands) depends on US2 discovery.

## Parallel Execution Opportunities

**Phase 2**: T004+T006 are independent of each other, all parallelizable after T002+T003. T009+T010+T011 (tests) are all parallelizable.

**Phase 4**: T022+T023+T024+T025+T026 are all parallelizable (independent script updates).

**Phase 6**: T032+T033+T034+T035 are all parallelizable (independent script updates).

**Phase 9**: T043+T044+T045+T046 are all parallelizable (independent SKILL.md updates).

## Implementation Strategy

**MVP (minimum viable)**: Phase 1 + Phase 2 + Phase 3 (US1). Smart scaffolding for both single-eval and multi-eval projects. Enough to validate the layout approach.

**Full P1**: Add Phase 4 (US2) + Phase 5 (US5). Auto-discovery and correct path resolution, making the layout transparent to daily usage.

**Complete**: Add Phase 6 (US4) + Phase 7 (US3) + Phase 8 (US6) + Phase 9 (SC-007) + Phase 10. Full run isolation, reorganization for growing projects, shared dataset validation, and all commands updated.
