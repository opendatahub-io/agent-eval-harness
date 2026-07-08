# 009: Multi-Step Workflow Evaluation

**Status:** Implemented  
**Date:** 2026-07-08

## Problem

The eval harness evaluates a single skill per case. Real-world agent deployments
are multi-step pipelines where skill invocations are interleaved with
deterministic scripts, conditional logic, and validation loops with retry
(backpressure). This gap means critical failure modes — especially the
"validate output, retry if invalid" loop — are untestable.

### Motivating Example

The OpenShift payload agent workflow (~635-line bash script) has these phases:

1. Resolve a payload tag (deterministic API calls)
2. Run `/ci:payload-analysis` with a 1hr timeout
3. If timeout: re-invoke Claude with a "wrap up" nudge (session continuation)
4. Validate structured outputs; re-invoke Claude up to 3x if invalid (backpressure)
5. Generate Slack summary (separate skill)

Step 4 is the highest-value evaluation target — it tests whether the agent can
self-correct under structured feedback — and was completely untestable before
this change.

## Design

### eval.yaml Schema

`workflow:` is a new top-level key, mutually exclusive with `skill:`. It
replaces the single-skill execution with a linear step sequence.

```yaml
name: payload-agent

workflow:
  steps:
    - id: snapshot
      type: script
      command: python3 scripts/payload_snapshot.py {payload_tag}
      timeout: 120

    - id: analysis
      type: skill
      skill: ci:payload-analysis
      arguments: "{payload_tag} --snapshot-dir snapshot"
      timeout: 3600

    - id: nudge
      type: skill
      skill: ci:payload-analysis
      arguments: "Wrap up now and generate report artifacts."
      continue_session: true
      timeout: 600
      condition: "steps.analysis.timed_out"

    - id: validate
      type: validate
      command: python3 scripts/validate_output.py
      retry_step: analysis
      retry_prompt: |
        Output invalid: {validate.stderr}
        Regenerate the required files now.
      max_retries: 3

    - id: slack-summary
      type: skill
      skill: ci:slack-summary
      arguments: "--input artifacts/payload-analysis.yaml"
```

### Step Types

Three types — the minimal set that covers the observed patterns:

| Type       | Behavior |
|------------|----------|
| `skill`    | Invokes a skill via the existing runner (`run_skill()`) |
| `script`   | Runs a shell command via `subprocess` |
| `validate` | Runs a shell command; on non-zero exit, re-invokes `retry_step` up to `max_retries` times |

### Key Decisions and Rationale

**1. Linear steps with conditions, not a DAG.**

DAGs add complexity (cycle detection, join semantics, visualization) without
covering additional real-world patterns. The `condition` field on any step
provides skip-if-not-needed logic, which handles all observed branching patterns
(timeout nudge, optional post-processing). If DAG workflows emerge later, this
design extends naturally — add a `depends_on` field and topological sort.

**2. `validate` is a distinct step type, not a generic retry wrapper.**

The backpressure pattern (validate → retry skill → re-validate) is specific
enough to warrant first-class support. Making it a step type keeps the eval.yaml
declarative and avoids users needing to implement retry loops in custom scripts.
The `retry_step` field must reference a `skill` step — you can only retry an
agent, not a deterministic script.

**3. Session continuation via `continue_session: true`.**

The nudge and validation-retry patterns require continuing the same Claude
conversation. This maps directly to `claude --continue`. Implementation required
deferring session cleanup (`_cleanup_session()`) between workflow steps and only
cleaning up at workflow end. The `skip_cleanup` parameter on `run_skill()` is
the mechanism.

**4. Inter-step data flow via shared workspace + env vars.**

Steps share the case workspace filesystem. Each completed step also injects
environment variables (`STEP_<ID>_EXIT_CODE`, `STEP_<ID>_DURATION_S`,
`STEP_<ID>_TIMED_OUT`, etc.) into subsequent steps. Argument templates support
`{step_id.field}` for referencing prior step results (e.g., `{validate.stderr}`
in retry prompts).

This avoids a separate data-passing mechanism — the filesystem is already the
primary data channel between skills.

**5. `on_failure: abort | continue`.**

Steps default to `abort` (stop the workflow on failure). `continue` lets
non-critical steps fail without blocking downstream steps. This is simpler than
error-handler steps and covers the common patterns.

### Workflow Result Artifacts

Each case produces `workflow_result.json` alongside `run_result.json`:

```json
{
  "steps": {
    "snapshot": {"exit_code": 0, "duration_s": 12.3, "type": "script", "skipped": false},
    "analysis": {"exit_code": 0, "duration_s": 845, "type": "skill", "cost_usd": 2.10},
    "validate": {"exit_code": 0, "duration_s": 18.5, "type": "validate", "retries": 1}
  },
  "total_retries": 1,
  "total_duration_s": 920.8,
  "total_cost_usd": 2.40
}
```

### Judging Workflow Metadata

Judges can access workflow data:

- **Inline checks:** `outputs.get("workflow", {})` — e.g., assert retry count
- **LLM judges:** `{{ workflow }}` Jinja2 variable — e.g., "the workflow needed
  {{ workflow.total_retries }} validation retries"
- **Builtin/external judges:** receive the full outputs dict including `workflow`

### Report Integration

The HTML report's per-case detail view includes a workflow steps table showing
step ID, type, duration, cost, retries, and status for each step, plus total
retries across the workflow.

## Changes

| File | What changed |
|------|-------------|
| `agent_eval/config.py` | `WorkflowStepConfig`, `StepValidateConfig`, `WorkflowConfig` dataclasses; `workflow` field and `is_workflow` property on `EvalConfig`; parsing + validation in `from_yaml()` |
| `agent_eval/agent/base.py` | `continue_session` and `skip_cleanup` params on `run_skill()` ABC; `cleanup_session()` method |
| `agent_eval/agent/claude_code.py` | `--continue` flag support; deferred session cleanup |
| `agent_eval/agent/cli_runner.py` | Signature update for ABC compatibility |
| `agent_eval/agent/responses_api.py` | Signature update for ABC compatibility |
| `skills/eval-run/scripts/execute.py` | `StepResult`, `_run_workflow_case()`, `_run_script_step()`, `_eval_step_condition()`, `_step_env()`, `_resolve_step_args()` |
| `skills/eval-run/scripts/collect.py` | Exclude `workflow_result.json` from skill output collection |
| `skills/eval-run/scripts/score.py` | Load `workflow_result.json` into case record; expose `{{ workflow }}` in Jinja2 |
| `skills/eval-run/scripts/report.py` | Workflow steps table in per-case detail view |
| `tests/test_workflow.py` | 30 unit tests covering config parsing, condition eval, script execution, argument resolution |

## Backwards Compatibility

- Existing `skill:` configs are unchanged — no `workflow:` key triggers the
  existing single-skill path
- `execution:`, `runner:`, `hooks:`, `judges:`, `outputs:` all compose with
  `workflow:` naturally
- `workflow_result.json` is additive — components that don't know about it
  ignore it

## Deferred

- **Harbor/EvalHub execution:** The workflow step loop needs to run inside the
  container/pod. Harbor can serialize `workflow:` config but executing steps
  requires the loop from `execute.py`.
- **DAG workflows:** Linear + conditionals covers all observed patterns. Add
  `depends_on` later if needed.
- **Per-step output isolation:** All steps share the workspace. Revisit if
  collision becomes a problem.
