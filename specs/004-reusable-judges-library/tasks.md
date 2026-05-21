# Tasks: Reusable Judges Library

**Input**: Design documents from `specs/004-reusable-judges-library/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: Create the judges package structure with empty `__init__.py` files

- [ ] T001 Create `agent_eval/judges/` package directory with `__init__.py` (empty initially), plus category subdirectories `safety/`, `process/`, `efficiency/`, `quality/` each with their own empty `__init__.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Extend `JudgeConfig`, build the dual-type `BuiltinJudgeRegistry`, and add Jinja2 support

**CRITICAL**: No user story work can begin until this phase is complete

- [ ] T002 Add `builtin: str = ""` and `arguments: dict = field(default_factory=dict)` fields to `JudgeConfig` dataclass in `agent_eval/config.py`. Update YAML parsing to populate these fields from eval.yaml judge entries (the `builtin` field is read directly from YAML, like `check` and `prompt`).
- [ ] T003 Implement `BuiltinJudgeRegistry` class in `agent_eval/judges/__init__.py`. The `discover()` method scans category subdirectories: for `.py` files, import the module and store a Python entry with `(module, function_name)` tuple; for `.md` files, record the prompt path as an LLM entry. Each entry stores `kind` ("python"/"llm") and `category` (parent dir name). Raises `ValueError` on name collisions across categories. `get(name)` accepts flat names (`no_harmful_content`) or FQN (`safety/no_harmful_content`) and returns the entry, or raises `ValueError` listing available names. `list_names()` returns a sorted list. Judge name derived from filename without extension.
- [ ] T004 Add duplicate judge name validation at the start of `load_judges()` in `skills/eval-run/scripts/score.py`. Collect all judge names from the config list, raise `ValueError` if any name appears more than once.
- [ ] T005 Add `builtin` branch to `load_judges()` in `skills/eval-run/scripts/score.py`. Instantiate `BuiltinJudgeRegistry` lazily: only create the registry and call `discover()` on first encounter of a judge with `builtin` set. Validate mutual exclusivity: if `builtin` is set alongside `check`, `prompt`, `prompt_file`, `module`, or `function`, raise an error listing the conflicting fields. For Python entries (`kind == "python"`): extract the callable via `getattr(module, function_name)` and wrap it to pass `arguments` as `**kwargs`. For LLM entries (`kind == "llm"`): create a scorer that renders the Jinja2 template with `arguments` and `outputs`, sends to the LLM (using `jc.model` or `config.models.judge`), and parses the JSON response into `(bool, str)`. Return the wrapped scorer in the `(name, scorer, condition, judge_type)` 4-tuple format. Also extend `_load_code_judge` to pass `arguments` as `**kwargs` when present, `_load_llm_judge` to apply Jinja2 rendering with `arguments` when present, and `_make_inline_check` to inject `arguments` into eval locals.
- [ ] T006 Add Jinja2 template rendering utility in `agent_eval/judges/__init__.py` (or a separate `agent_eval/judges/rendering.py`). Takes a `.md` file path (or prompt string), renders with `arguments` (dict) and `outputs` (dict) as template variables. Standard Jinja2 `Environment` with `tojson` filter available. Returns the rendered prompt string. Used by both builtin LLM judges and inline `prompt_file` judges.

**Checkpoint**: Foundation ready. The harness can resolve `builtin` judges (both Python and LLM) from the registry but no actual judges exist yet.

---

## Phase 3: User Story 1 - Use a Built-in Judge in eval.yaml (Priority: P1) MVP

**Goal**: Skill authors can reference built-in judges by name in eval.yaml and the harness executes them during scoring.

**Independent Test**: Add `builtin` judge entries to an eval.yaml, run scoring against synthetic case records, verify judges execute and produce pass/fail results. Verify unknown names produce clear error messages listing available judges.

### Implementation for User Story 1

- [ ] T007 [P] [US1] Implement `no_harmful_content` LLM judge in `agent_eval/judges/safety/no_harmful_content.md`. YAML frontmatter with `__version__: "1.0"`. Markdown preamble describing the check, required fields (`conversation`, `files`), and failure meaning. Jinja2 template body that evaluates conversation and file contents for harmful or dangerous content via nuanced LLM analysis. Supports `{{ arguments.categories }}` for customizable content categories. Instructs the LLM to respond with `{"passed": true/false, "rationale": "..."}`.
- [ ] T008 [P] [US1] Implement `tool_call_validation` judge in `agent_eval/judges/process/tool_call_validation.py`. Module docstring describing the check, required fields (`tool_calls`, `events`), and failure meaning. `__version__ = "1.0"`. Function `judge(outputs, **kwargs)` checks `outputs.get("tool_calls", [])` for tool calls with error results or missing responses. Returns `(True, "All N tool calls completed successfully")` or `(False, "Tool call errors: <detail>")`. Returns `(True, "No tool calls to validate")` when tool_calls is empty.
- [ ] T009 [P] [US1] Implement `cost_budget` judge in `agent_eval/judges/efficiency/cost_budget.py`. Module docstring describing the check, required fields (`cost_usd`), and failure meaning. `__version__ = "1.0"`. Function `judge(outputs, **kwargs)` reads `outputs.get("cost_usd")` and compares against `kwargs.get("max_cost_usd", 1.0)`. Returns `(True, "Cost $X.XX within budget $Y.YY")` or `(False, "Cost $X.XX exceeds budget $Y.YY")`. Returns `(False, "No cost data available")` when `cost_usd` is missing or None.
- [ ] T010 [P] [US1] Implement `output_completeness` LLM judge in `agent_eval/judges/quality/output_completeness.md`. YAML frontmatter with `__version__: "1.0"`. Markdown preamble describing what it evaluates, required fields, and failure meaning. Jinja2 template body that evaluates output completeness. Supports `{{ arguments.strictness | default('medium') }}` and optional `{{ arguments.criteria }}` list. Instructs the LLM to respond with `{"passed": true/false, "rationale": "..."}`.
- [ ] T011 [US1] Write unit tests in `tests/test_builtin_judges.py`. Test each Python judge (tool_call_validation, cost_budget) with: passing case record, failing case record, missing/empty data case, and `**kwargs` behavior (for cost_budget). Test both LLM judge templates (no_harmful_content, output_completeness) for correct rendering (mock the LLM call, verify template renders correctly with arguments/outputs). Use synthetic case record dicts, no external dependencies.
- [ ] T012 [US1] Write unit tests in `tests/test_judge_registry.py`. Test `BuiltinJudgeRegistry`: successful discovery of all four judges (3 Python + 1 LLM), `get()` returns correct entry with right `kind`, `get()` with unknown name raises `ValueError` with available names listed, `list_names()` returns sorted list. Test name collision detection by mocking a duplicate module. Test `.md` file detection as LLM type.
- [ ] T013 [US1] Write integration test in `tests/test_score_builtin.py`. Test `load_judges()` with a `JudgeConfig` having `builtin="cost_budget"` and an arguments dict. Verify the returned scorer callable accepts `outputs` kwarg and returns the expected `(bool, str)` tuple. Test FQN resolution (`builtin="efficiency/cost_budget"`). Test that unknown builtin name raises `ValueError`. Test mutual exclusivity validation (builtin + check raises error). Test that `arguments` is passed as `**kwargs` to Python judges and as Jinja variables to LLM judges.

**Checkpoint**: User Story 1 complete. Skill authors can add `builtin` judges to eval.yaml and the harness resolves, executes, and reports results.

---

## Phase 4: User Story 2 - Browse Available Judges (Priority: P1)

**Goal**: Skill authors can discover available built-in judges by browsing the judges directory and reading docstrings.

**Independent Test**: List the `agent_eval/judges/` directory tree, read any judge file, confirm the documentation describes the check, required fields, and failure meaning. Verify category subdirectory organization.

### Implementation for User Story 2

- [ ] T014 [US2] Verify all four judge files (T007-T010) have complete documentation following the convention: what it checks, what `outputs` fields it reads (with field names), and what a failure means. Python judges: module-level docstrings. LLM judges: Markdown preamble. Ensure `__version__` is present in each. Fix any that don't meet the standard.

**Checkpoint**: User Story 2 complete. This is primarily a documentation/quality gate on the judge files created in US1. The directory structure and documentation convention enable browsing.

---

## Phase 5: User Story 3 - Vendor a Library Judge for Customization (Priority: P2)

**Goal**: Skill authors can copy a built-in judge to their project, modify it, and reference it using the standard field for its type.

**Independent Test**: Copy `cost_budget.py` to a local directory, change the default threshold, reference via `module`/`function` in eval.yaml, run scoring, verify the modified threshold applies. Copy `output_completeness.md` locally, modify, reference via `prompt_file`, verify modified behavior.

### Implementation for User Story 3

- [ ] T015 [US3] Verify vendoring works for both judge types. For Python: a copied judge file referenced via `module`/`function` should work without any code changes. For LLM: a copied `.md` file referenced via `prompt_file` should work. Write tests in `tests/test_score_builtin.py` that exercise `load_judges()` with: (a) a `JudgeConfig` using `module`/`function` pointing to a vendored Python judge copy, (b) a `JudgeConfig` using `prompt_file` pointing to a vendored LLM judge copy. Verify that vendored judge names can shadow builtin names without conflict.

**Checkpoint**: User Story 3 complete. Vendoring is a documentation pattern. Python builtins vendor to `module`/`function`, LLM builtins vendor to `prompt_file`.

---

## Phase 6: User Story 4 - Library Judges in Score Reports (Priority: P2)

**Goal**: Score reports visually distinguish built-in judges from custom judges.

**Independent Test**: Run scoring with both `builtin` and inline `check` judges, generate the HTML report, verify built-in judges show "builtin" type label in the scoring summary table.

### Implementation for User Story 4

- [ ] T016 [US4] Extend judge type metadata passing in `skills/eval-run/scripts/score.py`. Change `load_judges()` to return 4-tuples `(name, scorer, condition, judge_type)` where `judge_type` is a string: `"builtin"`, `"check"`, `"llm"`, or `"code"`. Update `score_cases()` to carry `judge_type` through to the per-case results dict (add `"judge_type"` key alongside `"value"` and `"rationale"`). Update the aggregated results similarly.
- [ ] T017 [US4] Update `_render_scoring_summary` in `skills/eval-run/scripts/report.py` to detect builtin judge type. In the Type column, display "builtin" (instead of "code" or "check") when the judge was resolved from the builtin registry. This distinguishes library guardrail results from skill-specific judges in the scoring summary table.
- [ ] T018 [US4] Write a test in `tests/test_score_builtin.py` that verifies the judge type metadata flows through `load_judges()` and is available for report rendering. Test that a builtin judge's type information is distinguishable from an inline check or code judge.

**Checkpoint**: User Story 4 complete. Score reports clearly label built-in judges.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Final validation and cleanup

- [ ] T019 Run all tests with `python3 -m pytest tests/ -v` and fix any failures
- [ ] T020 Run quickstart.md validation: verify the eval.yaml examples from `specs/004-reusable-judges-library/quickstart.md` are syntactically correct, use the `builtin` field (not `type: builtin`), and reference valid judge names

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies
- **Foundational (Phase 2)**: Depends on Phase 1. T003 depends on T001 (package must exist). T004 and T005 depend on T002 (JudgeConfig fields). T005 depends on T003 (registry must exist). T006 can run in parallel with T004/T005.
- **User Story 1 (Phase 3)**: Depends on Phase 2 completion. T007, T008, T009, T010 can run in parallel. T011-T013 depend on T007-T010.
- **User Story 2 (Phase 4)**: Depends on T007-T010 (judge files must exist to verify docs)
- **User Story 3 (Phase 5)**: Depends on Phase 2 (code judge loading must work) and T007-T010 (judges to vendor)
- **User Story 4 (Phase 6)**: Depends on Phase 2 (builtin resolution) and T007-T010 (judges to test with). T017 depends on T016. T018 depends on T016.
- **Polish (Phase 7)**: Depends on all previous phases

### Parallel Opportunities

- T007, T008, T009, T010 can all run in parallel (separate files, no dependencies between judges)
- T006 (Jinja rendering) can run in parallel with T004 (duplicate validation)
- US3 and US4 can run in parallel after US1 completes (independent concerns)
- T016 and T017 touch different files and can run in parallel

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (package structure)
2. Complete Phase 2: Foundational (JudgeConfig, registry, scoring pipeline, Jinja rendering)
3. Complete Phase 3: User Story 1 (four judges + tests)
4. **STOP and VALIDATE**: Run `python3 -m pytest tests/ -v`, verify all judges pass/fail correctly
5. Feature is usable at this point

### Incremental Delivery

1. Setup + Foundational -> Infrastructure ready
2. User Story 1 -> Judges work in eval.yaml (MVP!)
3. User Story 2 -> Documentation verified (browsability)
4. User Story 3 -> Vendoring pattern validated (both Python and LLM)
5. User Story 4 -> Report labeling complete
6. Polish -> Full test suite green, quickstart validated

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story
- Total tasks: 20
- US1: 7 tasks (2 Python + 2 LLM judges + 3 test files)
- US2: 1 task (documentation verification)
- US3: 1 task (vendoring validation)
- US4: 3 tasks (metadata, report, test)
- Setup: 1 task, Foundational: 5 tasks, Polish: 2 tasks
