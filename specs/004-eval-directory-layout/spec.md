# Feature Specification: Flexible Eval Directory Layout

**Feature Branch**: `004-eval-directory-layout`
**Created**: 2026-05-28
**Updated**: 2026-05-29
**Status**: Draft
**Input**: Flexible eval directory layout for multi-skill plugin projects. Offer layout conventions during scaffolding, with smart auto-discovery that adapts to whichever layout the user chose.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Scaffold Eval Config with Layout Choice (Priority: P1)

A skill author runs `/eval-analyze --skill eval-run` in a plugin project containing six skills. The system asks which layout convention they prefer, offering per-skill nested (`eval/eval-run/eval.yaml`) as the recommended default. The author picks the default. The system creates the config in the chosen location with correct relative paths for datasets and run output.

**Why this priority**: This is the entry point for the entire eval workflow. The layout choice happens here and determines where all downstream artifacts land.

**Independent Test**: Run `/eval-analyze --skill <name>` in a fresh project, verify the layout choice prompt appears, select the default, and confirm files are created at `eval/<skill-name>/eval.yaml`.

**Acceptance Scenarios**:

1. **Given** a plugin project with multiple skills and no existing eval config, **When** the user runs `/eval-analyze --skill eval-run`, **Then** the system presents layout convention options with per-skill nested as the recommended default
2. **Given** the user selects the per-skill nested convention, **When** scaffolding completes, **Then** the system creates `eval/eval-run/eval.yaml`, `eval/eval-run/eval.md`, and `eval/eval-run/cases/`
3. **Given** the user selects the flat convention, **When** scaffolding completes, **Then** the system creates `eval/eval-run.yaml` and `eval/cases/eval-run/`
4. **Given** the user provides `--config eval/custom/path.yaml`, **When** scaffolding completes, **Then** the system creates the config at the explicitly specified path, bypassing convention selection

---

### User Story 2 - Auto-Discover and Run Eval for a Skill (Priority: P1)

A skill author runs `/eval-run` without specifying `--config`. The system scans for eval configs using smart discovery (checking known convention patterns and any `eval.yaml` files under `eval/`), finds one or more configs, and either auto-selects (when only one exists) or prompts the user to choose which skill to evaluate.

**Why this priority**: Developers should not need to remember or type config paths. Smart discovery makes the layout choice transparent after initial scaffolding.

**Independent Test**: Create eval configs using different layout conventions, then run `/eval-run` without `--config` and verify discovery finds all configs regardless of layout.

**Acceptance Scenarios**:

1. **Given** exactly one eval config exists (in any supported location), **When** the user runs `/eval-run` without `--config`, **Then** the system auto-selects that config and proceeds
2. **Given** multiple eval configs exist (possibly in different layout conventions), **When** the user runs `/eval-run` without `--config`, **Then** the system lists all discovered configs with their skill names and prompts for selection
3. **Given** no eval configs exist, **When** the user runs `/eval-run` without `--config`, **Then** the system reports no config found and suggests running `/eval-analyze` first
4. **Given** the user provides `--config path/to/eval.yaml`, **When** the user runs `/eval-run`, **Then** the system uses that explicit path and skips auto-discovery
5. **Given** configs exist in both root-level and per-skill locations, **When** the system discovers configs, **Then** it includes all of them in the selection list (with a deprecation notice for root-level ones)

---

### User Story 3 - Migrate Root-Level Eval Config (Priority: P2)

A skill author has an existing project with `eval.yaml` at the project root (created before the layout conventions). When they run `/eval-analyze`, the system detects the root-level config, warns about the deprecated location, and offers to migrate it to the user's chosen convention.

**Why this priority**: Backward compatibility matters for existing users, but this is a one-time transition, not a daily workflow.

**Independent Test**: Place an `eval.yaml` at the project root, run `/eval-analyze`, and verify the deprecation warning appears with a migration offer.

**Acceptance Scenarios**:

1. **Given** `eval.yaml` exists at the project root, **When** the user runs `/eval-analyze`, **Then** the system displays a deprecation warning suggesting migration
2. **Given** the user accepts migration, **When** the system migrates, **Then** it moves `eval.yaml`, `eval.md`, dataset files, and run history to the chosen convention location
3. **Given** the user accepts migration, **When** the system migrates, **Then** it updates internal path references within `eval.yaml` to reflect the new location
4. **Given** the user declines migration, **When** they continue working, **Then** the system operates with the root-level config without errors
5. **Given** `eval.yaml` exists at the project root, **When** the user runs any eval command without `--config`, **Then** the system finds the root-level config but shows a deprecation notice

---

### User Story 4 - Per-Skill Run Isolation (Priority: P2)

A skill author runs evaluations for two different skills. Each skill's run output is stored relative to its own eval config location, keeping results separate and preventing cross-contamination.

**Why this priority**: Run isolation prevents confusion when comparing results across skills, but the system still functions if results are mixed (just harder to navigate).

**Independent Test**: Run evaluations for two skills and verify each skill's runs appear only in its respective output directory.

**Acceptance Scenarios**:

1. **Given** eval configs exist for `skill-a` and `skill-b`, **When** the user runs evaluations for both, **Then** results are stored in separate directories determined by each config's location and `AGENT_EVAL_RUNS_DIR`
2. **Given** eval configs exist for a skill, **When** the user runs multiple evaluations, **Then** each run creates a new timestamped directory under the skill's run output path

---

### User Story 5 - Dataset Path Resolution (Priority: P1)

A skill author edits their eval config and sets `dataset.path: cases/`. The system resolves this path relative to the eval.yaml location, not relative to the project root.

**Why this priority**: Incorrect path resolution would break every eval run. This is a foundational behavior that other features depend on.

**Independent Test**: Create an eval config at any supported location with `dataset.path: cases/`, place test cases relative to that config, and verify they are found.

**Acceptance Scenarios**:

1. **Given** an eval config at any location contains `dataset.path: cases/`, **When** the system resolves the dataset path, **Then** it resolves relative to the eval.yaml file location
2. **Given** an eval config contains an absolute dataset path, **When** the system resolves the dataset path, **Then** it uses the absolute path as-is

---

### Edge Cases

- What happens when a skill name contains special characters (e.g., dots, underscores) in directory names?
- How does the system behave when `eval/` directory exists but contains non-eval subdirectories?
- What happens if two skills produce the same config filename in flat convention?
- How does `--config` interact with auto-discovery (explicit config should always win)?
- What happens when root-level `eval.yaml` references `eval/cases/` and the migration moves it (path fixup needed)?
- How does discovery handle a mix of layout conventions in the same project?

## Requirements *(mandatory)*

### Functional Requirements

**Layout Convention Selection (Scaffolding)**

- **FR-001**: `/eval-analyze` MUST offer the user a choice of layout conventions when creating a new eval config, with per-skill nested (`eval/<skill-name>/eval.yaml`) as the recommended default
- **FR-002**: `/eval-analyze` MUST support at minimum two conventions: per-skill nested (`eval/<skill>/eval.yaml` with `eval/<skill>/cases/`) and flat (`eval/<skill>.yaml` with `eval/cases/<skill>/`)
- **FR-003**: `/eval-analyze` MUST accept `--config <path>` to bypass convention selection and scaffold at an explicit location
- **FR-004**: `/eval-analyze` MUST create the eval documentation (`eval.md`) alongside the config file, regardless of convention
- **FR-005**: `/eval-analyze` MUST create the dataset directory relative to the config file location when generating the initial config
- **FR-006**: `/eval-analyze` MUST persist the chosen layout convention at the project level so subsequent runs for other skills reuse it without re-prompting

**Auto-Discovery**

- **FR-007**: All eval commands MUST auto-discover eval configs when `--config` is not provided, by scanning known convention patterns (`eval/*/eval.yaml`, `eval/*.yaml`, root `eval.yaml`)
- **FR-008**: Auto-discovery MUST auto-select the config when exactly one is found
- **FR-009**: Auto-discovery MUST prompt for skill selection when multiple configs are found
- **FR-010**: The `--config` flag MUST override auto-discovery and use the specified path directly

**Path Resolution**

- **FR-011**: All relative paths in `eval.yaml` (dataset path, output paths) MUST resolve relative to the eval.yaml file location, not the project root
- **FR-012**: Run results MUST be stored in `$AGENT_EVAL_RUNS_DIR/<skill-name>/`, where `AGENT_EVAL_RUNS_DIR` defaults to `eval/runs` and acts as a base path under which per-skill run directories are created. The skill name MUST be derived from the `skill` field inside the eval.yaml content.

**Backward Compatibility and Migration**

- **FR-013**: The system MUST display a deprecation warning when a root-level `eval.yaml` is detected
- **FR-014**: The system MUST offer to migrate root-level eval artifacts to the user's chosen convention
- **FR-015**: Migration MUST move `eval.yaml`, `eval.md`, dataset files, and run history to the new location
- **FR-016**: Migration MUST update internal path references within `eval.yaml` to reflect the new location
- **FR-017**: The system MUST continue to operate with root-level `eval.yaml` if the user declines migration

**Housekeeping**

- **FR-018**: A single `.gitignore` pattern MUST cover run output for the default per-skill nested convention (`eval/*/runs/`)

### Key Entities

- **Layout Convention**: A named directory structure pattern for organizing eval artifacts. Each convention defines where config, documentation, datasets, and runs are stored relative to the project root.
- **Eval Config**: The `eval.yaml` file containing skill evaluation configuration (dataset path, judges, thresholds, execution settings). Location determined by the chosen layout convention.
- **Eval Directory**: The directory (or set of paths) containing all eval artifacts for one skill: config, documentation, test cases, and run results.
- **Dataset**: Test cases for evaluation, stored at a path resolved relative to the eval.yaml location.
- **Run Output**: Timestamped evaluation results, stored under `$AGENT_EVAL_RUNS_DIR/<skill-name>/`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Skill authors can set up and run evaluations for any skill in a multi-skill project without config file conflicts
- **SC-002**: New eval configs are created using the user's chosen convention on the first attempt, without manual directory creation
- **SC-003**: Users can choose their preferred eval directory layout during scaffolding, with a sensible default requiring no extra thought
- **SC-004**: Existing projects with root-level eval.yaml can migrate to their chosen convention in a single step
- **SC-005**: Users can run evaluations without specifying config paths for the common case (single-config auto-select, multi-config prompted selection)
- **SC-006**: Run results for different skills never appear in the same output directory
- **SC-007**: All eval-related commands (`/eval-analyze`, `/eval-run`, `/eval-dataset`, `/eval-optimize`, `/eval-review`, `/eval-mlflow`) work correctly with any supported layout convention
- **SC-008**: Auto-discovery finds eval configs regardless of which layout convention was used, including mixed conventions within a single project

## Clarifications

### Session 2026-05-28

- Q: How should `AGENT_EVAL_RUNS_DIR` behave with per-skill run directories? -> A: Redefine as base path: `$AGENT_EVAL_RUNS_DIR/<skill>/` for each skill's runs
- Q: Should the eval directory layout be a fixed convention or user-configurable? -> A: Configurable at scaffolding time. `/eval-analyze` should offer layout conventions (per-skill nested being the default) and let the user choose. Feedback from Antonin Stefanutti: projects should decide their own layout; the LLM agent can be smart about discovery regardless of structure. (Slack thread: #wg-agent-eval-harness, 2026-05-28)

### Session 2026-05-29

- Q: Should subsequent `/eval-analyze` runs remember the layout convention or ask again? -> A: Remember at project level. Ask once, reuse for all subsequent skills.
- Q: In flat convention, where do datasets and runs go? -> A: Datasets at `eval/cases/<skill>/`, runs at `$AGENT_EVAL_RUNS_DIR/<skill>/`. Flat top-level, skill-namespaced inside shared parent directories.
- Q: How is skill name derived for run isolation with custom `--config` paths? -> A: Read the `skill` field from inside the eval.yaml content. This is authoritative regardless of file location.

## Assumptions

- Skill names are unique within a plugin project (derived from skill directory names)
- The `eval/` directory is reserved for evaluation artifacts; other project content does not reside there
- Existing root-level `eval.yaml` files reference a single skill (the project's primary or only skill)
- `AGENT_EVAL_RUNS_DIR` is redefined as a base path (default `eval/runs`); per-skill runs are stored at `$AGENT_EVAL_RUNS_DIR/<skill-name>/`
- Users are comfortable with one additional level of directory nesting for the default convention
- The LLM agent can intelligently discover eval configs across different layout conventions without requiring a registry file
- PR #74 (harness-level context) will compose with this layout, with each eval config carrying its own `harness_context`
- Suite execution (running all discovered configs in sequence, per issue #3) is a future feature that builds on the discovery mechanism defined here
