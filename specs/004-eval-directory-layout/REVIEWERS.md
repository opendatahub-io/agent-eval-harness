# Review Guide: Flexible Eval Directory Layout

**Generated**: 2026-05-30 | **Spec**: [spec.md](spec.md)

## Why This Change

Right now, `/eval-analyze` drops `eval.yaml` and `eval.md` at the project root. That works for single-skill projects, but plugin projects with multiple skills (like rfe-creator with 7+ skills) end up with config collisions. There's no clean way to isolate datasets or run results per skill. Each skill needs its own eval config with different test cases, judges, and thresholds.

## What Changes

`/eval-analyze` now asks which directory layout convention you prefer when scaffolding eval configs, with per-skill nested (`eval/<skill>/eval.yaml`) as the recommended default. The choice is remembered project-wide so you're only asked once. All eval commands (`/eval-run`, `/eval-dataset`, `/eval-optimize`, `/eval-review`, `/eval-mlflow`) gain auto-discovery that finds configs regardless of which layout was chosen. Existing root-level `eval.yaml` files get a deprecation warning with a migration offer. Run results are isolated per skill under `$AGENT_EVAL_RUNS_DIR/<skill-name>/`. No breaking changes: root-level configs continue to work if migration is declined.

## How It Works

The core change is adding a `config_dir` field to `EvalConfig` (set from the config file's parent during `from_yaml()`). All relative path resolution shifts from `Path.cwd()` to `config_dir`, which is the foundation everything else builds on.

A `discover_configs()` function in `agent_eval/config.py` scans three patterns: `eval/*/eval.yaml` (nested), `eval/*.yaml` (flat), and root `eval.yaml` (deprecated). It returns `DiscoveryResult` objects with the config path, skill name (read from the YAML's `skill` field), and deprecation flag.

Convention persistence is a single-line text file at `eval/.eval-convention` (either `nested` or `flat`), read/written by two helper functions.

Migration logic lives in `agent_eval/migrate.py`, handling file moves and path reference updates in `dataset.path` and `outputs[].path`.

The SKILL.md files for all 7 eval skills get updated instructions to use discovery instead of defaulting to `eval.yaml`.

## When It Applies

**Applies when**:
- Plugin projects with multiple skills that each need independent evaluation
- Single-skill projects (works the same, just with one config under `eval/`)
- Existing projects with root-level `eval.yaml` that want to migrate

**Does not apply when**:
- Suite execution (running all discovered configs as a batch). This is a future feature per [issue #3](https://github.com/opendatahub-io/agent-eval-harness/issues/3) that builds on the discovery mechanism here.
- EvalHub provider changes. The evalhub adapter uses its own config translation and is unaffected.

## Key Decisions

1. **Layout is user-configurable, not enforced.** During scaffolding, `/eval-analyze` offers layout conventions (nested as default, flat as alternative, explicit `--config` to bypass). Feedback from Antonin Stefanutti: projects should decide their own layout; the LLM agent can be smart about discovery regardless of structure. Alternative rejected: hardcoded per-skill nested layout with no choice.

2. **Skill name derived from eval.yaml content, not file path.** The `skill` field inside the YAML is authoritative for run isolation (`$AGENT_EVAL_RUNS_DIR/<skill>/`). This works correctly even with custom `--config` paths where the filename doesn't match the skill name. Alternative rejected: deriving from directory/filename (breaks for custom paths).

3. **`AGENT_EVAL_RUNS_DIR` redefined as base path.** Instead of deprecating the env var, it becomes the parent directory under which per-skill run folders are created. Default remains `eval/runs`, actual runs at `eval/runs/<skill>/`. Alternative rejected: deprecating the env var entirely (breaks existing CI scripts).

4. **Convention stored in `eval/.eval-convention`, not a registry.** A one-line text file is the simplest format that works. No need for a full config file or registry. Scanned once during `/eval-analyze`, ignored by discovery. Alternative rejected: inferring convention from existing layout (unreliable for new projects).

5. **Discovery in shared `scripts/discover.py`.** A single CLI wrapper callable by all skills via `${CLAUDE_SKILL_DIR}/../../scripts/discover.py`, following the same pattern as the existing `scripts/ensure_deps.py`. Alternative rejected: per-skill copies of discovery logic (duplication).

6. **Flat convention uses `eval/cases/<skill>/` for datasets.** Keeps the flat convention truly flat at the config level while isolating data per skill under shared parent directories. Alternative rejected: `eval/cases-<skill>/` prefix pattern (proliferates top-level directories).

## Areas Needing Attention

- **Path resolution backward compatibility.** Changing `project_root` from `Path.cwd()` to `config_dir` could affect scripts that rely on the current behavior. The fallback to `Path.cwd()` when `config_dir` is unset mitigates this, but worth verifying no edge cases slip through.
- **SKILL.md instruction quality.** Most of the user-facing changes are in SKILL.md files (LLM-interpreted instructions, not Python code). These are harder to test mechanically and depend on the LLM following instructions correctly.
- **Mixed convention discovery.** A project could end up with configs in both nested and flat layouts. The discovery function handles this, but the UX of presenting mixed results to the user hasn't been deeply explored.

## Open Questions

No open questions identified. All critical decisions were resolved during the clarification sessions (2026-05-28 and 2026-05-29).

## Review Checklist

- [ ] Key decisions are justified
- [ ] Breaking changes are documented with migration guidance
- [ ] Scope matches the stated boundaries
- [ ] Success criteria are achievable
- [ ] No unstated assumptions
- [ ] Path resolution changes are backward compatible (root-level configs still work)
- [ ] Discovery patterns cover all supported layouts
- [ ] Convention persistence doesn't interfere with git workflows

---

<!-- Code phase sections are appended below this line by the phase-manager command -->
