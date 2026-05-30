# Brainstorm: Per-Skill Eval Directory Layout

**Date:** 2026-05-28
**Status:** active

## Problem Framing

Currently, `/eval-analyze` places `eval.yaml` and `eval.md` in the project root. For a single-skill project this is fine, but plugin projects typically contain multiple skills (e.g., eval-analyze, eval-run, eval-dataset, eval-optimize, eval-review, eval-mlflow). Each skill needs its own eval config, dataset, and run results. Root-level files don't scale to this and clutter the project.

The runs directory (`eval/runs/`) is already separate, but it's shared across all skills with no per-skill isolation.

## Approaches Considered

### A: Flat `eval/` with per-skill subdirectories

```
eval/
  <skill-name>/
    eval.yaml
    eval.md
    cases/
    runs/
```

Skill name derived from `plugin.json` name field. Each skill's eval artifacts are self-contained. A single `.gitignore` pattern (`eval/*/runs/`) covers all run output.

- Pros: Clean separation, discoverable, simple gitignore, aligns with plugin.json as source of truth
- Cons: Deeper nesting, `dataset.path` in eval.yaml needs to be relative to eval.yaml location (not project root)

### B: Root-level per-skill configs

```
eval-<skill-name>.yaml
eval-<skill-name>.md
eval/cases/<skill-name>/
eval/runs/<skill-name>/
```

- Pros: Config files stay visible at root, easy to discover
- Cons: Root clutter grows linearly with skills, no single directory to gitignore, naming convention fragile

### C: Keep current layout, use `--config` for multi-skill

Leave `eval.yaml` at root as the default for single-skill. Multi-skill projects use `--config eval/<skill>/eval.yaml` explicitly.

- Pros: No breaking change, zero migration
- Cons: No convention for multi-skill, each project invents its own layout, discoverability poor

## Decision

**Approach A**: Per-skill subdirectories under `eval/`. This provides the cleanest multi-skill story while remaining simple for single-skill projects (just one subdirectory).

## Key Requirements

- `/eval-analyze --skill X` creates `eval/X/eval.yaml` by default (skill name from `plugin.json`)
- `/eval-run` auto-discovers config at `eval/<skill>/eval.yaml` when `--config` is omitted. If multiple skills have eval configs, prompt for selection
- Dataset `path` in eval.yaml is relative to the eval.yaml location, not project root
- Runs stored in `eval/<skill>/runs/` (per-skill isolation)
- Single `.gitignore` entry: `eval/*/runs/`
- `--config` still works as escape hatch for custom locations
- Deprecation warning when root-level `eval.yaml` is detected, with suggestion to migrate to `eval/<skill-name>/`
- Auto-migration: when a root-level `eval.yaml` exists and the user runs `/eval-analyze`, offer to move it to the new location

## Relationship to PR #74 (Harness-Level Context)

PR #74 adds cross-skill analysis (`harness_context` in eval.yaml, `harness_inventory.py`). The two changes are complementary:

- **Directory layout** (this brainstorm) addresses *where* eval artifacts live on disk
- **Harness context** (PR #74) addresses *what* goes into eval.yaml (cross-skill overlap data)

They compose naturally. With per-skill directories, each `eval/<skill>/eval.yaml` carries its own `harness_context` relative to its peers. The `find_skills.py` script reads `plugin.json` for skill discovery, which works regardless of where eval.yaml lives.

The harness inventory (`harness_inventory.py`) could additionally report the eval directory layout: which skills have eval configs, which don't, and whether any root-level eval.yaml needs migration. This fits naturally into the "harness overview" step in eval-setup.

## Open Questions

- Should `/eval-run` without `--config` scan `eval/*/eval.yaml` and auto-select when only one exists, or always prompt?
- Should the migration helper move `eval/cases/` and `eval/runs/` too, or just `eval.yaml` and `eval.md`?
- How should `harness_inventory.py` (PR #74) surface the eval directory state (configured vs. unconfigured skills)?
