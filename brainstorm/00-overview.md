# Brainstorm Overview

Last updated: 2026-05-19

## Sessions

| # | Date | Topic | Status | Spec |
|---|------|-------|--------|------|
| - | 2026-05-05 | stdout-template-variable | spec-created | 001 |
| 01 | 2026-05-06 | structured-events | merged | 002 |
| 02 | 2026-05-06 | event-powered-judges | active | - |
| 03 | 2026-05-13 | trajectory-standardization | active | - |
| 04 | 2026-05-19 | builtin-field-llm-judges | active | - |

## Open Threads
- Exact event schema (field names, nesting, type discriminators) to be defined during specification (from #01)
- Whether `extract_usage()` in `stream_capture.py` should also adopt the shared parser (from #01)
- Default value for `traces.events` (should it be true by default?) (from #01)
- Migration path for existing eval.yaml configs parsing `outputs["stdout"]` directly (from #01)
- Name for the new template variable replacing `{{ stdout }}` (from #01)
- Should the framework ship with built-in judge templates for common patterns? (from #02)
- How should regression fingerprinting integrate with thresholds? (from #02)
- Should process metrics be auto-computed from events? (from #02)
- Reusable judge library: packaging, presets, versioning (from #02)
- Trajectory format alignment with ATIF when adding a second runner (from #03)
- OpenCode trace format investigation needed (from #03)
- LLM judge output parsing into (bool, str): structured output vs JSON block vs regex (from #04)
- Builtin LLM judge support for context field (from #04)
- Jinja filter/function availability for prompt templates (from #04)
- Standard preamble for LLM prompt templates (from #04)

## Parked Ideas

(none)
