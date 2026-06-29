# Execution Monitoring Reference

## Launching

Launch execute.py using the Bash tool with `run_in_background: true`. **Do NOT
pipe the command** through `tail`, `head`, `grep`, or any other filter — piping
buffers all output and prevents progress monitoring. The command must be the bare
`python3 ... execute.py ...` invocation with no pipes.

## Monitoring progress

Once launched, the Bash tool returns an output file path. Monitor by reading it:

```bash
tail -20 <output_file>
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
