# Research: Flexible Eval Directory Layout

## Decision 1: Path Resolution Strategy

**Decision**: Track `config_path` on `EvalConfig` and resolve all relative paths against it.

**Rationale**: Currently `EvalConfig.project_root` returns `Path.cwd()`, which means relative paths in `eval.yaml` resolve against wherever the user invokes the command. FR-011 requires paths to resolve relative to the eval.yaml location. Storing the config file's parent directory on the dataclass and using it as the base for all relative path resolution is the minimal change that fixes this correctly.

**Alternatives considered**:
- Rewrite all paths to absolute at parse time: brittle, breaks portability of eval.yaml across machines
- Require absolute paths in eval.yaml: poor UX, breaks existing configs
- Resolve relative to a `.evalrc` or registry: over-engineered, adds a new artifact

**Implementation**: Add a `config_dir: Path` field to `EvalConfig`, set it from the config file's parent in `from_yaml()`. Update `project_root` property or add a `resolve_path(relative)` method. All downstream scripts already receive `--config`, so they can pass the path through.

## Decision 2: Auto-Discovery Implementation

**Decision**: Create a `discover_configs()` function in `agent_eval/config.py` that scans three patterns in order: `eval/*/eval.yaml`, `eval/*.yaml`, then root `eval.yaml`.

**Rationale**: The LLM skill code (SKILL.md) currently defaults `--config` to `eval.yaml`. Instead of rewriting skill SKILL.md files, the discovery logic lives in a shared Python function that scripts call when `--config` is not explicitly provided. This keeps discovery centralized and testable.

**Alternatives considered**:
- Discovery in each SKILL.md: duplicated logic, hard to keep consistent
- A registry file listing config paths: requires maintenance, defeats the "smart discovery" goal
- Walking the entire project tree: too slow for large projects, may find unrelated YAML files

**Implementation**: `discover_configs(project_root: Path) -> list[Path]` returns all found configs sorted by path. Callers decide on auto-select vs. prompt behavior.

## Decision 3: Convention Persistence

**Decision**: Store the chosen layout convention in `eval/.eval-convention` (a single-line text file containing the convention name, e.g., `nested` or `flat`).

**Rationale**: FR-006 requires the convention to persist project-wide. Using a dedicated file under `eval/` keeps it co-located with eval artifacts and avoids polluting the project root. A single-line text file is the simplest format that works.

**Alternatives considered**:
- Store in `.specify/feature.json`: couples convention to the speckit workflow, not portable
- Store in a top-level `.evalrc`: adds a dot-file to the project root
- Infer from existing layout: unreliable for new projects with no eval artifacts yet

## Decision 4: Skill Name Derivation

**Decision**: Always read the `skill` field from the eval.yaml content. For run isolation, use this field as `<skill-name>` in `$AGENT_EVAL_RUNS_DIR/<skill-name>/`.

**Rationale**: The eval.yaml already has a mandatory `skill` field. This is authoritative regardless of where the config file lives. It also avoids issues with custom `--config` paths where the filename/directory doesn't match the skill name.

**Alternatives considered**:
- Derive from directory name or filename: breaks for custom paths
- Require `--skill` alongside `--config`: extra argument burden on users

## Decision 5: Migration Scope

**Decision**: Migration updates `dataset.path` and all `outputs[].path` entries, plus moves companion files (`eval.md`, `cases/` directory, `runs/` directory).

**Rationale**: FR-016 says "update internal path references." The fields that contain relative paths are `dataset.path` and `outputs[].path`. Other fields (`judges[].prompt_file`, `judges[].context`) reference skill-internal files, not eval artifacts, so they don't move. The companion directories (`cases/`, `runs/`) are eval-specific and must follow.

**Alternatives considered**:
- Move everything: would break references to skill files
- Only move eval.yaml: leaves dangling references to old dataset/runs locations

## Decision 6: Flat Convention Dataset Path

**Decision**: Flat convention uses `eval/cases/<skill>/` for datasets and `$AGENT_EVAL_RUNS_DIR/<skill>/` for runs.

**Rationale**: Established in clarification session. Keeps the eval directory structure shallow (configs at `eval/<skill>.yaml`) while isolating data per skill using namespaced subdirectories under shared parents.
