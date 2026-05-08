# Brainstorm: Structured Event Stream for Judges

**Date:** 2026-05-06
**Status:** active

## Problem Framing

The eval harness has four independent JSONL parsers that all iterate the same `stdout.log` stream, each extracting different data:

| Parser | Location | What it extracts | When it runs |
|--------|----------|-----------------|-------------|
| `extract_usage()` | `stream_capture.py` | Tokens, cost, turns, models | Execution time |
| `_extract_tool_calls()` | `score.py` | Tool use blocks matching patterns | Scoring time (once per case) |
| `_extract_assistant_text()` | `score.py` | Top-level assistant text | Scoring time (per judge using `{{ stdout }}`) |
| `build_trace()` | `trace_builder.py` | Full hierarchical event tree | MLflow logging |

This redundancy creates three problems:

1. **Repeated parsing**: the same JSONL gets parsed multiple times per case, and `_extract_assistant_text()` runs once per LLM judge.
2. **Limited judge capabilities**: judges can only evaluate final outputs (files, extracted text). They cannot evaluate the skill's process: tool selection quality, reasoning chain, efficiency, or behavioral patterns.
3. **No structured interface**: judges wanting tool call timelines or conversation flow must parse raw JSONL themselves, leading to ad-hoc extraction code in check judges.

This feature was originally scoped in the project's CLAUDE.md as remaining work: "`traces.events` implementation: parse stream-json into structured `outputs["events"]` for judges."

Context from Antonin (repo creator): "The initial idea was to parse all the stream-json outputs (from main and sub-agents) at collection time to make them available as structured data to the judges so they don't have to do the parsing individually multiple times."

## Approaches Considered

### A: Parse at Collection Time, Store as `events.json`

`collect.py` parses `stdout.log` and writes `events.json` per case. `load_case_record()` loads it. Simple but doesn't consolidate existing parsers.

- Pros: Clean separation, events inspectable on disk, matches Antonin's vision
- Cons: Doesn't address the 4 redundant parsers, collection and scoring coupled by schema

### B: Parse at Scoring Time, Lazy Cache in Record

`load_case_record()` parses JSONL into structured events in memory. No file written.

- Pros: Simplest change, no new file format
- Cons: Still parses at scoring time, events not inspectable on disk, not reusable outside scoring

### C: Shared Parser Module + Collection-Time Storage

New `agent_eval/events.py` module with a single `parse_stream_events()` function. Called by `collect.py` at collection time, results stored as `events.json`. All scoring consumers use the structured events from the record dict.

- Pros: Single source of truth for JSONL parsing, collection-time storage for inspection, reusable by any future consumer
- Cons: Larger scope (touches collect.py, score.py, new module), event schema becomes a contract

## Decision

**Approach C: Shared parser module + collection-time storage.**

The redundancy of 4 independent JSONL parsers is the root problem, and only a shared parser addresses it structurally. The extra scope is manageable because the parsing logic already exists across the codebase (consolidation, not invention).

### Key Design Decisions

**Events replace stdout for judges.** `record["stdout"]` is removed from the record dict. Judges access execution data exclusively through `record["events"]`. If a judge needs data that events don't expose, we extend the events schema rather than fall back to raw JSONL. No escape hatch.

**`{{ stdout }}` is deprecated and errors.** The `{{ stdout }}` template variable (added in PR #57) was a workaround for the absence of structured events. Once events land, `{{ stdout }}` should raise an error pointing judge authors to the events-based replacement (e.g., a new `{{ conversation }}` template variable or direct `outputs["events"]` access in check judges). This avoids quietly reimplementing a raw-stdout workaround on top of the structured interface.

**Raw stdout stays on disk for debugging.** `traces.stdout: true` controls whether `stdout.log` is kept on disk for human inspection. `traces.events: true` controls whether structured events are parsed and available to judges. Both can be true independently.

**Tool results included with a size cap.** Tool result content is stored in events with a generous cap (e.g., 50K chars per result). This covers all realistic judge scenarios without bloating `events.json` from pathological cases (e.g., reading a 500K file).

**Subagent events included as flat-with-tags.** All events (root + subagent) in one ordered list, subagent events tagged with `parent_tool_use_id` and `agent_id`. Subagent transcript files merged and deduplicated by message ID. This matches Claude Code's native streaming format and keeps judge iteration simple.

**`trace_builder.py` stays independent for now.** It builds MLflow-specific hierarchical spans, which is a different shape than the flat event list judges need. It could adopt the shared parser later as an optional refactor.

## Key Requirements

1. New `agent_eval/events.py` module with `parse_stream_events(stdout_text) -> list[Event]`
2. Events parsed at collection time by `collect.py`, stored as `events.json` per case
3. `load_case_record()` loads events into `record["events"]`, removes `record["stdout"]`
4. `{{ stdout }}` template variable deprecated: raises an error with migration guidance
5. New template variable (e.g., `{{ conversation }}` or `{{ events }}`) renders structured conversation from `record["events"]`
6. `_extract_tool_calls()` and `_extract_assistant_text()` in score.py replaced by lookups over `record["events"]`
6. `traces.events` config flag controls event parsing (default: true)
7. `traces.stdout` continues to control raw `stdout.log` file retention on disk
8. Tool results capped at a configurable size threshold with truncation marker
9. Event structure covers: assistant text, tool calls (name + input), tool results (content + status), timestamps

## New Judge Patterns This Enables

- **Process quality**: Did the skill read inputs before generating output? Did it thrash (many edits to the same file)?
- **Cost/efficiency**: Cost per useful action, retry ratio, permission denial rate
- **Safety/compliance**: Did the skill attempt file access outside workspace? Destructive commands?
- **Regression fingerprinting**: Behavioral drift detection (tool call count changes, workflow order shifts) across runs
- **Enhanced pairwise**: Compare not just outputs but efficiency and approach between runs

## Open Questions

- Exact event schema (field names, nesting, type discriminators) to be defined during specification
- Whether `extract_usage()` in `stream_capture.py` should also adopt the shared parser (it runs at execution time, before collection)
- Default value for `traces.events` (should it be true by default, replacing the current false?)
- Migration path for existing eval.yaml configs that have check judges parsing `outputs["stdout"]` directly
- Name for the new template variable replacing `{{ stdout }}` (candidates: `{{ conversation }}`, `{{ events }}`, `{{ transcript }}`)
