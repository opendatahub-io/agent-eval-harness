# Feature Specification: Reusable Judges Library

**Feature Branch**: `004-reusable-judges-library`  
**Created**: 2026-05-17  
**Status**: Draft  
**Input**: User description: "Reusable judges library - bundled, skill-agnostic judges that ship with the harness for common evaluation patterns (safety, process quality, efficiency)"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Use a Built-in Judge in eval.yaml (Priority: P1)

A skill author configuring eval.yaml wants to add a safety check without writing custom Python. They reference a built-in judge by name and the harness resolves it automatically.

**Why this priority**: This is the primary interaction point. If authors can't easily reference and use library judges, the entire feature has no value.

**Independent Test**: Can be fully tested by adding a `type: builtin` judge entry to eval.yaml, running scoring against a test case, and verifying the judge executes and produces a pass/fail result.

**Acceptance Scenarios**:

1. **Given** an eval.yaml with `type: builtin` and a valid judge name, **When** scoring runs, **Then** the harness locates the bundled judge script and executes it against the case record.
2. **Given** an eval.yaml with `type: builtin` and an invalid judge name, **When** scoring runs, **Then** the harness prints a clear error message naming the unknown judge and listing available built-in judges.
3. **Given** an eval.yaml mixing built-in and custom judges, **When** scoring runs, **Then** both types execute and their results appear in the score report.

---

### User Story 2 - Browse Available Judges (Priority: P1)

A skill author wants to discover what built-in judges exist so they can choose which ones to add to their eval.yaml. They browse a directory of categorized judge files, each with a docstring explaining what it checks and what event data it needs.

**Why this priority**: Without discoverability, authors won't know what's available. This is equally critical to Story 1 because adoption depends on it.

**Independent Test**: Can be fully tested by listing the judges directory, reading any judge file, and confirming the docstring describes the check, required fields, and failure meaning.

**Acceptance Scenarios**:

1. **Given** the harness is installed, **When** a user lists the judges directory, **Then** they see judges organized into category subdirectories (safety, process, efficiency).
2. **Given** any judge file in the library, **When** a user reads it, **Then** the file contains a module docstring describing what it checks, what record fields it requires, and what a failure means.

---

### User Story 3 - Vendor a Library Judge for Customization (Priority: P2)

A skill author wants to modify a built-in judge's behavior for their specific project. They copy the judge file into their project's eval directory and reference it as a standard `module`/`function` judge.

**Why this priority**: Customization is important but secondary to basic usage and discovery. The library judges are useful out of the box; vendoring is a power-user workflow.

**Independent Test**: Can be fully tested by copying a built-in judge to a local directory, modifying its thresholds, referencing it as a `module`/`function` judge in eval.yaml, and confirming the modified behavior takes effect.

**Acceptance Scenarios**:

1. **Given** a built-in judge file copied to a project's local directory, **When** the author references it via `module`/`function` in eval.yaml, **Then** the local copy executes instead of the built-in version.
2. **Given** a vendored judge with modified thresholds, **When** scoring runs, **Then** the modified thresholds apply.

---

### User Story 4 - Library Judges in Score Reports (Priority: P2)

When evaluation results are reported, library judges are visually distinguishable from skill-specific judges, so the author can quickly separate generic guardrail failures from skill quality issues.

**Why this priority**: Important for usability but the feature works without it. Report grouping is a polish item that improves the evaluation review experience.

**Independent Test**: Can be fully tested by running scoring with both built-in and custom judges, then checking the HTML report for distinct grouping.

**Acceptance Scenarios**:

1. **Given** an eval run with both built-in and custom judges, **When** the report is generated, **Then** library judges appear in a separate section or are labeled to distinguish them from skill-specific judges.

---

### Edge Cases

- What happens when a built-in judge requires event data (`outputs["events"]`) but events were not captured (events list is empty)? The judge should handle this gracefully and report a clear failure reason rather than crashing.
- What happens when the same judge name is defined both as `type: builtin` and as a custom judge in the same eval.yaml? The harness should reject the duplicate and report an error.
- What happens when a harness upgrade changes a built-in judge's behavior? Existing eval baselines may shift. Judge files should include a version indicator so authors can pin to a specific behavior.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The harness MUST ship a `judges/` directory within the `agent_eval` package containing categorized judge scripts organized by domain (safety, process, efficiency).
- **FR-002**: Each judge file MUST be a standalone Python module exporting a function with signature `(outputs: dict) -> tuple[bool, str]` compatible with the existing external code judge interface (`module`/`function`).
- **FR-003**: The harness MUST support a `type: builtin` judge type in eval.yaml that resolves to a bundled judge by name without requiring the author to know the file path.
- **FR-004**: When `type: builtin` references an unknown name, the harness MUST raise a clear error listing all available built-in judge names.
- **FR-005**: Each judge file MUST include a module-level docstring describing what it checks, what `outputs` fields it reads, and what a failure means.
- **FR-006**: Built-in judges MUST compose with existing judge types (inline `check`, LLM `prompt`, external `module`/`function`) in the same eval.yaml without conflicts.
- **FR-007**: Built-in judges MUST handle missing event data gracefully, returning a clear failure reason (not an unhandled exception) when required fields are absent from the record.
- **FR-008**: The score report MUST label built-in judges distinctly from custom judges so users can differentiate guardrail failures from skill-specific quality issues.
- **FR-009**: The harness MUST reject duplicate judge names within a single eval.yaml (whether built-in, custom, or mixed).

### Key Entities

- **Judge Module**: A Python file in `agent_eval/judges/<category>/` exporting a scoring function. Attributes: name, category, docstring, required record fields, version.
- **Built-in Judge Reference**: An eval.yaml entry with `type: builtin` and a `name` that the harness resolves to a judge module at scoring time.
- **Case Record**: The existing dict of outputs, events, annotations, and execution metadata passed to all judges. Library judges consume the same record as custom judges.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A skill author can add a built-in safety judge to their eval.yaml in under 30 seconds (two lines of config: name and type).
- **SC-002**: The initial library ships with at least one judge per category (safety, process quality, efficiency), covering the most common evaluation patterns.
- **SC-003**: 100% of library judge files include a module docstring that describes the check, required fields, and failure meaning.
- **SC-004**: All library judges handle missing event data without raising unhandled exceptions.
- **SC-005**: Score reports clearly distinguish library judge results from custom judge results for any eval run using both types.

## Assumptions

- Structured events (`outputs["events"]`) are available in the case record when judges run. Judges that depend on events handle the empty-list case gracefully.
- The existing `JudgeConfig` dataclass and `load_judges` function in `score.py` will be extended (not replaced) to support the new `type: builtin` resolution.
- Library judges follow the same `(outputs: dict) -> tuple[bool, str]` contract as external code judges, keeping the scoring pipeline uniform.
- Execution metrics (`cost_usd`, `duration_s`, `token_usage`, `num_turns`) are available in the case record for efficiency judges.
- Judge presets (curated bundles like `safety-baseline`) are out of scope for this feature. Individual judge references come first; preset bundles can layer on later.
- Regression fingerprinting (comparing event patterns between runs) is out of scope. It requires a new judge type beyond what this feature introduces.
