# Review Guide: Flexible Eval Directory Layout

**Generated**: 2026-05-30 | **Spec**: [spec.md](spec.md)

## Why This Change

Right now, `/eval-analyze` drops `eval.yaml` and `eval.md` at the project root. That works for single-eval projects, but projects with multiple eval targets (like rfe-creator with 7+ skills) end up with config collisions. There's no clean way to isolate run results per eval target.

## What Changes

`/eval-analyze` now adapts its behavior based on project complexity. For the first eval in a project, it creates `eval.yaml` at the root (zero friction). When a second eval is added, it detects the conflict and offers to reorganize into an `eval/` directory structure. All eval commands gain auto-discovery that finds configs regardless of layout. Run results are isolated per eval target under `$AGENT_EVAL_RUNS_DIR/<eval-name>/`. Root-level configs remain fully supported for single-eval projects with no deprecation warnings.

## How It Works

The core change is adding a `config_dir` field to `EvalConfig` (set from the config file's parent during `from_yaml()`). `dataset.path` resolution shifts from `Path.cwd()` to `config_dir`. Other paths (`outputs[].path`, run directories, `project_root`) remain unchanged.

A `discover_configs()` function in `agent_eval/config.py` scans three patterns: `eval/*/eval.yaml` (nested), `eval/*.yaml` (flat), and root `eval.yaml`. It returns `DiscoveryResult` objects with the config path, eval name (read from the YAML's `skill` field), and whether it's at the project root.

Layout is inferred from existing file structure (discovery patterns). No persistence file needed. In single-eval mode there's no `eval/` directory.

Reorganization logic lives in `agent_eval/reorganize.py`, handling file moves and `dataset.path` rewriting. `outputs[].path` is NOT rewritten (workspace-relative, not config-relative).

The SKILL.md files for all 7 eval skills get updated instructions to use discovery instead of defaulting to `eval.yaml`.

## When It Applies

**Applies when**:
- Projects with multiple eval targets that need independent evaluation
- Single-eval projects (works as today, root-level `eval.yaml`, no changes needed)
- Projects outgrowing root-level layout

**Does not apply when**:
- Suite execution (running all discovered configs as a batch). This is a future feature per [issue #3](https://github.com/opendatahub-io/agent-eval-harness/issues/3) that builds on the discovery mechanism here.
- EvalHub provider changes. The evalhub adapter uses its own config translation and is unaffected.

## Key Decisions

1. **Root-level is first-class, not deprecated.** Single-eval projects keep `eval.yaml` at the root with zero friction. No deprecation warnings. Reorganization only offered when a second eval creates a conflict. Feedback from Antonin Stefanutti: smart defaults based on project complexity.

2. **Datasets are independent of eval layout.** `dataset.path` is user-specified and can point anywhere, including shared locations across multiple eval configs. The layout does not force per-eval dataset directories. Feedback from Antonin Stefanutti: datasets should be defined independently of the task/agent that consumes them.

3. **Eval name derived from eval.yaml content, not file path.** The `skill` field inside the YAML is authoritative for run isolation (`$AGENT_EVAL_RUNS_DIR/<eval-name>/`). The field is called `skill` for backward compatibility but serves as the eval identifier for any target type (including prompt-based evals per issue #77).

4. **`AGENT_EVAL_RUNS_DIR` redefined as base path.** Instead of deprecating the env var, it becomes the parent directory under which per-eval run folders are created. Default remains `eval/runs`, actual runs at `eval/runs/<eval-name>/`.

5. **Layout inferred, not persisted.** No `.eval-layout` file. Layout is detected from existing file structure via discovery patterns. In single-eval mode there's no `eval/` directory, so a persistence file there can't exist anyway.

6. **Discovery in shared `scripts/discover.py`.** A single CLI wrapper callable by all skills via `${CLAUDE_SKILL_DIR}/../../scripts/discover.py`, following the same pattern as the existing `scripts/ensure_deps.py`.

## Areas Needing Attention

- **Path resolution backward compatibility.** `config_dir` is used only for `dataset.path` resolution. `project_root` remains `Path.cwd()`. When `config_dir` is unset, dataset path resolution falls back to `Path.cwd()`, preserving current behavior.
- **SKILL.md instruction quality.** Most of the user-facing changes are in SKILL.md files (LLM-interpreted instructions, not Python code). These are harder to test mechanically and depend on the LLM following instructions correctly.
- **Future eval targets beyond skills.** Issue #77 introduces prompt-based evaluation. The `skill` field in eval.yaml is reused as the eval identifier, but the spec avoids assuming all evals test skills.

## Open Questions

No open questions identified. All critical decisions were resolved during the clarification sessions (2026-05-28, 2026-05-29, and 2026-05-30).

## Review Checklist

- [ ] Key decisions are justified
- [ ] No breaking changes for single-eval projects
- [ ] Scope matches the stated boundaries
- [ ] Success criteria are achievable
- [ ] No unstated assumptions
- [ ] Path resolution changes are backward compatible (root-level configs still work)
- [ ] Discovery patterns cover all supported layouts
- [ ] No layout persistence files needed (inferred from file structure)
- [ ] Datasets are independent of eval config layout

---

<!-- Code phase sections are appended below this line by the phase-manager command -->
