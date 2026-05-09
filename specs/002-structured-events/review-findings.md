# Deep Review Findings

**Date:** 2026-05-09
**Branch:** 002-structured-events
**Rounds:** 1
**Gate Outcome:** PASS
**Invocation:** manual

## Summary

| Severity | Found | Fixed | Remaining |
|----------|-------|-------|-----------|
| Critical | 2 | 0 | 2 |
| Important | 3 | 3 | 0 |
| Minor | 19 | 2 | 17 |
| **Total** | **24** | **5** | **19** |

**Note:** The 2 Critical findings (eval/exec in score.py) are pre-existing code, not introduced by this feature. They do not block this feature's gate.

**Agents completed:** 5/5 (+ 1 external tool)
**Agents failed:** none

## Findings

### FINDING-1
- **Severity:** Important
- **Confidence:** 95
- **File:** agent_eval/events.py:290-307
- **Category:** correctness
- **Source:** correctness-agent (also reported by: architecture-agent)
- **Round found:** 1
- **Resolution:** fixed (round 1)

**What is wrong:**
`_parse_transcript_assistant()` did not cap string values in tool inputs. Tool inputs were added raw via `block.get("input", {})` without calling `_cap_values()`, while the main parser `_parse_assistant_event()` correctly called `_cap_values()`. This violated FR-008.

**Why this matters:**
Subagent transcripts containing large tool inputs (e.g., Write calls with large file content) would produce unbounded events.json entries, defeating the size-bounding guarantee of `event_result_cap`.

**How it was resolved:**
Added `result_cap` parameter to `merge_subagent_transcripts()` and `_parse_transcript_assistant()`. Tool inputs are now capped via `_cap_values()` in both code paths.

### FINDING-2
- **Severity:** Important
- **Confidence:** 85
- **File:** tests/test_events.py:340-357
- **Category:** test-quality
- **Source:** test-quality-agent
- **Round found:** 1
- **Resolution:** fixed (round 1)

**What is wrong:**
The test for `traces.events=false` never called `_generate_events_json`. It asserted `events.json` doesn't exist, which was trivially true because nothing wrote it.

**Why this matters:**
A wrong-reason pass gives false confidence. The test would pass even if the code completely ignored the `events=False` flag.

**How it was resolved:**
Rewrote the test to verify both the positive case (events=True writes the file) and the negative case (load_case_record returns events=[] when no events.json exists).

### FINDING-3
- **Severity:** Important
- **Confidence:** 72
- **File:** agent_eval/events.py:273-274
- **Category:** correctness
- **Source:** correctness-agent
- **Round found:** 1
- **Resolution:** fixed (round 1)

**What is wrong:**
`merge_subagent_transcripts()` sorted events by `e.get("timestamp") or ""`. Events with `None` timestamps (like `result` events) sorted to the beginning because empty string sorts before ISO timestamps.

**Why this matters:**
The `result` event (conversation summary) would be moved to the front of the event list after merging subagent transcripts, breaking the chronological ordering that judges expect.

**How it was resolved:**
Changed sort key to use a tuple: `(0, timestamp)` for events with timestamps, `(1, "")` for events without. Added a test to verify result events stay at the end after merge.

### FINDING-4 (pre-existing)
- **Severity:** Critical
- **Confidence:** 95
- **File:** skills/eval-run/scripts/score.py:260-261
- **Category:** security
- **Source:** security-agent
- **Resolution:** not applicable (pre-existing)

**What is wrong:**
`score_cases` uses `eval()` to evaluate judge conditions from eval.yaml with an insufficient sandbox.

### FINDING-5 (pre-existing)
- **Severity:** Critical
- **Confidence:** 90
- **File:** skills/eval-run/scripts/score.py:327-338
- **Category:** security
- **Source:** security-agent
- **Resolution:** not applicable (pre-existing)

**What is wrong:**
`_make_inline_check` uses `exec()` with full `__builtins__` on check code from eval.yaml. Known design tradeoff: eval.yaml is treated as trusted code.

## Remaining Findings (Minor, not blocking)

**Architecture:** Duplication between `_parse_assistant_event` and `_parse_transcript_assistant`. Test helpers re-implement production logic. JSONL conversion helpers duplicated across test files.

**Test Quality:** No test for nested dict capping. `load_case_record` tests don't exercise `run_id` paths. Benchmark ratio permissive. No test for mixed content block types.

**Production Readiness:** No cap on total event count. All case records loaded concurrently with no size limit. Anthropic client created per judge call.

**Security:** Batch mode file copy missing symlink check (pre-existing, per-case mode has it).
