# Brainstorm: Trajectory Format Standardization

**Date:** 2026-05-13
**Status:** active
**Origin:** @astefanutti's feedback on PR #58, pointing to the [Harbor ATIF RFC](https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md)

## Problem Framing

Our `events.json` format (from brainstorm #01) is currently Claude Code-specific: flat event list, type-discriminated, subagent events tagged with `parent_tool_use_id` and `agent_id`. As we add runners beyond Claude Code (OpenCode, Agent SDK, others), each runner will produce its own raw event stream. We need a strategy for whether our internal event format stays custom or aligns with an emerging standard.

The Harbor framework has published ATIF (Agent Trajectory Interchange Format), a JSON spec for logging agent interactions across runtimes. It targets MiniSweAgent, OpenHands, Gemini CLI, and others. This is the most concrete attempt at a cross-runtime trajectory standard we've seen so far.

## Current State

Our `events.json` uses a **flat event list** with type discriminators:

```json
[
  {"type": "assistant", "message": "...", "tools": [...]},
  {"type": "tool_result", "tool_use_id": "...", "content": "..."},
  {"type": "assistant", "message": "...", "tools": [...], "parent_tool_use_id": "..."}
]
```

ATIF uses a **bundled step model** where each LLM turn groups message + tool calls + observations into a single step, with subagent trajectories as separately embedded objects.

## Key Differences

| Aspect | Our format | ATIF |
|--------|-----------|------|
| Granularity | One event per message/result | One step per LLM turn (bundles everything) |
| Subagents | Flat list, tagged with parent pointers | Separate embedded trajectory objects |
| Ordering | Array position | Explicit `step_id` ordinals |
| Tool results | Separate `tool_result` events | Nested in `observation.results` within same step |
| Metrics | Separate from events | Per-step `metrics` object |

## Approaches Considered

### A: Adopt ATIF as Internal Format

Replace `events.json` with ATIF-shaped output. All runners produce ATIF, all judges consume ATIF.

- Pros: Immediate interoperability, can ingest trajectories from other harnesses, community alignment
- Cons: ATIF's bundled-step model is a worse fit for our judges (they iterate events sequentially, checking tool order and patterns). The step grouping adds complexity for process quality judges that care about individual actions. ATIF is also early-stage with no stable release yet.

### B: ATIF as Export Format

Keep our flat event list internally, add an ATIF export adapter. Judges work on our format; external tools can consume the ATIF export.

- Pros: Best of both worlds. Judges keep the simple flat iteration they're designed for. Interoperability through export.
- Cons: Maintaining two formats. Export fidelity might drift.

### C: Convergence Layer per Runner

Each runner has its own raw format. A per-runner normalizer converts to our internal format. If ATIF matures, we add an ATIF normalizer alongside Claude Code's.

- Pros: Runner-agnostic without committing to ATIF prematurely. New runners just need a normalizer.
- Cons: Still a custom internal format. Doesn't help with importing trajectories from other harnesses.

### D: Wait and Watch

ATIF is an RFC, not a standard. Monitor adoption before investing. Keep our format, revisit when adding a second runner.

- Pros: No wasted effort if ATIF doesn't gain traction
- Cons: Risk of building more judges against a format we'll later need to change

## Preliminary Thinking

**Approach C with B as a follow-up** seems pragmatic:

1. Define a clean normalizer interface for when we add a second runner (OpenCode is the likely candidate)
2. Keep our flat event format as the internal contract for judges
3. Add ATIF export when there's a concrete consumer (e.g., sharing trajectories with another team's harness)

The flat event list is genuinely better for our judge patterns. Process quality judges check tool ordering, cost judges count errors in sequence, safety judges scan individual commands. All of these are simpler with flat iteration than with ATIF's nested step structure. We shouldn't compromise judge ergonomics for format alignment.

That said, exploring OpenCode's trajectory format early would inform whether our normalizer interface is general enough. Antonin's suggestion to "review the options" is the right first move.

## Open Questions

- What format does OpenCode use for its execution traces?
- Has anyone else adopted ATIF yet, or is it still Harbor-only?
- Do we need trajectory import (ingesting runs from other harnesses), or is export enough?
- Should the normalizer interface live in `agent_eval/events.py` or as a per-runner concern?

## Next Steps

- Investigate OpenCode's trace format as a concrete second data point
- Check ATIF adoption status (GitHub stars, issues, other implementors)
- Define the normalizer interface shape when starting on a second runner
