# Feature Specification: Flexible Eval Directory Layout

**Feature Branch**: `005-eval-directory-layout`
**Created**: 2026-05-28
**Updated**: 2026-05-30
**Status**: Draft
**Input**: Flexible eval directory layout for multi-eval projects. Smart defaults based on project complexity, with auto-discovery that adapts to whichever layout the user chose.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Smart Scaffolding Based on Project Complexity (Priority: P1)

A developer runs `/eval-analyze` in a project. The system detects how many eval configs already exist and adapts its behavior:
- **First eval in the project**: creates `eval.yaml` at the project root (simple, zero-friction default).
- **Second eval onward**: detects that a root-level config already exists, offers to organize into an `eval/` directory layout, and scaffolds the new config there.

The developer can always override with `--config <path>` to place the config anywhere.

**Why this priority**: This is the entry point for the entire eval workflow. Getting the default right for both simple and complex projects avoids unnecessary friction.

**Independent Test**: Run `/eval-analyze` in a fresh project, verify the config is created at the root. Then run `/eval-analyze` again for a different eval target, verify it offers to organize into `eval/`.

**Acceptance Scenarios**:

1. **Given** a project with no existing eval config, **When** the user runs `/eval-analyze`, **Then** the system creates `eval.yaml` at the project root
2. **Given** a project with one root-level eval config, **When** the user runs `/eval-analyze` for a different eval target, **Then** the system detects the conflict and offers to organize into an `eval/` directory layout
3. **Given** the user accepts the layout offer, **When** scaffolding completes, **Then** the system moves the existing root config and creates the new config under `eval/`
4. **Given** the user provides `--config eval/custom/path.yaml`, **When** scaffolding completes, **Then** the system creates the config at the explicitly specified path, bypassing layout selection

---

### User Story 2 - Auto-Discover and Run Eval (Priority: P1)

A developer runs `/eval-run` without specifying `--config`. The system scans for eval configs using smart discovery (checking `eval/` subdirectories, `eval/*.yaml`, and root `eval.yaml`), finds one or more configs, and either auto-selects (when only one exists) or prompts the user to choose which eval to run.

**Why this priority**: Developers should not need to remember or type config paths. Smart discovery makes the layout choice transparent after initial scaffolding.

**Independent Test**: Create eval configs using different layouts, then run `/eval-run` without `--config` and verify discovery finds all configs regardless of layout.

**Acceptance Scenarios**:

1. **Given** exactly one eval config exists (in any supported location), **When** the user runs `/eval-run` without `--config`, **Then** the system auto-selects that config and proceeds
2. **Given** multiple eval configs exist (possibly in different layouts), **When** the user runs `/eval-run` without `--config`, **Then** the system lists all discovered configs with their eval names and prompts for selection
3. **Given** no eval configs exist, **When** the user runs `/eval-run` without `--config`, **Then** the system reports no config found and suggests running `/eval-analyze` first
4. **Given** the user provides `--config path/to/eval.yaml`, **When** the user runs `/eval-run`, **Then** the system uses that explicit path and skips auto-discovery
5. **Given** configs exist in both root-level and `eval/` locations, **When** the system discovers configs, **Then** it includes all of them in the selection list
6. **Given** a single root-level `eval.yaml` exists, **When** the user runs any eval command without `--config`, **Then** the system uses it directly without any warnings

---

### User Story 3 - Organize When Outgrowing Root Layout (Priority: P2)

A developer has an existing project with `eval.yaml` at the project root. They add a second eval target. The system detects the conflict and offers to reorganize into an `eval/` directory structure, moving the existing config and creating the new one.

**Why this priority**: Smooth transition from simple to organized layout matters for growing projects, but most projects start with a single eval and may never need this.

**Independent Test**: Start with a root-level `eval.yaml`, run `/eval-analyze` for a second eval target, verify the reorganization offer and correct file moves.

**Acceptance Scenarios**:

1. **Given** `eval.yaml` exists at the project root and the user runs `/eval-analyze` for a different eval target, **When** the system detects the naming conflict, **Then** it offers to reorganize into `eval/` layout
2. **Given** the user accepts reorganization, **When** the system migrates, **Then** it moves the existing config, its `eval.md`, and dataset files to `eval/<name>/`
3. **Given** the user accepts reorganization, **When** the system migrates, **Then** it updates `dataset.path` within `eval.yaml` to reflect the new location
4. **Given** the user declines reorganization, **When** they provide `--config` explicitly, **Then** the system places the new config at the specified path without moving anything

---

### User Story 4 - Per-Eval Run Isolation (Priority: P2)

A developer runs evaluations for two different eval targets. Each target's run output is stored under `$AGENT_EVAL_RUNS_DIR/<eval-name>/`, keeping results separate and preventing cross-contamination.

**Why this priority**: Run isolation prevents confusion when comparing results across eval targets, but the system still functions if results are mixed (just harder to navigate).

**Independent Test**: Run evaluations for two targets and verify each target's runs appear only in its respective output directory.

**Acceptance Scenarios**:

1. **Given** eval configs exist for `eval-a` and `eval-b`, **When** the user runs evaluations for both, **Then** results are stored in separate directories under `AGENT_EVAL_RUNS_DIR`
2. **Given** eval configs exist for a target, **When** the user runs multiple evaluations, **Then** each run creates a new timestamped directory under the target's run output path

---

### User Story 5 - Dataset Path Resolution (Priority: P1)

A developer edits their eval config and sets `dataset.path: cases/`. The system resolves this path relative to the eval.yaml location, not relative to the project root.

**Why this priority**: Incorrect path resolution would break every eval run. This is a foundational behavior that other features depend on.

**Independent Test**: Create an eval config at any supported location with `dataset.path: cases/`, place test cases relative to that config, and verify they are found.

**Acceptance Scenarios**:

1. **Given** an eval config at any location contains `dataset.path: cases/`, **When** the system resolves the dataset path, **Then** it resolves relative to the eval.yaml file location
2. **Given** an eval config contains an absolute dataset path, **When** the system resolves the dataset path, **Then** it uses the absolute path as-is

---

### User Story 6 - Shared Datasets Across Evals (Priority: P3)

A developer has two eval configs that share the same dataset (e.g., one evaluates a skill, another evaluates a prompt-based agent, both using the same test cases). Each config's `dataset.path` points to a shared location.

**Why this priority**: Dataset independence from eval configs is important for flexibility but is already supported via `dataset.path`. This story validates that the layout system does not break shared datasets.

**Independent Test**: Create two eval configs each pointing `dataset.path` to a shared `eval/cases/shared/` directory. Run both evals and verify they use the same test cases.

**Acceptance Scenarios**:

1. **Given** two eval configs both reference `dataset.path: ../cases/shared/`, **When** the system resolves dataset paths, **Then** both configs find the same test cases
2. **Given** a dataset directory is not co-located with any eval config, **When** the user specifies an absolute or relative path, **Then** the system resolves it correctly

---

### Edge Cases

- What happens when an eval name contains special characters (e.g., dots, underscores) in directory names?
- How does the system behave when `eval/` directory exists but contains non-eval subdirectories?
- What happens if two eval targets produce the same config filename in flat layout?
- How does `--config` interact with auto-discovery (explicit config should always win)?
- What happens when root-level `eval.yaml` references `eval/cases/` and the reorganization moves it (path fixup needed)?
- How does discovery handle a mix of layouts in the same project?

## Requirements *(mandatory)*

### Error Handling

- YAML files that fail parsing during discovery MUST be skipped with a warning to stderr
- YAML files without a `skill` field MUST be skipped (not valid eval configs)
- If reorganization target path already exists, abort with an error (don't overwrite)
- If source files are missing during reorganization (e.g., no eval.md), warn but continue with what exists
- If eval.yaml has no `skill` field during reorganization, abort (can't determine target path)

### Functional Requirements

**Smart Scaffolding**

- **FR-001**: `/eval-analyze` MUST create `eval.yaml` at the project root when no other eval config exists (single-eval default)
- **FR-002**: `/eval-analyze` MUST detect when a root-level eval config already exists and offer to organize into an `eval/` directory layout when creating a second eval config
- **FR-003**: `/eval-analyze` MUST accept `--config <path>` to bypass layout selection and scaffold at an explicit location
- **FR-004**: `/eval-analyze` MUST create the eval documentation (`eval.md`) alongside the config file
- **FR-005**: When organizing into `eval/`, the system MUST support a per-eval nested layout (`eval/<name>/eval.yaml` with dataset path as configured in `dataset.path`)
- **FR-006**: `/eval-analyze` MUST infer the current layout from existing file structure (discovery patterns) rather than persisting layout state in a separate file

**Auto-Discovery**

- **FR-007**: All eval commands MUST auto-discover eval configs when `--config` is not provided, by scanning: `eval/*/eval.yaml`, `eval/*.yaml`, root `eval.yaml`
- **FR-008**: Auto-discovery MUST auto-select the config when exactly one is found
- **FR-009**: Auto-discovery MUST prompt for selection when multiple configs are found
- **FR-010**: The `--config` flag MUST override auto-discovery and use the specified path directly

**Path Resolution**

- **FR-011**: The `dataset.path` in `eval.yaml` MUST resolve relative to the eval.yaml file location, not the project root. Note: `outputs[].path` values are workspace-relative (resolved against the execution workspace by `workspace.py` and `collect.py`) and MUST NOT be changed by this requirement.
- **FR-012**: Run results MUST be stored in `$AGENT_EVAL_RUNS_DIR/<eval-name>/`, where `AGENT_EVAL_RUNS_DIR` defaults to `eval/runs` and acts as a base path under which per-eval run directories are created. The eval name MUST be derived from the `skill` field inside the eval.yaml content (the field is named `skill` for backward compatibility, but serves as the eval identifier for any target type). The eval name MUST be validated as a single path segment (no path separators, `..`, or control characters) before use in path construction.

**Backward Compatibility**

- **FR-013**: Root-level `eval.yaml` MUST remain a fully supported, first-class location for single-eval projects (no deprecation warnings)
- **FR-014**: When a second eval config is needed, the system MUST offer to reorganize into an `eval/` layout, moving the existing root config
- **FR-015**: Reorganization MUST move `eval.yaml`, `eval.md`, and dataset files to the new location. Run history is NOT moved (runs are stored under `$AGENT_EVAL_RUNS_DIR` which is independent of config location).
- **FR-016**: Reorganization MUST update `dataset.path` within `eval.yaml` to reflect the new location
- **FR-017**: The system MUST continue to operate with root-level `eval.yaml` if the user declines reorganization

**Housekeeping**

- **FR-018**: A single `.gitignore` pattern MUST cover run output directories (`eval/runs/`)

### Key Entities

- **Eval Layout**: A directory structure pattern for organizing eval artifacts, inferred from existing file structure (not persisted). Determines where config, documentation, and runs are stored relative to the project root. Datasets are located independently via `dataset.path`.
- **Eval Config**: The `eval.yaml` file containing evaluation configuration (dataset path, judges, thresholds, execution settings). Can be at the project root (single-eval) or under `eval/` (multi-eval).
- **Dataset**: Test cases for evaluation, stored at a path resolved relative to the eval.yaml location. Datasets are independent of eval configs and can be shared across multiple evals.
- **Run Output**: Timestamped evaluation results, stored under `$AGENT_EVAL_RUNS_DIR/<eval-name>/`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Developers can set up and run evaluations for multiple eval targets in the same project without config file conflicts
- **SC-002**: Single-eval projects work with root-level `eval.yaml` with zero additional configuration or warnings
- **SC-003**: The system adapts its scaffolding behavior based on project complexity (single vs. multi-eval)
- **SC-004**: Projects outgrowing root-level layout can reorganize into `eval/` in a single step
- **SC-005**: Users can run evaluations without specifying config paths for the common case (single-config auto-select, multi-config prompted selection)
- **SC-006**: Run results for different eval targets never appear in the same output directory
- **SC-007**: All eval-related commands (`/eval-analyze`, `/eval-run`, `/eval-dataset`, `/eval-optimize`, `/eval-review`, `/eval-mlflow`) work correctly with any supported layout
- **SC-008**: Auto-discovery finds eval configs regardless of which layout was used, including mixed layouts within a single project
- **SC-009**: Datasets can be shared across multiple eval configs without duplication

## Clarifications

### Session 2026-05-28

- Q: How should `AGENT_EVAL_RUNS_DIR` behave with per-eval run directories? -> A: Redefine as base path: `$AGENT_EVAL_RUNS_DIR/<eval-name>/` for each eval target's runs
- Q: Should the eval directory layout be a fixed convention or user-configurable? -> A: Configurable at scaffolding time. `/eval-analyze` should offer layout options and let the user choose. Feedback from Antonin Stefanutti: projects should decide their own layout; the LLM agent can be smart about discovery regardless of structure. (Slack thread: #wg-agent-eval-harness, 2026-05-28)

### Session 2026-05-29

- Q: Should subsequent `/eval-analyze` runs remember the layout or ask again? -> A: Infer from existing file structure. Discovery already scans the patterns, so the layout is implicit in what's on disk. No persistence file needed.
- Q: In flat layout, where do datasets and runs go? -> A: Datasets are user-specified via `dataset.path` (not derived from eval name). Runs at `$AGENT_EVAL_RUNS_DIR/<eval-name>/`.
- Q: How is eval name derived for run isolation with custom `--config` paths? -> A: Read the `skill` field from inside the eval.yaml content. This is authoritative regardless of file location.

### Session 2026-05-30

- Q: Should root-level `eval.yaml` be deprecated? -> A: No. Root-level is the natural default for single-eval projects. No deprecation warnings. Migration only offered when adding a second eval creates a conflict. (Feedback: Antonin Stefanutti, PR #85 review)
- Q: Should datasets be coupled to eval names in the directory layout? -> A: No. Datasets are independently located via `dataset.path`. They can be shared across multiple eval configs. The layout should not force per-eval dataset directories. (Feedback: Antonin Stefanutti, PR #85 review)
- Q: Should the layout persistence file be a narrow dot-file or a broader eval metadata file? -> A: Neither. Layout is inferred from existing file structure via discovery patterns. No persistence file needed. In single-eval mode there's no `eval/` directory, so a file there can't exist anyway. "Layout" preferred over "convention" as terminology. (Feedback: Antonin Stefanutti, PR #85 review)
- Q: Should the spec assume "skill" as the only unit of testing? -> A: No. Issue #77 introduces prompt-based evaluation without skill wrappers. The directory layout should work for any eval target. The `skill` field in eval.yaml remains the source for the eval name used in directory paths, but the spec language should not assume all evals test skills. (Feedback: Antonin Stefanutti, PR #85 review)

## Assumptions

- Eval names (from the `skill` field in eval.yaml) are unique within a project
- The `eval/` directory is reserved for evaluation artifacts; other project content does not reside there
- Existing root-level `eval.yaml` files reference a single eval target
- `AGENT_EVAL_RUNS_DIR` is redefined as a base path (default `eval/runs`); per-eval runs are stored at `$AGENT_EVAL_RUNS_DIR/<eval-name>/`
- The LLM agent can intelligently discover eval configs across different layouts without requiring a registry file
- PR #74 (harness-level context) will compose with this layout, with each eval config carrying its own `harness_context`
- Suite execution (running all discovered configs in sequence, per issue #3) is a future feature that builds on the discovery mechanism defined here
- Prompt-based evaluation (issue #77) will use the same directory layout and discovery mechanism, with the `skill` field serving as the eval identifier
