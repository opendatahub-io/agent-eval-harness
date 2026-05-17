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

- [ ] T001 Create `agent_eval/judges/` package directory with `__init__.py` (empty initially), plus category subdirectories `safety/`, `process/`, `efficiency/` each with their own empty `__init__.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Extend `JudgeConfig` and build the `BuiltinJudgeRegistry` that all user stories depend on

**CRITICAL**: No user story work can begin until this phase is complete

- [ ] T002 Add `type: str = ""` and `config: dict = field(default_factory=dict)` fields to `JudgeConfig` dataclass in `agent_eval/config.py`. Ensure YAML parsing populates these fields from eval.yaml judge entries (check the `_parse_judges` or equivalent loading logic).
- [ ] T003 Implement `BuiltinJudgeRegistry` class in `agent_eval/judges/__init__.py` with three methods: `discover()` scans category subdirectories using `importlib` and `pkgutil`, imports each module, extracts the `judge` function, builds a `{name: callable}` flat map, and raises `ValueError` on name collisions across categories; `get(name)` returns the callable or raises `ValueError` listing all available names; `list_names()` returns a sorted list of registered judge names. Judge name is derived from module filename without `.py`.
- [ ] T004 Add duplicate judge name validation at the start of `load_judges()` in `skills/eval-run/scripts/score.py`. Collect all judge names from the config list, raise `ValueError` if any name appears more than once.
- [ ] T005 Add `type == "builtin"` branch to `load_judges()` in `skills/eval-run/scripts/score.py`. When a judge has `type: "builtin"`, instantiate `BuiltinJudgeRegistry`, call `discover()`, call `get(jc.name)` to get the callable, wrap it to pass `config` from `jc.config` as second argument. Return the wrapped scorer in the same `(name, scorer, condition)` tuple format as other judge types.

**Checkpoint**: Foundation ready. The harness can resolve `type: builtin` judges from the registry but no actual judges exist yet.

---

## Phase 3: User Story 1 - Use a Built-in Judge in eval.yaml (Priority: P1) MVP

**Goal**: Skill authors can reference built-in judges by name in eval.yaml and the harness executes them during scoring.

**Independent Test**: Add `type: builtin` judge entries to an eval.yaml, run scoring against synthetic case records, verify judges execute and produce pass/fail results. Verify unknown names produce clear error messages listing available judges.

### Implementation for User Story 1

- [ ] T006 [P] [US1] Implement `no_harmful_content` judge in `agent_eval/judges/safety/no_harmful_content.py`. Module docstring describing the check, required fields (`conversation`, `files`), and failure meaning. `__version__ = "1.0"`. Function `judge(outputs, config=None)` scans `outputs.get("conversation", "")` and file contents in `outputs.get("files", {})` for harmful content patterns. Returns `(True, "No harmful content detected")` or `(False, "Harmful content found: <detail>")`. Handles missing `conversation`/`files` keys gracefully.
- [ ] T007 [P] [US1] Implement `tool_call_validation` judge in `agent_eval/judges/process/tool_call_validation.py`. Module docstring describing the check, required fields (`tool_calls`, `events`), and failure meaning. `__version__ = "1.0"`. Function `judge(outputs, config=None)` checks `outputs.get("tool_calls", [])` for tool calls with error results or missing responses. Returns `(True, "All N tool calls completed successfully")` or `(False, "Tool call errors: <detail>")`. Returns `(True, "No tool calls to validate")` when tool_calls is empty.
- [ ] T008 [P] [US1] Implement `cost_budget` judge in `agent_eval/judges/efficiency/cost_budget.py`. Module docstring describing the check, required fields (`cost_usd`), and failure meaning. `__version__ = "1.0"`. Function `judge(outputs, config=None)` reads `outputs.get("cost_usd")` and compares against `config.get("max_cost_usd", 1.0)` if config provided, else default `1.0`. Returns `(True, "Cost $X.XX within budget $Y.YY")` or `(False, "Cost $X.XX exceeds budget $Y.YY")`. Returns `(False, "No cost data available")` when `cost_usd` is missing or None.
- [ ] T009 [US1] Write unit tests in `tests/test_builtin_judges.py`. Test each judge (no_harmful_content, tool_call_validation, cost_budget) with: passing case record, failing case record, missing/empty data case, and config parameter behavior (for cost_budget). Use synthetic case record dicts, no external dependencies.
- [ ] T010 [US1] Write unit tests in `tests/test_judge_registry.py`. Test `BuiltinJudgeRegistry`: successful discovery of all three judges, `get()` returns callable, `get()` with unknown name raises `ValueError` with available names listed, `list_names()` returns sorted list. Also test name collision detection by mocking a duplicate module.
- [ ] T011 [US1] Write integration test in `tests/test_score_builtin.py`. Test `load_judges()` with a `JudgeConfig` having `type="builtin"` and `name="cost_budget"` with a config dict. Verify the returned scorer callable accepts `outputs` kwarg and returns the expected `(bool, str)` tuple. Test that unknown builtin name raises `ValueError`.

**Checkpoint**: User Story 1 complete. Skill authors can add `type: builtin` judges to eval.yaml and the harness resolves, executes, and reports results.

---

## Phase 4: User Story 2 - Browse Available Judges (Priority: P1)

**Goal**: Skill authors can discover available built-in judges by browsing the judges directory and reading docstrings.

**Independent Test**: List the `agent_eval/judges/` directory tree, read any judge file, confirm the docstring describes the check, required fields, and failure meaning. Verify category subdirectory organization.

### Implementation for User Story 2

- [ ] T012 [US2] Verify all three judge modules (T006-T008) have complete module-level docstrings following the convention: what it checks, what `outputs` fields it reads (with field names), and what a failure means. Fix any that don't meet the standard. Ensure `__version__` is present in each.

**Checkpoint**: User Story 2 complete. This is primarily a documentation/quality gate on the judge files created in US1. The directory structure and docstring convention enable browsing.

---

## Phase 5: User Story 3 - Vendor a Library Judge for Customization (Priority: P2)

**Goal**: Skill authors can copy a built-in judge to their project, modify it, and reference it as a standard `module`/`function` judge.

**Independent Test**: Copy `cost_budget.py` to a local directory, change the default threshold, reference via `module`/`function` in eval.yaml, run scoring, verify the modified threshold applies.

### Implementation for User Story 3

- [ ] T013 [US3] Verify vendoring works with existing `_load_code_judge` in `skills/eval-run/scripts/score.py`. A copied judge file referenced via `module`/`function` should work without any code changes since judges use the standard function signature. Write a test in `tests/test_score_builtin.py` that imports a judge module directly and calls its `judge` function with custom config to confirm the vendoring pattern works.

**Checkpoint**: User Story 3 complete. Vendoring is a documentation pattern, not a code feature. The existing `module`/`function` judge type already handles it.

---

## Phase 6: User Story 4 - Library Judges in Score Reports (Priority: P2)

**Goal**: Score reports visually distinguish built-in judges from custom judges.

**Independent Test**: Run scoring with both `type: builtin` and inline `check` judges, generate the HTML report, verify built-in judges show "builtin" type label in the scoring summary table.

### Implementation for User Story 4

- [ ] T014 [US4] Extend judge type metadata passing in `skills/eval-run/scripts/score.py`. Change `load_judges()` to return 4-tuples `(name, scorer, condition, judge_type)` where `judge_type` is a string: `"builtin"`, `"check"`, `"llm"`, or `"code"`. Update `score_cases()` to carry `judge_type` through to the per-case results dict (add `"judge_type"` key alongside `"value"` and `"rationale"`). Update the aggregated results similarly. This makes judge type available to the report renderer without changing the scorer callable contract.
- [ ] T015 [US4] Update `_render_scoring_summary` in `skills/eval-run/scripts/report.py` to detect builtin judge type. In the Type column, display "builtin" (instead of "code" or "check") when the judge was resolved from the builtin registry. This distinguishes library guardrail results from skill-specific judges in the scoring summary table.
- [ ] T016 [US4] Write a test in `tests/test_score_builtin.py` that verifies the judge type metadata flows through `load_judges()` and is available for report rendering. Test that a builtin judge's type information is distinguishable from an inline check or code judge.

**Checkpoint**: User Story 4 complete. Score reports clearly label built-in judges.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Final validation and cleanup

- [ ] T017 Run all tests with `python3 -m pytest tests/ -v` and fix any failures
- [ ] T018 Run quickstart.md validation: verify the eval.yaml examples from `specs/004-reusable-judges-library/quickstart.md` are syntactically correct and reference valid judge names

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies
- **Foundational (Phase 2)**: Depends on Phase 1. T003 depends on T001 (package must exist). T004 and T005 depend on T002 (JudgeConfig fields). T005 depends on T003 (registry must exist).
- **User Story 1 (Phase 3)**: Depends on Phase 2 completion. T006, T007, T008 can run in parallel. T009-T011 depend on T006-T008.
- **User Story 2 (Phase 4)**: Depends on T006-T008 (judge files must exist to verify docstrings)
- **User Story 3 (Phase 5)**: Depends on Phase 2 (code judge loading must work) and T006-T008 (judges to vendor)
- **User Story 4 (Phase 6)**: Depends on Phase 2 (builtin resolution) and T006-T008 (judges to test with). T015 depends on T014. T016 depends on T014.
- **Polish (Phase 7)**: Depends on all previous phases

### Parallel Opportunities

- T006, T007, T008 can all run in parallel (separate files, no dependencies between judges)
- US3 and US4 can run in parallel after US1 completes (independent concerns)
- T014 and T015 touch different files and can run in parallel

---

## Parallel Example: User Story 1 Judge Implementation

```
# Launch all three judge implementations together:
Task: "Implement no_harmful_content in agent_eval/judges/safety/no_harmful_content.py"
Task: "Implement tool_call_validation in agent_eval/judges/process/tool_call_validation.py"
Task: "Implement cost_budget in agent_eval/judges/efficiency/cost_budget.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (package structure)
2. Complete Phase 2: Foundational (JudgeConfig, registry, scoring pipeline)
3. Complete Phase 3: User Story 1 (three judges + tests)
4. **STOP and VALIDATE**: Run `python3 -m pytest tests/ -v`, verify all judges pass/fail correctly
5. Feature is usable at this point

### Incremental Delivery

1. Setup + Foundational -> Infrastructure ready
2. User Story 1 -> Judges work in eval.yaml (MVP!)
3. User Story 2 -> Docstrings verified (browsability)
4. User Story 3 -> Vendoring pattern validated
5. User Story 4 -> Report labeling complete
6. Polish -> Full test suite green, quickstart validated

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story
- Total tasks: 18
- US1: 6 tasks (3 judges + 3 test files)
- US2: 1 task (docstring verification)
- US3: 1 task (vendoring validation)
- US4: 3 tasks (metadata, report, test)
- Setup: 1 task, Foundational: 4 tasks, Polish: 2 tasks
