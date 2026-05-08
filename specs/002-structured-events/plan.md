# Implementation Plan: Structured Event Stream for Judges

**Branch**: `002-structured-events` | **Date**: 2026-05-08 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/002-structured-events/spec.md`

## Summary

Add a shared JSONL parser (`agent_eval/events.py`) that converts Claude Code stream-json stdout into structured events at collection time. Events are stored as `events.json` per case and loaded into `record["events"]` for judges. The event list uses a flat-with-tags structure: root and subagent events in one ordered list, subagent events tagged with `parent_tool_use_id` and `agent_id`. Subagent transcript files are merged and deduplicated.

This replaces `record["stdout"]` (removed, KeyError on access), consolidates `_extract_tool_calls()` and `_extract_assistant_text()` into event lookups, and introduces `{{ conversation }}` as the template variable for LLM judges (deprecating `{{ stdout }}` with an error).

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: json (stdlib), yaml
**Storage**: File-based (`events.json` per case in run output directory)
**Testing**: pytest (unit tests in `tests/`, e2e tests in `tests/e2e/`)
**Target Platform**: macOS/Linux CLI (Claude Code plugin)
**Project Type**: CLI tool / evaluation framework
**Performance Goals**: Event parsing adds <500ms per case
**Constraints**: No changes to `extract_usage()` or `trace_builder.py`
**Scale/Scope**: New module + modifications to collect.py and score.py

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution is not yet customized for this project (template only). No gates to evaluate. Proceeding.

## Project Structure

### Documentation (this feature)

```text
specs/002-structured-events/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0: research findings
├── data-model.md        # Phase 1: data model
├── REVIEWERS.md         # Review guide
└── checklists/
    └── requirements.md  # Quality checklist
```

### Source Code (files to modify/create)

```text
agent_eval/
├── events.py                             # NEW: shared JSONL parser + subagent transcript merger
└── config.py                             # MODIFY: add event_result_cap to TracesConfig

skills/eval-run/scripts/
├── collect.py                            # MODIFY: call parse_stream_events(), merge transcripts, write events.json
└── score.py                              # MODIFY: load events, replace extractors, add {{ conversation }}, error on {{ stdout }}

skills/eval-run/references/
└── data-pipeline.md                      # MODIFY: document events, {{ conversation }}, remove {{ stdout }} guidance

skills/eval-analyze/references/
└── eval-yaml-template.md                 # MODIFY: replace {{ stdout }} with {{ conversation }} in examples

skills/eval-analyze/prompts/
└── analyze-skill.md                      # MODIFY: update template variable guidance

tests/
└── test_events.py                        # NEW: unit tests for parser, transcript merging, template rendering
```

**Structure Decision**: No new directories beyond `agent_eval/events.py`. This is a consolidation of existing parsing logic into a shared location, plus modifications to the collection and scoring pipelines. Subagent transcript handling reuses the existing `subagents/*.jsonl` file convention.
