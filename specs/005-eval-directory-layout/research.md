# Research: Flexible Eval Directory Layout

## Decision 1: Path Resolution Strategy

**Decision**: Track `config_dir` on `EvalConfig` and resolve `dataset.path` against it.

**Rationale**: Currently `EvalConfig.project_root` returns `Path.cwd()`, which means relative paths in `eval.yaml` resolve against wherever the user invokes the command. FR-011 requires `dataset.path` to resolve relative to the eval.yaml location. Storing the config file's parent directory on the dataclass and using it as the base for dataset path resolution is the minimal change that fixes this correctly. `project_root` remains unchanged (`Path.cwd()`) since it serves repo-level concerns (symlinks, judge modules, settings.json). `outputs[].path` is workspace-relative and is not affected.

**Alternatives considered**:
- Rewrite all paths to absolute at parse time: brittle, breaks portability of eval.yaml across machines
- Require absolute paths in eval.yaml: poor UX, breaks existing configs
- Redefine `project_root` to return `config_dir`: breaks repo-level operations (symlinks, judge loading, settings)

**Implementation**: Add a `config_dir: Optional[Path]` field to `EvalConfig`, set it from the config file's parent in `from_yaml()`. Add a `resolve_path(relative)` method that uses `config_dir` (falling back to `Path.cwd()`). `project_root` remains unchanged. All downstream scripts already receive `--config`, so they can pass the path through.

## Decision 2: Auto-Discovery Implementation

**Decision**: Create a `discover_configs()` function in `agent_eval/config.py` that scans three patterns in order: `eval/*/eval.yaml`, `eval/*.yaml`, then root `eval.yaml`.

**Rationale**: The LLM skill code (SKILL.md) currently defaults `--config` to `eval.yaml`. Instead of rewriting skill SKILL.md files, the discovery logic lives in a shared Python function that scripts call when `--config` is not explicitly provided. This keeps discovery centralized and testable.

**Alternatives considered**:
- Discovery in each SKILL.md: duplicated logic, hard to keep consistent
- A registry file listing config paths: requires maintenance, defeats the "smart discovery" goal
- Walking the entire project tree: too slow for large projects, may find unrelated YAML files

**Implementation**: `discover_configs(project_root: Path) -> list[DiscoveryResult]` returns all found configs sorted by path. Callers decide on auto-select vs. prompt behavior.

## Decision 3: Layout Inference (No Persistence File)

**Decision**: Infer the current layout from existing file structure. No persistence file needed.

**Rationale**: Discovery already scans `eval/*/eval.yaml`, `eval/*.yaml`, and root `eval.yaml`. The layout is implicit in what's on disk. In single-eval mode there's no `eval/` directory, so a persistence file there can't exist anyway. Removing the file eliminates an artifact to manage, a gitignore entry, and an error handling case. "Layout" is preferred over "convention" per reviewer feedback.

**Alternatives rejected**:
- `eval/.eval-layout` file: can't exist in single-eval mode (no `eval/` dir), adds an artifact to manage
- `.evalrc` at project root: pollutes root, unnecessary given discovery
- Store in `.specify/feature.json`: couples to speckit workflow

## Decision 4: Eval Name Derivation

**Decision**: Always read the `skill` field from the eval.yaml content. For run isolation, use this field as `<eval-name>` in `$AGENT_EVAL_RUNS_DIR/<eval-name>/`.

**Rationale**: The eval.yaml already has a mandatory `skill` field. This is authoritative regardless of where the config file lives. It also avoids issues with custom `--config` paths where the filename/directory doesn't match the eval name. The field is called `skill` for backward compatibility, but serves as the eval identifier for any eval target (including prompt-based evals per issue #77).

**Alternatives considered**:
- Derive from directory name or filename: breaks for custom paths
- Require `--skill` alongside `--config`: extra argument burden on users

## Decision 5: Reorganization Scope

**Decision**: Reorganization updates `dataset.path` only, plus moves companion files (`eval.md`, dataset directory if co-located). `outputs[].path` values are workspace-relative (resolved against the execution workspace by `workspace.py` and `collect.py`) and are NOT rewritten.

**Rationale**: FR-016 says "update internal path references." The field that contains config-relative paths is `dataset.path`. `outputs[].path` is workspace-relative (used by the runner, not resolved against config location). Other fields (`judges[].prompt_file`, `judges[].context`) reference skill-internal files, not eval artifacts, so they don't move.

**Alternatives considered**:
- Move everything: would break references to skill files
- Only move eval.yaml: leaves dangling references to old dataset locations

## Decision 6: Root-Level as First-Class

**Decision**: Root-level `eval.yaml` is a fully supported, first-class location for single-eval projects. No deprecation warnings. Reorganization into `eval/` is only offered when adding a second eval config creates a conflict.

**Rationale**: For single-eval projects (the majority), root-level is the natural, zero-friction location. Deprecation adds unnecessary complexity. The system should adapt to project complexity: simple projects stay simple, multi-eval projects get organization. Feedback from Antonin Stefanutti (PR #85 review): smart defaults based on project complexity, not forced migration.

**Alternatives considered**:
- Always deprecate root-level: adds friction for single-eval projects (majority use case)
- Always prompt for layout choice: unnecessary for single-eval projects

## Decision 7: Dataset Independence

**Decision**: Datasets are independently located via `dataset.path` in eval.yaml. The directory layout does not force per-eval dataset directories. Datasets can be shared across multiple eval configs.

**Rationale**: Datasets are not inherently per-eval. A dataset could be shared across multiple eval configs (e.g., one evaluates a skill, another evaluates a prompt-based agent, both using the same test cases). Coupling dataset location to the eval name is the wrong abstraction. Feedback from Antonin Stefanutti (PR #85 review): datasets should be defined independently of the task/agent that consumes them.

**Alternatives considered**:
- Force `eval/<name>/cases/` for nested layout: couples datasets to eval names unnecessarily
- Auto-create per-eval dataset dirs: misleads users into thinking datasets must be per-eval
