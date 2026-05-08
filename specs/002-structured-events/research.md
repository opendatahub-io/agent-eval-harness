# Research: Structured Event Stream for Judges

## R1: Event schema design

**Decision**: Use a flat list of typed event dicts with a `type` discriminator field. Each event has `type`, `timestamp`, and type-specific content fields.

**Rationale**: Judges need to iterate events in order and filter by type. A flat list is simpler than a nested tree (which trace_builder uses for MLflow spans). The `type` field mirrors the stream-json format (`assistant`, `user`, `system`, `result`) so the mental model stays consistent.

**Alternatives considered**:
- Hierarchical tree (like trace_builder): too complex for judge access patterns, which are sequential iteration and filtering
- Separate lists per type (assistant_events, tool_calls, etc.): loses ordering, makes process quality judges harder

## R2: Where to parse (collection time vs scoring time)

**Decision**: Parse at collection time in `collect.py`, write `events.json` per case. `load_case_record()` loads it.

**Rationale**: Parse-once-store-once eliminates repeated parsing. The `events.json` file is inspectable on disk for debugging. Collection already handles per-case artifact management.

**Alternatives considered**:
- Scoring time (in `load_case_record()`): simpler but still parses at scoring time, not inspectable
- Execution time (in `execute.py`): too early, stdout.log isn't finalized until execution completes

## R3: Shared parser location

**Decision**: New `agent_eval/events.py` module with `parse_stream_events(stdout_text, result_cap=50000) -> list[dict]`.

**Rationale**: `agent_eval/` already contains shared modules (`config.py`, `state.py`). A dedicated events module is the natural home. The function takes raw text (not file path) so it's testable without filesystem setup.

**Alternatives considered**:
- Add to `stream_capture.py`: already handles execution-time concerns (usage extraction), mixing collection-time parsing would blur responsibilities
- Add to `score.py`: that's the consumer, not the producer. Parsing belongs in the data pipeline, not the scoring pipeline

## R4: Tool result content handling

**Decision**: Include tool result content with a 50K character default cap, configurable via `traces.event_result_cap` in eval.yaml. Truncated results get a `"[truncated]"` marker.

**Rationale**: Most tool results are small (error messages, short file reads, command outputs). The cap prevents pathological cases (e.g., Read on a 500K file) from bloating events.json while preserving all realistic judge data.

## R5: Existing parser migration

**Decision**: `_extract_tool_calls()` and `_extract_assistant_text()` in score.py become thin lookups over `record["events"]`. The raw JSONL parsing code is removed from score.py.

**Rationale**: These functions duplicate the parsing that events.py now handles. Converting them to event lookups maintains backward compatibility for `outputs["tool_calls"]` while eliminating redundant parsing. `extract_usage()` in stream_capture.py is left unchanged (it runs at execution time before collection and has different output needs).

## R6: Template variable naming

**Decision**: `{{ conversation }}` replaces `{{ stdout }}`. Using `{{ stdout }}` raises an error with migration guidance.

**Rationale**: "conversation" clearly describes what it renders (assistant conversation text). It's distinct from `{{ events }}` (which could be confused with the full event list) and from `{{ transcript }}` (which could be confused with subagent transcripts).

## R7: stdout removal from record

**Decision**: `record["stdout"]` is removed entirely. Accessing `outputs["stdout"]` in a judge raises KeyError. No deprecation grace period.

**Rationale**: A hard break forces immediate migration and prevents the raw JSONL escape hatch from undermining the structured events interface. If events are missing data, the correct fix is extending the events schema, not falling back to raw parsing.

## R8: Subagent event structure

**Decision**: Flat list with tags. All events (root + subagent) in one ordered list. Subagent events carry `parent_tool_use_id` (linking to the Agent tool call) and `agent_id` (identifying the subagent instance).

**Rationale**: Matches Claude Code's native streaming format (>= 2.1.108 streams subagent messages inline with `parent_tool_use_id`). Keeps judge iteration simple: most judges iterate all events or filter to root-only with one condition. Process quality judges can reconstruct the hierarchy from tags when needed.

**Alternatives considered**:
- Nested (subagent events inside Agent tool result): forces recursion for simple queries like "count all tool calls"
- Separate lists by agent ID: loses chronological ordering between root and subagent activity

## R9: Subagent transcript deduplication

**Decision**: Reuse the existing `seen_msg_ids` pattern from `stream_capture.py`. When merging transcript files, skip events whose message ID was already seen in the stdout stream.

**Rationale**: Claude Code >= 2.1.108 streams foreground subagent messages in stdout with `parent_tool_use_id`. The same messages appear in `subagents/*.jsonl` transcripts. Deduplication by message ID is already proven in `extract_usage()` and `count_subagent_turns()`.
