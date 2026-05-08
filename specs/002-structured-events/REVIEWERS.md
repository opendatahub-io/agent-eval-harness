# Review Guide: Structured Event Stream for Judges

**Generated**: 2026-05-08 | **Spec**: [spec.md](spec.md)

## Why This Change

The eval harness has four independent JSONL parsers that all iterate the same `stdout.log` stream, each extracting different data at different times. This causes three problems:

1. **Repeated work**: `_extract_assistant_text()` runs once per LLM judge per case. With 5 judges and 20 cases, that's 100 redundant parses of the same file.
2. **Fragile extraction**: Each parser reimplements its own subset of JSONL field matching. Bugs get fixed in one parser but not the others.
3. **No structured access**: Judges that want to evaluate process quality (tool usage patterns, conversation flow, subagent behavior) have to write their own JSONL parsing from scratch.

The fix: parse once at collection time, produce a structured `events.json` per case, and give judges a clean `outputs["events"]` list instead of raw JSONL.

## What Changes

A single shared parser (`agent_eval/events.py`) replaces the scattered extraction logic. It runs once when `collect.py` processes results, writes `events.json` alongside existing artifacts, and loads the events into the case record for all judges.

The event list uses a **flat-with-tags structure**: root and subagent events in one ordered chronological list. Subagent events carry `parent_tool_use_id` and `agent_id` tags. Judges filter to root-only with `if not e.get("parent_tool_use_id")`. Subagent transcript files (`subagents/*.jsonl`) are merged into the event list, deduplicated by message ID against events already streamed in stdout.

For LLM judges, a new `{{ conversation }}` template variable renders root-level assistant text from events (replacing `{{ stdout }}`). Check judges access `outputs["events"]` as a typed list they can filter and iterate.

**This is a breaking change**: `outputs["stdout"]` is removed (KeyError on access). Existing judges that parse raw stdout must migrate to events.

## Key Decisions

1. **Parse at collection, not scoring**: Events are parsed once and stored as `events.json`. This eliminates repeated parsing at scoring time and makes events inspectable on disk. (Alternative: lazy parsing in `load_case_record()`, rejected because events wouldn't be inspectable.)

2. **Hard removal of `record["stdout"]`**: No deprecation grace period. KeyError forces immediate migration instead of letting judges silently keep using the raw escape hatch.

3. **Flat event list with subagent tags**: All events (root + subagent) in one chronological list, subagent events tagged with `parent_tool_use_id` and `agent_id`. Matches Claude Code's native streaming format (>= 2.1.108). (Alternatives rejected: nesting forces recursion for simple queries; separate lists by agent loses chronological ordering.)

4. **Tool results included with size cap**: 50K chars default (configurable via `traces.event_result_cap`). Judges can see what tools returned without falling back to raw JSONL.

5. **`{{ conversation }}` naming**: Describes what it renders (root-level assistant conversation text). Distinct from `{{ events }}` (full event list) and `{{ transcript }}` (could confuse with subagent transcripts).

6. **Transcript deduplication by message ID**: Reuses the existing `seen_msg_ids` pattern from `stream_capture.py`. Events from subagent transcripts that were already streamed in stdout are skipped.

## Areas Needing Attention

- **Breaking change impact**: Any check judge parsing `outputs.get("stdout", "")` will break. Error messages must include clear migration guidance.

- **Event schema as contract**: Once `events.json` is written by collect.py and consumed by score.py, the schema becomes a versioning concern. Older files from previous runs may not be compatible if the schema evolves.

- **50K tool result cap**: Generous but arbitrary. Judges evaluating large file contents could lose data beyond the cap. The configurable `event_result_cap` mitigates this, but the default could surprise users.

- **`traces.events` defaulting to `true`**: Old eval runs without `events.json` will have `record["events"]` as an empty list when re-scored. Judges should handle this gracefully.

- **Subagent deduplication edge cases**: Claude Code >= 2.1.108 streams foreground subagent messages in stdout, but background agents only appear in transcript files. The deduplication must handle both cases correctly.

## Scope Boundaries

**In scope**:
- New `agent_eval/events.py` shared parser module
- Collection-time parsing into `events.json` per case
- Subagent events in flat list with `parent_tool_use_id` and `agent_id` tags
- Subagent transcript file merging with deduplication
- Loading events into `record["events"]` for all judge types
- New `{{ conversation }}` template variable for LLM judges
- Deprecation of `{{ stdout }}` (raises error with migration guidance)
- Removal of `record["stdout"]` from the case record
- Replacement of `_extract_tool_calls()` and `_extract_assistant_text()` with event lookups
- Tool result content capping (configurable via `traces.event_result_cap`)
- `traces.events` config flag (default: true)

**Out of scope**:
- `trace_builder.py` refactoring (different shape, MLflow-specific)
- `extract_usage()` in `stream_capture.py` (runs at execution time, different lifecycle)
- Changes to raw `stdout.log` file retention on disk (stays as-is)

## Open Questions

- Migration path for existing eval.yaml configs with check judges parsing `outputs["stdout"]` directly needs documentation
- Whether `extract_usage()` should eventually adopt the shared parser (different lifecycle, runs at execution time)

## Review Checklist

- [ ] Key decisions are justified
- [ ] Breaking changes are documented with migration guidance
- [ ] Event schema is stable enough to be a contract
- [ ] Scope matches the stated boundaries
- [ ] Success criteria are achievable
- [ ] No unstated assumptions
- [ ] Backward compatibility for `outputs["tool_calls"]` is maintained
- [ ] Edge cases (empty stdout, non-JSONL, subagent filtering, deduplication) are tested
- [ ] Subagent events correctly tagged with `parent_tool_use_id` and `agent_id`
- [ ] `{{ conversation }}` renders root-only text (no subagent text)

---

<!-- Code phase sections are appended below this line by the phase-manager command -->
