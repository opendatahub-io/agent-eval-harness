# Feature Specification: Reusable Judges Library

**Feature Branch**: `004-reusable-judges-library`  
**Created**: 2026-05-17  
**Status**: Draft  
**Input**: User description: "Reusable judges library - bundled, skill-agnostic judges that ship with the harness for common evaluation patterns (safety, process quality, efficiency)"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Use a Built-in Judge in eval.yaml (Priority: P1)

A skill author configuring eval.yaml wants to add a safety check without writing custom code or prompts. They reference a built-in judge by name and the harness resolves it automatically.

**Why this priority**: This is the primary interaction point. If authors can't easily reference and use library judges, the entire feature has no value.

**Independent Test**: Can be fully tested by adding a `builtin` judge entry to eval.yaml, running scoring against a test case, and verifying the judge executes and produces a pass/fail result.

**Acceptance Scenarios**:

1. **Given** an eval.yaml with a `builtin` field referencing a valid judge name, **When** scoring runs, **Then** the harness locates the bundled judge (Python function or LLM prompt) and executes it against the case record.
2. **Given** an eval.yaml with a `builtin` field referencing an unknown name, **When** scoring runs, **Then** the harness prints a clear error message naming the unknown judge and listing available built-in judges.
3. **Given** an eval.yaml mixing built-in and custom judges, **When** scoring runs, **Then** both types execute and their results appear in the score report.

---

### User Story 2 - Browse Available Judges (Priority: P1)

A skill author wants to discover what built-in judges exist so they can choose which ones to add to their eval.yaml. They browse a directory of categorized judge files, each with a docstring explaining what it checks and what event data it needs.

**Why this priority**: Without discoverability, authors won't know what's available. This is equally critical to Story 1 because adoption depends on it.

**Independent Test**: Can be fully tested by listing the judges directory, reading any judge file, and confirming the docstring describes the check, required fields, and failure meaning.

**Acceptance Scenarios**:

1. **Given** the harness is installed, **When** a user lists the judges directory, **Then** they see judges organized into category subdirectories (safety, process, efficiency, quality).
2. **Given** any judge file in the library, **When** a user reads it, **Then** the file contains a docstring (Python module docstring or Markdown preamble) describing what it checks, what record fields it requires, and what a failure means.

---

### User Story 3 - Vendor a Library Judge for Customization (Priority: P2)

A skill author wants to modify a built-in judge's behavior for their specific project. They copy the judge file into their project's eval directory and reference it using the appropriate field for its type: `module`/`function` for Python judges, `prompt_file` for LLM judges.

**Why this priority**: Customization is important but secondary to basic usage and discovery. The library judges are useful out of the box; vendoring is a power-user workflow.

**Independent Test**: Can be fully tested by copying a built-in judge to a local directory, modifying its behavior, referencing it via the standard field (`module`/`function` or `prompt_file`) in eval.yaml, and confirming the modified behavior takes effect.

**Acceptance Scenarios**:

1. **Given** a built-in Python judge file copied to a project's local directory, **When** the author references it via `module`/`function` in eval.yaml (without `builtin`), **Then** the local copy executes instead of the built-in version.
2. **Given** a built-in LLM judge prompt copied locally, **When** the author references it via `prompt_file` in eval.yaml (without `builtin`), **Then** the local copy executes.
3. **Given** a vendored judge with modified thresholds, **When** scoring runs, **Then** the modified thresholds apply.

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
- What happens when two judges share the same `name` in the same eval.yaml (whether builtin, custom, or mixed)? The harness should reject the duplicate and report an error.
- What happens when a harness upgrade changes a built-in judge's behavior? Existing eval baselines may shift. Each judge includes a `__version__` string for documentation; behavior changes are signaled via version bump and changelog. Authors who need the old behavior can vendor the judge file.

## Clarifications

### Session 2026-05-17

- Q: How does `builtin` in eval.yaml resolve to a judge file across category subdirectories? → A: Flat name resolution. The harness auto-discovers judges across all category dirs and builds a flat registry. Authors reference judges by simple name (e.g., `builtin: no_harmful_content`), not by category path. Name uniqueness is only enforced within a single eval.yaml. A `module`/`function` or `prompt_file` judge (without `builtin`) can use the same `name` as a builtin judge without conflict, which is how vendoring (US3) works.
- Q: Can built-in judges accept configurable parameters (e.g., cost thresholds)? → A: Yes. An optional `arguments` dict in eval.yaml is passed as `**kwargs` (Python judges) or rendered as Jinja template variables (LLM judges). This works for all judge types, not just builtins.
- Q: Which specific judges should the initial library ship with? → A: Four judges: safety/`no_harmful_content` (LLM, checks agent output for harmful/dangerous content via nuanced LLM evaluation), process/`tool_call_validation` (Python, verifies tool calls follow expected patterns), efficiency/`cost_budget` (Python, checks `cost_usd` against a configurable threshold), quality/`output_completeness` (LLM, evaluates output completeness via LLM).
- Q: How should judge versioning work for stability across harness upgrades? → A: Each judge file defines a `__version__` string for documentation only. No pinning mechanism in eval.yaml for this feature. Authors who need old behavior can vendor the judge.

### Session 2026-05-19 (PR #66 review feedback from @astefanutti)

- Q: Should `type: builtin` be the discriminator, or a `builtin` field? → A: Use a `builtin` field as the type discriminator. This follows the existing inference pattern where presence of a field (`check`, `prompt`, `module`) determines judge type. The `name` field stays user-defined for thresholds and reports.
- Q: Should built-in judges support LLM prompts, not just Python functions? → A: Yes. The registry auto-detects from file extension: `.py` for Python function judges, `.md` for LLM prompt template judges. Both are referenced the same way via `builtin: <name>`.
- Q: Are category subdirectories user-facing? → A: No. Categories (safety/, process/, etc.) are filesystem organization for browsability. Users reference judges by flat name (or optionally by FQN like `safety/no_harmful_content`). Category is derived from the parent directory for report grouping only.
- Q: What templating do LLM prompt judges use? → A: Jinja2 with `arguments` and `outputs` as template variables. Rendered before the LLM call. This applies to all LLM judges (builtin `.md` and inline `prompt_file`), not just builtins.

### Session 2026-05-20 (PR #66 review round 2 from @astefanutti)

- Q: Should `no_harmful_content` be an LLM judge instead of Python? → A: Yes. Harmful content detection benefits from nuanced LLM evaluation rather than crude pattern matching. Moved to LLM type.
- Q: Should FQN references (e.g., `safety/no_harmful_content`) be supported? → A: Yes. Both flat (`builtin: no_harmful_content`) and FQN (`builtin: safety/no_harmful_content`) references are supported. FQN is optional but useful for disambiguation.
- Q: Should `config` be renamed to `arguments`? → A: Yes. Renamed to `arguments` throughout. For Python judges, passed as `**kwargs`. For LLM judges, available as Jinja template variable. This naming better reflects that values are callable parameters, not static configuration.
- Q: Should `arguments` work for all judge types, not just builtins? → A: Yes. The `arguments` field works uniformly across all judge types: builtin, `module`/`function`, `check`, and `prompt`/`prompt_file`. This provides full symmetry.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The harness MUST ship a `judges/` directory within the `agent_eval` package containing categorized judge files organized by domain (safety, process, efficiency, quality).
- **FR-002**: Python judge files (`.py`) MUST be standalone modules exporting a function with signature `judge(outputs: dict, **kwargs) -> tuple[bool, str]`. The `**kwargs` receive values from the `arguments` dict in eval.yaml. Judges that don't need arguments simply ignore them. This signature applies uniformly to builtin Python judges, `module`/`function` judges, and is backward-compatible with existing judges that accept only `(outputs)`.
- **FR-002a**: LLM judge files (`.md`) MUST be Jinja2 prompt templates with `arguments` and `outputs` available as template variables. The harness renders the template, sends it to the configured LLM model, and parses the response into a `(bool, str)` result. This Jinja2 rendering with `arguments` applies to all LLM judges (builtin `.md` files and inline `prompt_file` judges).
- **FR-003**: The harness MUST support a `builtin` field on judge entries in eval.yaml that resolves to a bundled judge by flat name (e.g., `builtin: no_harmful_content`) or fully-qualified name with category prefix (e.g., `builtin: safety/no_harmful_content`). The `name` field remains user-defined for thresholds and reports. The harness auto-discovers judges across all category subdirectories, auto-detects type from file extension (`.py` or `.md`), and builds a flat registry. Name collisions across categories MUST be caught with a clear error.
- **FR-004**: When `builtin` references an unknown name, the harness MUST raise a clear error listing all available built-in judge names.
- **FR-005**: Each judge file MUST include documentation describing what it checks, what `outputs` fields it reads, and what a failure means. Python judges use module-level docstrings; LLM judges use a Markdown preamble section.
- **FR-006**: Built-in judges MUST compose with existing judge types (inline `check`, LLM `prompt`, external `module`/`function`) in the same eval.yaml without conflicts.
- **FR-007**: Built-in judges MUST handle missing event data gracefully, returning a clear failure reason (not an unhandled exception) when required fields are absent from the record.
- **FR-008**: The score report MUST label built-in judges distinctly from custom judges so users can differentiate guardrail failures from skill-specific quality issues.
- **FR-009**: The harness MUST reject duplicate judge names within a single eval.yaml (whether built-in, custom, or mixed).
- **FR-010**: Each judge file MUST define a `__version__` string (Python: module attribute, LLM: YAML frontmatter) for documentation purposes. No runtime pinning mechanism is provided; version changes are communicated via changelog.
- **FR-011**: The `if` field in judge configurations MUST be evaluated using `eval(expr, {"__builtins__": {}}, safe_locals)` where `safe_locals` contains only the `outputs` and `annotations` dicts. This disables all Python builtins completely. This is an existing security constraint on all judge types. eval.yaml is a repository-controlled, trusted configuration file and MUST NOT be accepted from untrusted sources.
- **FR-012**: The `builtin` field MUST be mutually exclusive with `check`, `prompt`, `prompt_file`, `module`, and `function`. If both `builtin` and any of these fields are set, the harness MUST raise a validation error listing the conflicting fields.
- **FR-013**: LLM judges (builtin and inline) MUST support the `model` field for per-judge model override, falling back to the `models.judge` default when not specified.
- **FR-014**: The `arguments` dict MUST be available to all judge types: passed as `**kwargs` for Python judges (builtin and `module`/`function`), available as a local variable for inline `check` judges, and available as a Jinja2 template variable for LLM judges (builtin `.md` and `prompt_file`). For backward compatibility, `module`/`function` judges that don't accept `**kwargs` MUST NOT break when `arguments` is empty.

### Key Entities

- **Python Judge File**: A `.py` file in `agent_eval/judges/<category>/` exporting a scoring function. Attributes: name, category, docstring, required record fields, version.
- **LLM Judge File**: A `.md` file in `agent_eval/judges/<category>/` containing a Jinja2 prompt template. Attributes: name, category, preamble documentation, version (in YAML frontmatter).
- **Built-in Judge Reference**: An eval.yaml entry with a `builtin` field that the harness resolves to a judge file (Python or LLM) at scoring time. The `name` field is user-defined.
- **Case Record**: The existing dict of outputs, events, annotations, and execution metadata passed to all judges. Library judges consume the same record as custom judges.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A skill author can add a built-in safety judge to their eval.yaml in under 30 seconds (two lines of config: name and builtin).
- **SC-002**: The initial library ships with four judges: `no_harmful_content` (safety, LLM), `tool_call_validation` (process, Python), `cost_budget` (efficiency, Python), and `output_completeness` (quality, LLM), covering the most common evaluation patterns across both judge types.
- **SC-003**: 100% of library judge files include a module docstring that describes the check, required fields, and failure meaning.
- **SC-004**: All library judges handle missing event data without raising unhandled exceptions.
- **SC-005**: Score reports clearly distinguish library judge results from custom judge results for any eval run using both types.

## Assumptions

- The `outputs["events"]` field is always present in the case record but MAY be an empty list. Judges that require event data MUST handle empty lists gracefully and return clear failure messages rather than raising exceptions.
- The existing `JudgeConfig` dataclass and `load_judges` function in `score.py` will be extended (not replaced) to support the new `builtin` field resolution.
- Python judges (builtin and external) use `judge(outputs: dict, **kwargs) -> tuple[bool, str]`. LLM judges (builtin and inline `prompt_file`) use Jinja2 templating with `arguments` and `outputs` as template variables. The `arguments` field provides a unified parameterization mechanism across all judge types.
- Execution metrics (`cost_usd`, `duration_s`, `token_usage`, `num_turns`) are present in the case record but MAY be None. Judges that depend on metrics MUST handle missing values gracefully.
- Judge presets (curated bundles like `safety-baseline`) are out of scope for this feature. Individual judge references come first; preset bundles can layer on later.
- Regression fingerprinting (comparing event patterns between runs) is out of scope. It requires a new judge type beyond what this feature introduces.
