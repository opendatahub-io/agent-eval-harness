# Tasks: Structured Event Stream for Judges

**Input**: Design documents from `specs/002-structured-events/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md

**Tests**: Included (the eval harness has existing test infrastructure in `tests/`).

**Organization**: Tasks grouped by user story for independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Phase 1: Setup

**Purpose**: No project initialization needed. Feature addition to existing codebase.

(No setup tasks required.)

---

## Phase 2: Foundational (Shared Parser Module)

**Purpose**: Create the shared event parser that all user stories depend on.

- [ ] T001 Create `agent_eval/events.py` with `parse_stream_events(stdout_text, result_cap=50000)` function that parses JSONL into structured event dicts per the data model schema (includes subagent events with `parent_tool_use_id` and `agent_id` tags)
- [ ] T002 [P] Add `event_result_cap` field to `TracesConfig` in `agent_eval/config.py` (default: 50000) and update `traces.events` default to `true`
- [ ] T003 [P] Unit tests for `parse_stream_events()` in `tests/test_events.py`: valid JSONL with assistant text, tool calls, tool results, system/result events, timestamps, subagent events with tags

**Checkpoint**: Shared parser exists, is tested, and can convert JSONL stdout into structured events including subagent events.

---

## Phase 3: User Story 1 - Evaluate skill output through structured events (Priority: P1) MVP

**Goal**: Judges access pre-parsed structured events via `outputs["events"]` instead of raw JSONL.

**Independent Test**: Configure a check judge accessing `outputs["events"]`, run scoring against a case with JSONL stdout, verify events list contains expected tool calls and assistant text.

### Tests for User Story 1

- [ ] T004 [P] [US1] Unit test for events.json generation in `tests/test_events.py`: verify `collect.py` integration writes correct events.json from JSONL stdout
- [ ] T005 [P] [US1] Unit test for `load_case_record()` loading events from events.json in `tests/test_events.py`: verify `record["events"]` populated, `record["stdout"]` absent (KeyError)

### Implementation for User Story 1

- [ ] T006 [US1] Modify `skills/eval-run/scripts/collect.py` to call `parse_stream_events()` on `stdout.log` and write `events.json` per case (gated by `traces.events` config)
- [ ] T007 [US1] Modify `load_case_record()` in `skills/eval-run/scripts/score.py` to load `events.json` into `record["events"]` and remove `record["stdout"]` loading
- [ ] T008 [US1] Replace `_extract_tool_calls()` in `skills/eval-run/scripts/score.py` with a lookup over `record["events"]` (filter for tool_use blocks in assistant events)
- [ ] T009 [US1] Remove `_extract_assistant_text()` from `skills/eval-run/scripts/score.py` (will be replaced by `{{ conversation }}` in US2)

**Checkpoint**: `outputs["events"]` works for check judges. `outputs["stdout"]` raises KeyError. Tool calls derived from events.

---

## Phase 4: User Story 2 - Render conversation text in LLM judge prompts (Priority: P2)

**Goal**: LLM judges use `{{ conversation }}` to include assistant text from events. `{{ stdout }}` errors with migration guidance.

**Independent Test**: Configure an LLM judge with `{{ conversation }}`, run scoring, verify rendered prompt contains assistant text from events.

### Tests for User Story 2

- [ ] T010 [P] [US2] Unit test for `{{ conversation }}` rendering in `tests/test_events.py`: verify template variable replaced with concatenated root-level assistant text from events (no subagent text)
- [ ] T011 [P] [US2] Unit test for `{{ stdout }}` error in `tests/test_events.py`: verify using `{{ stdout }}` raises an error with migration message mentioning `{{ conversation }}`

### Implementation for User Story 2

- [ ] T012 [US2] Add `{{ conversation }}` rendering block in `_make_anthropic_llm_judge()` in `skills/eval-run/scripts/score.py` (extract root-level assistant text from `record["events"]`, filter out subagent events, concatenate)
- [ ] T013 [US2] Replace `{{ stdout }}` rendering block with error raise in `_make_anthropic_llm_judge()` in `skills/eval-run/scripts/score.py` (error message guides to `{{ conversation }}`)

**Checkpoint**: `{{ conversation }}` works for LLM judges, rendering root-only text. `{{ stdout }}` raises clear error.

---

## Phase 5: User Story 3 - Evaluate skill process quality (Priority: P3)

**Goal**: Check judges can inspect tool call sequences, counts, and patterns via `outputs["events"]`.

**Independent Test**: Configure a check judge that verifies tool call ordering (Read before Write) by iterating `outputs["events"]`.

### Tests for User Story 3

- [ ] T014 [P] [US3] Unit test for process quality judge pattern in `tests/test_events.py`: verify iterating events gives correct tool call sequence with names and inputs

### Implementation for User Story 3

(No additional implementation needed. US1 already provides `outputs["events"]` with tool calls. This phase validates that the event structure supports process quality patterns via tests.)

**Checkpoint**: Process quality judge pattern verified. Events contain sufficient data for tool sequence, retry count, and behavioral analysis.

---

## Phase 6: User Story 4 - Evaluate subagent behavior (Priority: P4)

**Goal**: Judges can identify and evaluate subagent events within the flat event list using `parent_tool_use_id` and `agent_id` tags.

**Independent Test**: Configure a check judge that filters `outputs["events"]` by `parent_tool_use_id` to separate root from subagent events. Run against a case with subagent activity. Verify correct separation.

### Tests for User Story 4

- [ ] T015 [P] [US4] Unit test for subagent event tagging in `tests/test_events.py`: verify events from stdout with `parent_tool_use_id` carry both `parent_tool_use_id` and `agent_id` in parsed events
- [ ] T016 [P] [US4] Unit test for transcript merging in `tests/test_events.py`: verify `subagents/*.jsonl` transcript events are merged into the event list with proper `agent_id`, deduplicated against already-streamed events by message ID
- [ ] T017 [P] [US4] Unit test for root-only filtering in `tests/test_events.py`: verify filtering events by `not e.get("parent_tool_use_id")` yields only root events

### Implementation for User Story 4

- [ ] T018 [US4] Add `merge_subagent_transcripts(events, subagent_dir)` function to `agent_eval/events.py` that reads `subagents/*.jsonl`, converts to event dicts with `agent_id`, deduplicates by message ID against existing events, and inserts in chronological order
- [ ] T019 [US4] Modify `skills/eval-run/scripts/collect.py` to call `merge_subagent_transcripts()` after `parse_stream_events()`, passing the case's subagent directory

**Checkpoint**: Judges can distinguish root from subagent events. Transcript events merged without duplicates.

---

## Phase 7: Edge Cases

**Purpose**: Verify graceful handling of edge cases identified in the spec.

- [ ] T020 [P] Unit test for empty/missing stdout.log in `tests/test_events.py`: verify `parse_stream_events("")` returns `[]`
- [ ] T021 [P] Unit test for non-JSONL content in `tests/test_events.py`: verify plain text lines are skipped, returns `[]`
- [ ] T022 [P] Unit test for tool result exceeding 50K cap in `tests/test_events.py`: verify content truncated with `"[truncated]"` marker
- [ ] T023 [P] Unit test for `traces.events: false` in `tests/test_events.py`: verify no `events.json` written, `record["events"]` is `[]`
- [ ] T024 [P] Unit test for subagent deduplication in `tests/test_events.py`: verify events streamed in stdout and also in transcript are not double-counted

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Documentation updates and regression check.

- [ ] T025 Run existing test suite (`python3 -m pytest tests/ -v`) to verify no regressions
- [ ] T026 [P] Update `skills/eval-run/references/data-pipeline.md`: document `record["events"]`, `{{ conversation }}`, subagent event tags, remove `{{ stdout }}` guidance, add events.json to pipeline flow
- [ ] T027 [P] Update `skills/eval-analyze/references/eval-yaml-template.md`: replace `{{ stdout }}` with `{{ conversation }}` in examples, add `traces.events` and `traces.event_result_cap` documentation
- [ ] T028 [P] Update `skills/eval-analyze/prompts/analyze-skill.md`: replace `{{ stdout }}` with `{{ conversation }}` in judge generation guidance
- [ ] T029 Remove `tests/test_stdout_template.py` (functionality migrated to `tests/test_events.py`)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Foundational (Phase 2)**: No dependencies, start immediately
- **US1 (Phase 3)**: Depends on T001 (parser) and T002 (config)
- **US2 (Phase 4)**: Depends on US1 (events loaded into record)
- **US3 (Phase 5)**: Depends on US1 (events available)
- **US4 (Phase 6)**: Depends on US1 (events available) and T001 (parser handles subagent tags)
- **Edge Cases (Phase 7)**: Depends on T001 (parser) and T018 (transcript merger)
- **Polish (Phase 8)**: Depends on all user stories complete

### User Story Dependencies

- **US1 (P1)**: Depends on foundational parser. Core implementation.
- **US2 (P2)**: Depends on US1. Template variable rendering.
- **US3 (P3)**: Depends on US1. Test-only verification.
- **US4 (P4)**: Depends on US1. Subagent transcript merging.

### Parallel Opportunities

- T002 and T003 can run in parallel (different files)
- T004 and T005 can run in parallel (different test functions)
- T010 and T011 can run in parallel (different test functions)
- T015, T016, T017 can all run in parallel (independent test functions)
- T020-T024 can all run in parallel (independent edge case tests)
- T026-T028 can all run in parallel (different documentation files)
- US3 and US4 can overlap after US1 (independent user stories)

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete T001-T003: parser module + config + tests
2. Complete T004-T009: events loading + tool_calls migration
3. **STOP and VALIDATE**: Run tests, verify `outputs["events"]` works
4. Complete T010-T013: `{{ conversation }}` + `{{ stdout }}` deprecation
5. Complete T014: process quality pattern validation

### Incremental Delivery

1. T001-T003 (parser) -> T006-T009 (integration) -> US1 complete, testable
2. T010-T013 (template variable) -> US2 complete
3. T014 (process quality test) -> US3 verified
4. T015-T019 (subagent events) -> US4 complete
5. T020-T024 (edge cases) -> robustness verified
6. T025-T029 (polish) -> feature complete

---

## Notes

- Total tasks: 29
- Foundational: 3 tasks
- US1: 6 tasks (core feature)
- US2: 4 tasks (template variable)
- US3: 1 task (verification only)
- US4: 5 tasks (subagent events)
- Edge Cases: 5 tasks
- Polish: 5 tasks
- Primary files: `agent_eval/events.py` (new), `collect.py` (modify), `score.py` (modify), `config.py` (modify)
- Test file: `tests/test_events.py` (new, replaces `tests/test_stdout_template.py`)
