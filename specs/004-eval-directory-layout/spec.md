# Feature Specification: Per-Skill Eval Directory Layout

**Feature Branch**: `004-eval-directory-layout`
**Created**: 2026-05-28
**Status**: Draft
**Input**: User description: "Per-skill eval directory layout for multi-skill plugin projects"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Analyze a Skill in a Multi-Skill Project (Priority: P1)

A skill author runs `/eval-analyze --skill eval-run` in a plugin project containing six skills. The system creates `eval/eval-run/eval.yaml` and `eval/eval-run/eval.md` instead of placing files at the project root. The author can then run `/eval-dataset` and `/eval-run` against that specific skill without config collisions.

**Why this priority**: This is the entry point for the entire eval workflow. Without per-skill directory creation, no downstream feature works correctly for multi-skill projects.

**Independent Test**: Can be fully tested by running `/eval-analyze --skill <name>` and verifying the eval config lands in `eval/<skill-name>/eval.yaml` with correct relative paths.

**Acceptance Scenarios**:

1. **Given** a plugin project with multiple skills, **When** the user runs `/eval-analyze --skill eval-run`, **Then** the system creates `eval/eval-run/eval.yaml` and `eval/eval-run/eval.md`
2. **Given** a plugin project with multiple skills, **When** the user runs `/eval-analyze --skill eval-dataset`, **Then** the system creates `eval/eval-dataset/eval.yaml` separately from any existing `eval/eval-run/` directory
3. **Given** a single-skill project, **When** the user runs `/eval-analyze --skill my-skill`, **Then** the system creates `eval/my-skill/eval.yaml` following the same convention

---

### User Story 2 - Auto-Discover and Run Eval for a Skill (Priority: P1)

A skill author runs `/eval-run` without specifying `--config`. The system scans `eval/*/eval.yaml`, finds one or more configs, and either auto-selects (when only one exists) or prompts the user to choose which skill to evaluate.

**Why this priority**: Developers should not need to remember or type config paths for the common case. Auto-discovery makes the new directory layout transparent.

**Independent Test**: Can be tested by creating eval configs in `eval/skill-a/` and `eval/skill-b/`, then running `/eval-run` without `--config` and verifying the discovery and selection behavior.

**Acceptance Scenarios**:

1. **Given** exactly one `eval/*/eval.yaml` exists, **When** the user runs `/eval-run` without `--config`, **Then** the system auto-selects that config and proceeds
2. **Given** multiple `eval/*/eval.yaml` files exist, **When** the user runs `/eval-run` without `--config`, **Then** the system lists available skills and prompts the user to select one
3. **Given** no `eval/*/eval.yaml` exists, **When** the user runs `/eval-run` without `--config`, **Then** the system reports that no eval config was found and suggests running `/eval-analyze` first
4. **Given** the user provides `--config path/to/custom.yaml`, **When** the user runs `/eval-run`, **Then** the system uses that explicit path and skips auto-discovery

---

### User Story 3 - Migrate Root-Level Eval Config (Priority: P2)

A skill author has an existing project with `eval.yaml` at the project root (created before the per-skill layout). When they run `/eval-analyze`, the system detects the root-level config, warns about the deprecated location, and offers to migrate it to `eval/<skill-name>/eval.yaml`.

**Why this priority**: Backward compatibility matters for existing users, but this is a one-time transition, not a daily workflow.

**Independent Test**: Can be tested by placing an `eval.yaml` at the project root, running `/eval-analyze`, and verifying the deprecation warning appears with a migration offer.

**Acceptance Scenarios**:

1. **Given** `eval.yaml` exists at the project root, **When** the user runs `/eval-analyze`, **Then** the system displays a deprecation warning suggesting migration to `eval/<skill-name>/`
2. **Given** `eval.yaml` exists at the project root, **When** the user accepts the migration offer, **Then** the system moves `eval.yaml`, `eval.md`, `eval/cases/`, and `eval/runs/` to the new per-skill directory
3. **Given** `eval.yaml` exists at the project root, **When** the user declines the migration, **Then** the system continues operating with the root-level config without errors
4. **Given** `eval.yaml` exists at the project root, **When** the user runs `/eval-run` without `--config`, **Then** the system finds the root-level config but displays a deprecation notice

---

### User Story 4 - Per-Skill Run Isolation (Priority: P2)

A skill author runs evaluations for two different skills. Each skill's run output is stored in its own directory (`eval/<skill>/runs/`), keeping results separate and preventing cross-contamination.

**Why this priority**: Run isolation prevents confusion when comparing results across skills, but the system still functions if results are mixed (just harder to navigate).

**Independent Test**: Can be tested by running evaluations for two skills and verifying each skill's runs appear only in its respective `eval/<skill>/runs/` directory.

**Acceptance Scenarios**:

1. **Given** eval configs exist for `skill-a` and `skill-b`, **When** the user runs evaluations for both, **Then** results are stored in `eval/skill-a/runs/` and `eval/skill-b/runs/` respectively
2. **Given** eval configs exist for a skill, **When** the user runs multiple evaluations, **Then** each run creates a new timestamped directory under `eval/<skill>/runs/`

---

### User Story 5 - Dataset Path Resolution (Priority: P1)

A skill author edits `eval/<skill>/eval.yaml` and sets `dataset.path: cases/`. The system resolves this path relative to the eval.yaml location (i.e., `eval/<skill>/cases/`), not relative to the project root.

**Why this priority**: Incorrect path resolution would break every eval run. This is a foundational behavior that other features depend on.

**Independent Test**: Can be tested by creating `eval/my-skill/eval.yaml` with `dataset.path: cases/`, placing test cases in `eval/my-skill/cases/`, and verifying they are found.

**Acceptance Scenarios**:

1. **Given** `eval/<skill>/eval.yaml` contains `dataset.path: cases/`, **When** the system resolves the dataset path, **Then** it resolves to `eval/<skill>/cases/` (relative to eval.yaml)
2. **Given** `eval/<skill>/eval.yaml` contains an absolute dataset path, **When** the system resolves the dataset path, **Then** it uses the absolute path as-is

---

### Edge Cases

- What happens when a skill name contains special characters (e.g., dots, underscores)?
- How does the system behave when `eval/` directory exists but contains non-eval subdirectories?
- What happens if two skills have the same name (from different sources)?
- How does `--config` interact with auto-discovery (explicit config should always win)?
- What happens when root-level `eval.yaml` references `eval/cases/` and the migration moves it (path fixup needed)?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `/eval-analyze` MUST create eval configs at `eval/<skill-name>/eval.yaml` by default, where `<skill-name>` comes from the skill's directory name
- **FR-002**: `/eval-analyze` MUST also create `eval/<skill-name>/eval.md` alongside the config
- **FR-003**: `/eval-analyze` MUST create the dataset directory at `eval/<skill-name>/cases/` when generating the initial config
- **FR-004**: `/eval-run` MUST auto-discover eval configs by scanning `eval/*/eval.yaml` when `--config` is not provided
- **FR-005**: `/eval-run` MUST auto-select the config when exactly one `eval/*/eval.yaml` exists
- **FR-006**: `/eval-run` MUST prompt for skill selection when multiple `eval/*/eval.yaml` files exist
- **FR-007**: The `--config` flag MUST override auto-discovery and use the specified path directly
- **FR-008**: All relative paths in `eval.yaml` (dataset path, output paths) MUST resolve relative to the eval.yaml file location, not the project root
- **FR-009**: Run results MUST be stored in `eval/<skill-name>/runs/` instead of the shared `eval/runs/` directory
- **FR-010**: The system MUST display a deprecation warning when a root-level `eval.yaml` is detected
- **FR-011**: The system MUST offer to migrate root-level eval artifacts to the per-skill directory structure
- **FR-012**: Migration MUST move `eval.yaml`, `eval.md`, dataset files, and run history to the new location
- **FR-013**: Migration MUST update internal path references within `eval.yaml` to reflect the new location
- **FR-014**: The system MUST continue to operate with root-level `eval.yaml` if the user declines migration
- **FR-015**: A single `.gitignore` pattern (`eval/*/runs/`) MUST cover all per-skill run output

### Key Entities

- **Eval Config**: The `eval.yaml` file containing skill evaluation configuration (dataset path, judges, thresholds, execution settings). Now located at `eval/<skill-name>/eval.yaml` instead of project root.
- **Eval Directory**: A per-skill directory under `eval/` containing all eval artifacts for one skill: config, documentation, test cases, and run results.
- **Dataset**: Test cases for evaluation, stored at `eval/<skill-name>/cases/`. Path resolved relative to eval.yaml location.
- **Run Output**: Timestamped evaluation results, stored at `eval/<skill-name>/runs/`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Skill authors can set up and run evaluations for any skill in a multi-skill project without config file conflicts
- **SC-002**: New eval configs are created in the per-skill directory on the first attempt, without manual directory creation
- **SC-003**: Existing projects with root-level eval.yaml can migrate to the new layout in a single step
- **SC-004**: Users can run evaluations without specifying config paths for the common case (single-skill or prompted multi-skill)
- **SC-005**: Run results for different skills never appear in the same output directory
- **SC-006**: All eval-related commands (`/eval-analyze`, `/eval-run`, `/eval-dataset`, `/eval-optimize`, `/eval-review`, `/eval-mlflow`) work correctly with the new directory structure

## Assumptions

- Skill names are unique within a plugin project (derived from skill directory names)
- The `eval/` directory is reserved for evaluation artifacts; other project content does not reside there
- Existing root-level `eval.yaml` files reference a single skill (the project's primary or only skill)
- The `eval/runs/` path in the current `AGENT_EVAL_RUNS_DIR` environment variable will be updated to point to per-skill run directories
- Users are comfortable with one additional level of directory nesting (`eval/<skill>/` vs. root level)
- PR #74 (harness-level context) will compose with this layout, with each `eval/<skill>/eval.yaml` carrying its own `harness_context`
