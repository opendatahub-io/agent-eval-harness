# Execution Monitoring Reference

## Launching

Launch execute.py using the Bash tool with `run_in_background: true`, and **do NOT
redirect or pipe its output** — no `>`, `|`, `tee`, `2>&1`, `tail`, `head`, or
`grep`. Claude Code's background-command viewer (and the output-file path the Bash
tool returns) shows the process's *own* stdout/stderr stream; any redirect or pipe
diverts that stream and leaves the viewer blank. The command must be the bare
`python3 ... execute.py ...` invocation. You don't need a redirect for a durable
log: execute.py already mirrors its live console to `<output_dir>/console.log`, and
emits a `WARNING:` if it detects its stdout was redirected into the run directory.

## Session lifecycle warning

**Do not end your turn while execute.py is running.** Background tasks are killed
when the session becomes idle — this applies to both headless and interactive
modes:

- **Headless (`-p`):** ending the turn exits the session immediately, killing all
  background tasks and producing empty results that CI reports as green.
- **Interactive:** Claude Code SIGTERMs background tasks after ~55 minutes of
  session inactivity. A long-running execute.py (common with 20+ cases) will be
  killed silently if no tool calls keep the session active.

You must keep polling until execution completes. Poll every 2–3 minutes with
`tail -20 <output_file>` (the path the Bash tool returned) or
`tail -20 <output_dir>/console.log` (the run dir passed via `--output`) — never a
self-created redirect. Polling keeps the session active and prevents the idle
timeout from firing.

## Monitoring progress

Once launched, the Bash tool returns an output file path. Monitor by reading it (or
the stable `console.log` mirror) — never a self-created redirect:

```bash
tail -20 <output_file>             # the path the Bash tool returned
tail -20 <output_dir>/console.log  # execute.py's own mirrored console
```

Look for phase markers (`## Phase`, `## Step`, `Batch N/M`), agent counts
(`N agents launched`, `N/M done`), and completion signals (`Done`). Summarize
concisely — e.g., "Batch 2/4: review agents 3/5 complete" rather than dumping
raw output.

## Detecting problems

If the last lines haven't changed across two checks (~2-3 min apart), the
pipeline may be stuck. Common signs:

- Repeated `sleep` commands with no progress change → agents may have timed out
- `ERROR` or `Traceback` in the output → script failure, report immediately
- No new output for 5+ minutes → possible hang, check if the process is running
- `exit code` or `EXIT:` appearing → execution finished (check the code)

Report issues with the relevant output lines rather than waiting for completion.

## After execution

Check `run_result.json` for execution metadata:

```bash
cat $AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/run_result.json
```

Key fields: `exit_code`, `duration_s`, `wall_clock_s` (lower when parallelism is
used), `cost_usd`, `num_turns`, `per_model_usage`, `per_model_turns`.

If `exit_code` is non-zero, report the failure with the exit code, duration, and
the first few lines of `stderr.log`. Do not continue to scoring.

## CLI flag fallbacks

Most execute.py flags fall back to eval.yaml config values:

- `--agent` → `runner.type` (default `claude-code`)
- `--model` → `models.skill` (required — errors if unset)
- `--mlflow-experiment` → `mlflow.experiment`
- `--skill-args` → `execution.arguments` (`{field}` placeholders resolved per case)
- `--effort` → `runner.effort` (Claude Code only)
- `--parallelism` → `execution.parallelism` (concurrent via thread pool)

Override via CLI only when testing different combinations than config specifies.
