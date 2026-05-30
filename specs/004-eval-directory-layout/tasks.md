# Tasks: Flexible Eval Directory Layout

**Feature Branch**: `004-eval-directory-layout`
**Plan**: [plan.md](plan.md)
**Spec**: [spec.md](spec.md)

## Phase 1: Setup

- [ ] T001 Add `eval/*/runs/` and `eval/.eval-convention` to `.gitignore`

## Phase 2: Foundational (Path Resolution + Discovery)

These tasks are blocking prerequisites for all user stories.

- [ ] T002 Add `config_dir: Path` field to `EvalConfig` and set it from the config file's parent in `from_yaml()` in `agent_eval/config.py`
- [ ] T003 Update `project_root` property to return `config_dir` when available (fallback to `Path.cwd()`) in `agent_eval/config.py`
- [ ] T004 [P] Add `DiscoveryResult` dataclass (`path`, `skill_name`, `is_deprecated`) to `agent_eval/config.py`
- [ ] T005 Implement `discover_configs(project_root: Path) -> list[DiscoveryResult]` scanning `eval/*/eval.yaml`, `eval/*.yaml`, root `eval.yaml` in `agent_eval/config.py`
- [ ] T006 [P] Implement `resolve_convention(project_root: Path) -> str | None` reading `eval/.eval-convention` in `agent_eval/config.py`
- [ ] T007 [P] Implement `save_convention(project_root: Path, convention: str) -> None` writing `eval/.eval-convention` in `agent_eval/config.py`
- [ ] T008 Update `_find_eval_yaml()` in `scripts/ensure_deps.py` to use `discover_configs()` instead of hardcoded two-path check
- [ ] T009 Add unit tests for `config_dir` path resolution in `tests/test_config.py`
- [ ] T010 [P] Add unit tests for `discover_configs()` (nested, flat, mixed, root-deprecated, empty) in `tests/test_discovery.py`
- [ ] T011 [P] Add unit tests for `resolve_convention()` and `save_convention()` in `tests/test_convention.py`

## Phase 3: User Story 1 - Scaffold Eval Config with Layout Choice (P1)

**Goal**: `/eval-analyze` offers layout conventions during scaffolding and creates eval artifacts in the chosen location.

**Independent Test**: Run `/eval-analyze --skill <name>` in a fresh project, verify convention prompt appears, select default, confirm files created at `eval/<skill>/eval.yaml`.

- [ ] T012 [US1] Update Step 0 in `skills/eval-analyze/SKILL.md` to check for existing convention via `resolve_convention()` before scaffolding
- [ ] T013 [US1] Add layout convention selection step to `skills/eval-analyze/SKILL.md`: if no convention set, present nested (default) and flat options, save choice via `save_convention()`
- [ ] T014 [US1] Update Step 5 (generate eval.yaml) in `skills/eval-analyze/SKILL.md` to scaffold config at convention-appropriate path instead of project root
- [ ] T015 [US1] Update Step 5 in `skills/eval-analyze/SKILL.md` to create `eval.md` alongside config file at convention path
- [ ] T016 [US1] Update Step 4 in `skills/eval-analyze/SKILL.md` to create dataset directory relative to the config file location (`eval/<skill>/cases/` for nested, `eval/cases/<skill>/` for flat)
- [ ] T017 [US1] Update `--config` handling in `skills/eval-analyze/SKILL.md` to bypass convention selection and scaffold at the explicit path (FR-003)
- [ ] T018 [US1] Update Step 5b validate_eval.py invocation in `skills/eval-analyze/SKILL.md` to pass the new config path

## Phase 4: User Story 2 - Auto-Discover and Run Eval (P1)

**Goal**: `/eval-run` auto-discovers eval configs when `--config` is not provided.

**Independent Test**: Create eval configs in `eval/skill-a/` and `eval/skill-b/`, run `/eval-run` without `--config`, verify discovery and selection prompt.

- [ ] T019 [US2] Create shared discovery helper script `scripts/discover.py` that calls `discover_configs()` and prints results as JSON (shared location so all skills can invoke it)
- [ ] T020 [US2] Update `skills/eval-run/SKILL.md` to run discovery when `--config` is not provided: call `scripts/discover.py` via `${CLAUDE_SKILL_DIR}/../../scripts/discover.py`, auto-select or prompt
- [ ] T021 [US2] Remove `default="eval.yaml"` from `--config` argparse in `skills/eval-run/scripts/workspace.py`, make it required (SKILL.md always passes it after discovery)
- [ ] T022 [P] [US2] Update `--config` default in `skills/eval-run/scripts/preflight.py` to require explicit path
- [ ] T023 [P] [US2] Update `--config` default in `skills/eval-run/scripts/score.py` to require explicit path
- [ ] T024 [P] [US2] Update `--config` default in `skills/eval-run/scripts/report.py` to require explicit path
- [ ] T025 [P] [US2] Update `--config` default in `skills/eval-run/scripts/execute.py` to require explicit path
- [ ] T026 [P] [US2] Update `--config` default in `skills/eval-run/scripts/collect.py` to require explicit path

## Phase 5: User Story 5 - Dataset Path Resolution (P1)

**Goal**: Relative paths in eval.yaml resolve relative to the eval.yaml location.

**Independent Test**: Create eval config at `eval/my-skill/eval.yaml` with `dataset.path: cases/`, place test cases in `eval/my-skill/cases/`, verify they are found by workspace.py.

- [ ] T027 [US5] Update `workspace.py` to resolve `config.dataset_path` relative to `config.config_dir` instead of cwd in `skills/eval-run/scripts/workspace.py`
- [ ] T028 [US5] Update `score.py` to resolve `dataset_root` relative to `config.config_dir` in `skills/eval-run/scripts/score.py`
- [ ] T029 [P] [US5] Update output path resolution in `collect.py` to use `config.config_dir` in `skills/eval-run/scripts/collect.py`
- [ ] T030 [US5] Add unit test for path resolution with config in subdirectory in `tests/test_config.py`

## Phase 6: User Story 4 - Per-Skill Run Isolation (P2)

**Goal**: Each skill's run output is stored under `$AGENT_EVAL_RUNS_DIR/<skill-name>/`.

**Independent Test**: Run evaluations for two skills, verify runs appear in separate directories.

- [ ] T031 [US4] Update `_get_runs_dir()` in `skills/eval-run/scripts/score.py` to append `config.skill` to the base runs directory
- [ ] T032 [P] [US4] Update runs directory resolution in `skills/eval-run/scripts/report.py` to use `$AGENT_EVAL_RUNS_DIR/<skill>/`
- [ ] T033 [P] [US4] Update runs directory resolution in `skills/eval-run/scripts/preflight.py` to use `$AGENT_EVAL_RUNS_DIR/<skill>/`
- [ ] T034 [P] [US4] Update `log_results.py` in `skills/eval-mlflow/scripts/log_results.py` to resolve runs under `$AGENT_EVAL_RUNS_DIR/<skill>/`
- [ ] T035 [P] [US4] Update `attach_feedback.py` in `skills/eval-mlflow/scripts/attach_feedback.py` to resolve runs under `$AGENT_EVAL_RUNS_DIR/<skill>/`
- [ ] T036 [US4] Add unit test verifying runs directory includes skill name in `tests/test_run_isolation.py`

## Phase 7: User Story 3 - Migrate Root-Level Eval Config (P2)

**Goal**: Detect root-level `eval.yaml`, warn about deprecated location, offer migration.

**Independent Test**: Place `eval.yaml` at project root, run `/eval-analyze`, verify deprecation warning and migration offer.

- [ ] T037 [US3] Implement `migrate_root_config(project_root, convention, skill_name)` in new `agent_eval/migrate.py` per contracts/migration-api.md
- [ ] T038 [US3] Add root-level config detection to `skills/eval-analyze/SKILL.md`: check for `eval.yaml` at project root, display deprecation warning
- [ ] T039 [US3] Add migration offer to `skills/eval-analyze/SKILL.md`: ask user to accept/decline, call migrate script on acceptance
- [ ] T040 [US3] Create migration helper script `skills/eval-analyze/scripts/migrate.py` wrapping `migrate_root_config()` as CLI
- [ ] T041 [US3] Add unit tests for `migrate_root_config()` (nested target, flat target, path fixup, missing eval.md) in `tests/test_migration.py`

## Phase 8: SC-007 - Other Eval Commands Discovery

**Goal**: All eval commands (`/eval-dataset`, `/eval-optimize`, `/eval-review`, `/eval-mlflow`) use auto-discovery.

**Independent Test**: Run `/eval-dataset` without `--config` in a single-config project, verify auto-select.

- [ ] T042 [P] [SC7] Update `skills/eval-dataset/SKILL.md` to use shared `scripts/discover.py` for config discovery when `--config` not provided
- [ ] T043 [P] [SC7] Update `skills/eval-optimize/SKILL.md` to use shared `scripts/discover.py` for config discovery when `--config` not provided
- [ ] T044 [P] [SC7] Update `skills/eval-review/SKILL.md` to use shared `scripts/discover.py` for config discovery when `--config` not provided
- [ ] T045 [P] [SC7] Update `skills/eval-mlflow/SKILL.md` to use shared `scripts/discover.py` for config discovery when `--config` not provided

## Phase 9: Polish & Cross-Cutting Concerns

- [ ] T046 Update `skills/eval-setup/scripts/check_env.py` to report per-skill run directories when showing `AGENT_EVAL_RUNS_DIR` status
- [ ] T047 Update `skills/eval-run/SKILL.md` default config instructions to mention auto-discovery instead of defaulting to `eval.yaml`
- [ ] T048 Add deprecation notice for root-level configs in `skills/eval-run/SKILL.md` discovery output
- [ ] T049 [P] Handle skill names with special characters in `discover_configs()` and convention path generation: sanitize dots/underscores to valid directory names in `agent_eval/config.py`
- [ ] T050 [P] Handle flat convention filename collision (two skills producing same `eval/<name>.yaml`) in `discover_configs()` by warning on duplicates in `agent_eval/config.py`
- [ ] T051 Add unit tests for edge cases (special chars, flat name collision) in `tests/test_discovery.py`

## Dependencies

```
T002, T003 (path resolution) ← T027-T030 (US5), T031-T036 (US4)
T004, T005 (discovery) ← T019-T026 (US2), T042-T045 (US7)
T006, T007 (convention) ← T012-T018 (US1)
T005 (discovery) ← T008 (ensure_deps), T038 (US3 detection)
T012-T018 (US1 scaffolding) ← T037-T041 (US3 migration)
T001 (gitignore) ← none (independent)
```

**User Story Independence**: US1 (scaffolding), US2 (discovery), US4 (run isolation), and US5 (path resolution) can proceed in parallel after Phase 2 completes. US3 (migration) depends on US1 scaffolding being done. US7 (other commands) depends on US2 discovery.

## Parallel Execution Opportunities

**Phase 2**: T004+T006+T007 are independent of each other, all parallelizable after T002+T003. T009+T010+T011 (tests) are all parallelizable.

**Phase 4**: T022+T023+T024+T025+T026 are all parallelizable (independent script updates).

**Phase 6**: T032+T033+T034+T035 are all parallelizable (independent script updates).

**Phase 8**: T042+T043+T044+T045 are all parallelizable (independent SKILL.md updates).

## Implementation Strategy

**MVP (minimum viable)**: Phase 1 + Phase 2 + Phase 3 (US1). This gives users the ability to scaffold eval configs at per-skill locations with convention choice. Enough to validate the layout approach.

**Full P1**: Add Phase 4 (US2) + Phase 5 (US5). This adds auto-discovery and correct path resolution, making the new layout transparent to daily usage.

**Complete**: Add Phase 6 (US4) + Phase 7 (US3) + Phase 8 (US7) + Phase 9. Full run isolation, migration for existing users, and all commands updated.
