# 0006: Execution Hooks

## Status

Proposed

## Problem

Eval cases often depend on resources that the harness doesn't manage today — running services, database seeding, generated fixtures, environment provisioning, or pre-/post-processing steps. Users work around this with manual scripts and env vars external to the harness:

```bash
# Current workaround — external to the harness, fragile, not reproducible
docker run -d --name eval-db -p 5432:5432 postgres:16
psql -h localhost -U eval -f seed.sql eval_db
/eval-run --config eval.yaml --model sonnet
docker rm -f eval-db
```

This has several problems:
- **Not reproducible** — setup steps live in tribal knowledge or READMEs, not in the eval config
- **Not validated** — the harness can't check that prerequisites are met before execution
- **Not cleaned up** — leaked temp files and orphaned containers accumulate
- **Not reported** — setup failures are invisible; the user sees a confusing skill failure instead

## Scope

Add **lifecycle hooks** to `eval.yaml` — user-defined shell commands that run at well-defined points in the eval pipeline. Does **not** change the runner contract, judge interface, or workspace layout.

## Design

### Hook Points

Six hook points spanning the full eval lifecycle, using conventional test-framework naming:

```
before_all
 ├─ before_each (case 1)
 │   └─ [skill execution]
 │   └─ after_each (case 1)
 ├─ before_each (case 2)
 │   └─ [skill execution]
 │   └─ after_each (case 2)
 ├─ [collection]
 ├─ before_scoring
 │   └─ [judge scoring]
 └─ after_all
```

| Hook | Runs | CWD | When | Use Case |
|------|------|-----|------|----------|
| `before_all` | Once | Project root | After workspace creation, before any case executes | Start services, extract shared archives, populate shared caches, pull OCI volumes |
| `before_each` | Per case | Case workspace | After case workspace setup, before skill execution | Extract per-case archives, seed case-specific state, configure case-specific services |
| `after_each` | Per case | Case workspace | After skill execution, before collection | Normalize outputs, capture ephemeral state (container logs, DB snapshots), clean up temp files |
| `before_scoring` | Once | Project root | After collection, before judge scoring | Aggregate cross-case data, start services needed by judges, prepare scoring context |
| `after_all` | Once | Project root | After scoring completes (or on failure) | Stop services, clean temp dirs, upload results, send notifications |

`after_all` is **guaranteed to run** even if earlier steps fail — it is a finally block for cleanup. All other hooks abort the run on failure (unless `on_failure: continue`).

### eval.yaml Schema

```yaml
hooks:
  before_all:
    - command: "scripts/start-services.sh"
      timeout: 120
      description: "Start Jira emulator and seed database"
    - command: "scripts/extract-shared-archives.sh $AGENT_EVAL_WORKSPACE"
      timeout: 60

  before_each:
    - command: |
        archive="$CASE_SOURCE_DIR/snapshot.tar.gz"
        [ -f "$archive" ] && tar xzf "$archive" -C .
      timeout: 60
      description: "Extract case snapshot"

  after_each:
    - command: "docker logs eval-jira > jira-debug.log 2>&1 || true"
      timeout: 15
      on_failure: continue

  before_scoring:
    - command: "python3 scripts/aggregate-cross-case-data.py --workspace $AGENT_EVAL_WORKSPACE"
      timeout: 30

  after_all:
    - command: "scripts/teardown-services.sh"
      timeout: 30
      on_failure: continue
      description: "Stop services and clean up"
```

Each hook entry:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `command` | string | yes | — | Shell command (`bash -c`). Multi-line supported. |
| `timeout` | int | no | 120 | Max seconds before the hook is killed |
| `description` | string | no | — | Human-readable label for progress output |
| `on_failure` | enum | no | `fail` | `fail` aborts the run, `continue` logs a warning and proceeds |
| `condition` | string | no | — | Shell expression; hook runs only if this exits 0 (see Conditional Hooks) |

Hooks within a phase run sequentially in declaration order. A failing hook (with `on_failure: fail`) skips remaining hooks in that phase and aborts.

### Environment Variables

Hooks inherit the caller's full environment plus harness-injected variables:

| Variable | Available In | Value |
|----------|-------------|-------|
| `AGENT_EVAL_WORKSPACE` | all | Root workspace path (`/tmp/agent-eval/{run-id}`) |
| `AGENT_EVAL_RUN_ID` | all | Current run ID (e.g., `2026-05-31-sonnet`) |
| `AGENT_EVAL_CONFIG` | all | Absolute path to eval.yaml |
| `AGENT_EVAL_PROJECT_ROOT` | all | Project root directory |
| `AGENT_EVAL_MODEL` | all | Skill model being tested (e.g., `sonnet`) |
| `CASE_ID` | per-case hooks | Case ID (e.g., `case-001-name`) |
| `CASE_WORKSPACE` | per-case hooks | Absolute path to case workspace directory |
| `CASE_SOURCE_DIR` | per-case hooks | Absolute path to the original case directory in the dataset |
| `CASE_INPUT` | per-case hooks | Absolute path to `input.yaml` in the case workspace |

`CASE_SOURCE_DIR` is the key variable — it gives hooks access to files in the dataset case directory (like `snapshot.tar.gz`, `fixtures/`, test data) that workspace.py deliberately does not copy into the case workspace to avoid leaking annotations and gold standards to the skill.

Variables from `execution.env` are also available, resolved the same way as during skill execution (`$VAR` references resolved from the caller's environment).

### Conditional Hooks

The `condition` field enables hooks that only run when relevant:

```yaml
hooks:
  before_each:
    # Only extract if the case has a snapshot archive
    - command: "tar xzf $CASE_SOURCE_DIR/snapshot.tar.gz -C ."
      condition: "test -f $CASE_SOURCE_DIR/snapshot.tar.gz"
      description: "Extract snapshot (if present)"

    # Only seed the database if the case has seed data
    - command: "psql < $CASE_SOURCE_DIR/seed.sql"
      condition: "test -f $CASE_SOURCE_DIR/seed.sql"

  before_all:
    # Only start Docker services if Docker is available
    - command: "docker compose -f eval-services.yaml up -d"
      condition: "command -v docker"
      description: "Start eval services"
```

When `condition` is set, the harness runs `bash -c "<condition>"` first. If it exits non-zero, the hook is silently skipped (not treated as a failure). This avoids needing `[ -f ... ] && ...` guards inside every command.

### Execution Contract

1. **Hooks run in the harness process**, not inside Claude Code. They are ordinary shell commands with no access to the skill's Claude session.
2. **Hooks are blocking.** The pipeline waits for each hook to finish before proceeding to the next step.
3. **Stdout/stderr are captured** to `{run_dir}/hooks/{hook_name}[.{case_id}].log`. Hooks should not assume an interactive terminal.
4. **`on_failure: fail`** (default) aborts the run. `on_failure: continue` logs a warning and proceeds. `after_all` always uses `continue` semantics internally (guaranteed cleanup).
5. **Parallelism**: When `execution.parallelism > 1`, `before_each` and `after_each` hooks run in the case's thread — concurrent with other cases' hooks. Hooks must be safe for concurrent execution. Shared resources (ports, containers, databases) should be managed in `before_all`/`after_all` (which are single-threaded), not in per-case hooks.
6. **No harness file mutation.** Hooks must not modify `.claude/settings.json`, `batch.yaml`, `case_order.yaml`, or other harness-managed files. They may freely create, extract, or modify other files in the workspace.
7. **Timeout enforcement.** Hooks are killed (SIGTERM, then SIGKILL after 5s) if they exceed `timeout`. Timed-out hooks are treated as failures.

### Security

1. **Trust model.** Hooks in `eval.yaml` are trusted developer input — the same trust level as the `skill` field or judge definitions. The harness does not sandbox or restrict hook commands. Untrusted third parties should not be able to modify `eval.yaml` or dataset case directories.
2. **Privilege level.** Hooks run with the full permissions of the harness process. There is no privilege separation between hooks and the rest of the pipeline.
3. **Path validation.** Hook log filenames are sanitized to prevent path traversal (CWE-22). `case_id` values used in log paths are restricted to alphanumeric characters, dots, hyphens, and underscores.
4. **Data flow risks.** Hooks that execute commands against external systems (databases, APIs) using dataset files (e.g., `psql < seed.sql`) assume those files are trusted. If datasets are sourced from untrusted origins, hook authors should validate inputs or use parameterized tooling rather than passing raw files to interpreters.

### Implementation

Changes are localized to three files:

**`agent_eval/config.py`** — Add hook dataclasses to the config schema:

```python
@dataclass
class HookEntry:
    command: str
    timeout: int = 120
    description: str = ""
    on_failure: str = "fail"  # "fail" | "continue"
    condition: str = ""

@dataclass
class HooksConfig:
    before_all: list[HookEntry] = field(default_factory=list)
    before_each: list[HookEntry] = field(default_factory=list)
    after_each: list[HookEntry] = field(default_factory=list)
    before_scoring: list[HookEntry] = field(default_factory=list)
    after_all: list[HookEntry] = field(default_factory=list)
```

**`agent_eval/hooks.py`** (new) — Hook executor:

```python
def run_hooks(
    entries: list[HookEntry],
    env: dict[str, str],
    cwd: Path,
    log_dir: Path,
    phase_name: str,
    case_id: str | None = None,
) -> list[HookResult]:
    """Run hooks sequentially. Returns results. Raises on failure if on_failure=fail."""
```

**`skills/eval-run/scripts/execute.py`** — Import `run_hooks` and call at each lifecycle point. Wire `after_all` into a `try/finally` around the main execution loop.

### Interaction with Existing Features

**Tool interception (`inputs.tools`)**: Hooks and tool interception are orthogonal. Hooks prepare the environment; tool interception controls what the skill can do during execution. A common pattern is `before_all` starts a service, `inputs.tools` intercepts the skill's HTTP calls to that service.

**`execution.env`**: Hook-injected env vars are available to hooks via the standard environment. Hooks cannot inject new env vars into the skill's session (that would require modifying `.claude/settings.json`, which is prohibited). Use `execution.env` for skill-visible variables.

**Workspace symlinks**: Hooks run after workspace setup, so project symlinks are already in place. Hooks can rely on symlinked resources (e.g., `scripts/` symlink) being available.

## Examples

### Compressed Artifacts (Motivating Case)

A payload analysis eval stores snapshot data as tar.gz to keep git lean:

```yaml
hooks:
  before_each:
    - command: |
        mkdir -p snapshot
        tar xzf "$CASE_SOURCE_DIR/snapshot.tar.gz" -C snapshot
      condition: "test -f $CASE_SOURCE_DIR/snapshot.tar.gz"
      timeout: 60
      description: "Extract payload snapshot"

execution:
  arguments: "{payload_tag} --snapshot-dir snapshot"
```

### OCI Image Volumes

Eval data stored as OCI images on a registry:

```yaml
hooks:
  before_all:
    - command: |
        mkdir -p "$AGENT_EVAL_WORKSPACE/archives"
        skopeo copy docker://quay.io/org/eval-data:latest \
          dir:"$AGENT_EVAL_WORKSPACE/archives"
      timeout: 300
      description: "Pull eval data from registry"

execution:
  env:
    EVAL_ARCHIVES_DIR: $AGENT_EVAL_WORKSPACE/archives
```

### Service Lifecycle (Database + Emulator)

```yaml
hooks:
  before_all:
    - command: |
        docker compose -f evals/services.yaml up -d
        timeout 30 bash -c 'until curl -sf localhost:8080/health; do sleep 1; done'
      timeout: 60
      description: "Start Jira emulator and Postgres"

  before_each:
    - command: "psql -h localhost -U eval -f $CASE_SOURCE_DIR/seed.sql eval_db"
      condition: "test -f $CASE_SOURCE_DIR/seed.sql"
      timeout: 15
      description: "Seed database for case"

  after_each:
    - command: "psql -h localhost -U eval -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' eval_db"
      timeout: 10
      on_failure: continue
      description: "Reset database between cases"

  after_all:
    - command: "docker compose -f evals/services.yaml down -v"
      timeout: 30
      on_failure: continue
      description: "Tear down services"
```

> **Note:** Database seeding from case files (`seed.sql`) assumes the dataset is trusted. If datasets are sourced externally, validate SQL files or use a parameterized seeding script rather than raw `psql`.

### Network Shims (PATH Manipulation)

Replace real CLI tools with shims that serve from local archives:

```yaml
hooks:
  before_each:
    - command: |
        mkdir -p .shims
        cp "$AGENT_EVAL_PROJECT_ROOT/evals/shims/"* .shims/
        chmod +x .shims/*
        # Prepend shims to PATH in the skill's env
        echo "PATH=.shims:$PATH" >> .env.local
      timeout: 10
      description: "Install CLI shims for hermetic execution"
```

### Generated Fixtures

Derive test inputs from a template + case-specific parameters:

```yaml
hooks:
  before_each:
    - command: |
        python3 "$AGENT_EVAL_PROJECT_ROOT/evals/scripts/render-fixture.py" \
          --template "$AGENT_EVAL_PROJECT_ROOT/evals/templates/cluster.yaml.j2" \
          --params input.yaml \
          --output cluster-state.yaml
      timeout: 30
      description: "Generate cluster fixture from template"
```

### Cross-Case Aggregation Before Scoring

Compute derived data that judges need but that spans multiple cases:

```yaml
hooks:
  before_scoring:
    - command: |
        python3 scripts/compute-cross-case-baselines.py \
          --workspace "$AGENT_EVAL_WORKSPACE" \
          --output "$AGENT_EVAL_WORKSPACE/cross-case-stats.json"
      timeout: 30
      description: "Compute cross-case statistics for judges"
```

### Notification on Completion

```yaml
hooks:
  after_all:
    - command: |
        python3 scripts/notify-slack.py \
          --channel "#eval-results" \
          --run-id "$AGENT_EVAL_RUN_ID" \
          --model "$AGENT_EVAL_MODEL"
      timeout: 15
      on_failure: continue
      description: "Post results to Slack"
```

## Alternatives Considered

**Wrapper scripts around `/eval-run`.** Works today but the harness can't validate, reproduce, report on, or clean up after external setup steps. Hooks inside eval.yaml are declarative, version-controlled, and visible in the run report.

**Extending workspace.py to copy all case files.** Rejected — the workspace deliberately excludes annotations and gold standards to prevent leaking expected outcomes to the skill. A blanket copy would require an exclusion mechanism. Hooks let the case author choose exactly what to extract, with `CASE_SOURCE_DIR` providing controlled access.

**`inputs.files` schema field.** A static list of extra files to copy from the case directory. Handles the simple case but can't extract archives, start services, generate fixtures, or run arbitrary setup. Hooks are strictly more capable with minimal additional complexity.

**Makefile / CI-level setup.** Moves setup out of the eval config into CI pipeline definitions. Fragments the eval specification across multiple files and systems. Hooks keep everything in eval.yaml.

## Migration

No breaking changes. `hooks:` is a new optional top-level key in eval.yaml. Existing configs without it behave identically. The eval-run skill instructions would add hook execution calls at each lifecycle point, and `preflight.py` would validate hook commands are syntactically valid.

## Open Questions

**Per-case hook overrides.** Should individual cases be able to define their own `before_each`/`after_each` hooks (e.g., via a `hooks.yaml` in the case directory)? Currently, per-case variation is handled by the `condition` field and `CASE_SOURCE_DIR` — hooks run or skip based on what files exist in the case directory. This covers the common pattern ("extract if snapshot exists", "seed if SQL present") but requires the eval author to anticipate all variations upfront in eval.yaml. Per-case hooks would let cases bring their own setup without touching the global config, at the cost of another file to discover/validate and harder reasoning about what runs for a given case. Deferred for now — revisit if the conditional approach proves insufficient.

## Future Extensions

- **Hook outputs as judge inputs**: `before_scoring` hooks could write structured data that judges receive via a new `{{ hook_outputs }}` template variable.
- **Built-in hook library**: Common patterns (archive extraction, Docker lifecycle, DB reset) could become named builtins: `- builtin: extract-archives` instead of inline shell.
- **Dry-run mode**: `--dry-run` flag that prints hook commands without executing, for debugging eval configs.
- **Hook metrics**: Capture duration and exit code per hook, surface in the HTML report alongside judge results.
