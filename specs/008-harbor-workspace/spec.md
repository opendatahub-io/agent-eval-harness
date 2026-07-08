# Spec: Harbor Workspace Symmetry

## Problem

The local eval-run flow (steps 2–6) creates a workspace that snapshots
the dataset, sets up tool interception, and collects results into a
self-contained run directory. The Harbor runner skips all of this — it
generates task packages separately and maps results after the fact.

This means:
- Dataset files could change during a long parallel run
- No local record of what was executed alongside the results
- Tool interception is generated ad-hoc into task packages, not
  through the shared workspace flow
- The Harbor run dir is less self-contained than a local run dir

## Design

The Harbor runner creates a workspace just like the local runner, then
generates task packages into it. The run dir becomes the single source
of truth for the entire eval.

```
eval/runs/<run-id>/
  eval.yaml              ← snapshot of config at run time
  dataset/               ← copy of cases (immutable during run)
  harbor-tasks/          ← generated task packages
  harbor-jobs/           ← Harbor output (transcripts, rewards)
  cases/<id>/artifacts/  ← collected output artifacts
  summary.yaml           ← judge results
  report.html            ← rendered report
```

### Flow

1. **Create workspace** — copy eval.yaml + dataset cases into the run
   dir (same as local steps 2–3)
2. **Set up tool interception** — generate hooks + settings.json using
   the shared `agent_eval.tools.interception` code path (same as local
   step 4)
3. **Generate task packages** — into `harbor-tasks/` within the
   workspace, referencing the snapshotted dataset
4. **Execute** — `harbor run` against the workspace's task packages
   (replaces local step 5)
5. **Collect + score** — parse Harbor results, copy artifacts, run
   suite-level judges (pairwise, regression) — same as local steps 6–7
6. **Report** — generate report.html from the workspace data

### What changes

- `run.py` gains workspace creation before calling `harbor run`
- Task packages are generated into the workspace, not a separate dir
- The `--tasks-dir` / `--jobs-dir` flags become internal (derived from
  the workspace path)
- `eval-run --runner harbor` SKILL.md instructions simplified — the
  run.py call handles everything

### What doesn't change

- `eval-dataset` still generates task packages standalone (for manual
  `harbor run` usage)
- The task package format stays the same
- `results.py` and `build_summary()` work unchanged
- Report generation is the same

## Benefits

- **Dataset integrity** — cases are snapshotted, can't change during
  a long parallel run
- **Reproducibility** — run dir contains everything to re-run or audit
- **Symmetry** — same workspace flow as local, only the execution step
  differs
- **Simpler CLI** — `eval-run --runner harbor` just works, no separate
  tasks-dir / jobs-dir management

## Multi-step integration

When multi-step task generation is added to `eval-dataset` / `tasks.py`,
the workspace flow handles it transparently — the task packages in
`harbor-tasks/` are multi-step instead of single-step, everything else
stays the same.
