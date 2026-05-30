# Brainstorm Overview

Last updated: 2026-05-28

## Sessions

| # | Date | Topic | Status | Spec |
|---|------|-------|--------|------|
| - | 2026-05-05 | stdout-template-variable | spec-created | 001 |
| 01 | 2026-05-06 | structured-events | merged | 002 |
| 02 | 2026-05-06 | event-powered-judges | active | - |
| 03 | 2026-05-13 | trajectory-standardization | active | - |
| 05 | 2026-05-28 | eval-directory-layout | active | - |

Implemented brainstorms are moved to `brainstorm/attic/`.

## Open Threads
- Should the framework ship with built-in judge templates for common patterns? (from #02)
- How should regression fingerprinting integrate with thresholds? (from #02)
- Should process metrics be auto-computed from events? (from #02)
- Reusable judge library: packaging, presets, versioning (from #02)
- Trajectory format alignment with ATIF when adding a second runner (from #03)
- OpenCode trace format investigation needed (from #03)
- Should `/eval-run` without `--config` auto-select when only one eval config exists? (from #05)
- Should the migration helper move cases/ and runs/ too, or just eval.yaml and eval.md? (from #05)
- How should `harness_inventory.py` (PR #74) surface the eval directory state? (from #05)

## Parked Ideas

(none)
