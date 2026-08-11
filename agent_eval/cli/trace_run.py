#!/usr/bin/env python3
"""Run a Claude Code skill with MLflow tracing.

Drop-in replacement for ``claude --print`` that captures stream-json
output and builds a hierarchical MLflow trace with tool calls,
subagent spans, and execution metrics.

Usage:
    # Pipe prompt via stdin (same as claude --print)
    echo "/rfe.speedrun --input batch.yaml --headless" | \\
      claude-trace --model opus

    # Prompt as argument
    claude-trace --model opus -p "/rfe.create 'GPU autoscaling'"

    # Explicit experiment and output directory
    claude-trace --model opus \\
      -p "/rfe.speedrun --input batch.yaml" \\
      --experiment rfe-prod \\
      --trace-dir runs/$(date +%Y%m%d)

    # Capture only (no MLflow push)
    claude-trace --model opus --no-mlflow --trace-dir /tmp/my-run

Environment:
    MLFLOW_TRACKING_URI   — MLflow server (default: http://127.0.0.1:5000)
    MLFLOW_EXPERIMENT_NAME — experiment name (overridden by --experiment)
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv before 3p imports

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_eval.agent.stream_capture import (
    make_prompt_event,
    inject_timestamp,
    extract_usage,
    count_subagent_turns,
    setup_subagent_hook,
)


def main():
    # ── Parse arguments ──────────────────────────────────────────
    # Separate claude-trace flags from claude flags.
    # claude-trace flags: --experiment, --trace-dir, --no-mlflow, -p
    # Everything else passes through to claude.
    trace_args, claude_args, prompt = _parse_args(sys.argv[1:])

    # Force stream-json output — strip any conflicting --output-format
    # that the caller may have passed (build_trace requires stream-json).
    _strip_flag(claude_args, "--output-format")
    claude_args.extend(["--output-format", "stream-json"])
    if "--print" not in claude_args:
        claude_args.insert(0, "--print")
    if "--verbose" not in claude_args:
        claude_args.append("--verbose")

    # Build command
    cmd = ["claude"] + claude_args

    # Resolve prompt: -p flag, or read from stdin
    if not prompt:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            print("Error: no prompt provided. Use -p or pipe via stdin.",
                  file=sys.stderr)
            sys.exit(1)

    # ── Execute ──────────────────────────────────────────────────
    trace_dir = Path(trace_args.get("trace_dir",
                     f"tmp/trace-runs/{datetime.now().strftime('%Y%m%d-%H%M%S')}"))
    trace_dir.mkdir(parents=True, exist_ok=True)

    # Write settings.json with SubagentStop hook. If the caller passed
    # --settings, load their file first and merge the hook into it.
    settings_dir = trace_dir / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings = {}
    caller_settings = _extract_flag(claude_args, "--settings")
    if caller_settings and Path(caller_settings).exists():
        try:
            with open(caller_settings) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    subagent_dir = str((trace_dir / "subagents").resolve())
    setup_subagent_hook(settings, subagent_dir)
    merged_settings = settings_dir / "settings.json"
    with open(merged_settings, "w") as f:
        json.dump(settings, f, indent=2)
    cmd.extend(["--settings", str(merged_settings)])

    # Session persistence must stay ON so subagent transcript files
    # survive long enough for the SubagentStop hook to copy them.

    print(f"claude-trace: running with tracing to {trace_dir}", file=sys.stderr)
    print(f"claude-trace: cmd = {' '.join(cmd[:6])}...", file=sys.stderr)

    start = time.monotonic()
    stdout_lines = []
    resolved_model = None

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Drain stderr in a background thread to avoid deadlock (CWE-400).
    # If claude fills the stderr pipe before stdout finishes, the child
    # blocks and this wrapper hangs.
    stderr_lines = []
    def _drain_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)
    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    proc.stdin.write(prompt)
    proc.stdin.close()

    # Inject synthetic user event for the prompt
    stdout_lines.append(make_prompt_event(prompt))

    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line.strip():
            stdout_lines.append(line)
            continue
        try:
            line = inject_timestamp(line)
            obj = json.loads(line)
            # Capture resolved model from init event
            if (not resolved_model
                    and obj.get("type") == "system"
                    and obj.get("subtype") == "init"):
                resolved_model = obj.get("model")
            # Print progress
            if obj.get("type") == "result":
                cost = obj.get("total_cost_usd", 0)
                turns = obj.get("num_turns", 0)
                print(f"claude-trace: done ({turns} turns, ${cost:.2f})",
                      file=sys.stderr)
        except (json.JSONDecodeError, ValueError):
            pass
        stdout_lines.append(line)

    proc.wait()
    stderr_thread.join(timeout=5)
    stderr = "".join(stderr_lines)

    duration = time.monotonic() - start

    # NOTE: no session cleanup here.  Unlike the eval runner (which uses
    # disposable temp workspaces), claude-trace runs in the user's real
    # project directory — deleting that session dir would destroy their
    # Claude Code session history.

    # ── Save artifacts ───────────────────────────────────────────
    # stdout.log
    stdout_text = "\n".join(stdout_lines)
    (trace_dir / "stdout.log").write_text(stdout_text)

    # stderr.log
    if stderr:
        (trace_dir / "stderr.log").write_text(stderr)

    # run_result.json
    (token_usage, cost_usd, num_turns, stream_ids, models_seen,
     per_model_usage, _stream_ids_by_model) = extract_usage(stdout_lines)
    # Add subagent turns from captured transcripts, deduplicating
    # against IDs already seen in the stream
    subagent_dir = trace_dir / "subagents"
    subagent_turns = count_subagent_turns(subagent_dir, already_seen=stream_ids)
    if subagent_turns:
        num_turns = (num_turns or 0) + subagent_turns
    run_result = {
        "exit_code": proc.returncode,
        "duration_s": round(duration, 1),
        "token_usage": token_usage,
        "cost_usd": cost_usd,
        "per_model_usage": per_model_usage,
        "num_turns": num_turns,
        "model": resolved_model or "",
        "agent": "claude-code",
    }
    with open(trace_dir / "run_result.json", "w") as f:
        json.dump(run_result, f, indent=2)

    print(f"claude-trace: saved to {trace_dir} "
          f"({duration:.0f}s, ${cost_usd or 0:.2f})", file=sys.stderr)

    # ── Build and push trace ─────────────────────────────────────
    if not trace_args.get("no_mlflow"):
        try:
            import mlflow
            tracking_uri = os.environ.get("MLFLOW_TRACKING_URI",
                                          "http://127.0.0.1:5000")
            mlflow.set_tracking_uri(tracking_uri)

            experiment = (trace_args.get("experiment")
                          or os.environ.get("MLFLOW_EXPERIMENT_NAME")
                          or "Default")
            mlflow.set_experiment(experiment)
            exp = mlflow.get_experiment_by_name(experiment)
            experiment_id = exp.experiment_id if exp else "0"

            from agent_eval.mlflow.trace_builder import build_trace, log_trace

            run_id = trace_dir.name
            trace_name = prompt.split()[0] if prompt else "skill-run"
            trace_name = f"{trace_name} ({run_id})"

            trace_dict = build_trace(
                stdout_path=trace_dir / "stdout.log",
                run_result=run_result,
                run_id=run_id,
                experiment_id=experiment_id,
                trace_name=trace_name,
            )
            if trace_dict:
                trace_id = log_trace(trace_dict)
                if trace_id:
                    num_spans = len(trace_dict["data"]["spans"])
                    print(f"claude-trace: trace {trace_id} "
                          f"({num_spans} spans) → {tracking_uri}",
                          file=sys.stderr)
        except ImportError:
            print("claude-trace: mlflow not installed, skipping trace push",
                  file=sys.stderr)
        except Exception as e:
            print(f"claude-trace: trace push failed: {e}", file=sys.stderr)

    sys.exit(proc.returncode)


def _parse_args(argv):
    """Separate claude-trace flags from claude flags.

    Returns (trace_args_dict, claude_args_list, prompt_str).
    """
    trace_args = {}
    claude_args = []
    prompt = ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--experiment" and i + 1 < len(argv):
            trace_args["experiment"] = argv[i + 1]
            i += 2
        elif arg == "--trace-dir" and i + 1 < len(argv):
            trace_args["trace_dir"] = argv[i + 1]
            i += 2
        elif arg == "--no-mlflow":
            trace_args["no_mlflow"] = True
            i += 1
        elif arg == "-p" and i + 1 < len(argv):
            prompt = argv[i + 1]
            i += 2
        elif arg == "--help":
            print(__doc__)
            sys.exit(0)
        else:
            claude_args.append(arg)
            i += 1
    return trace_args, claude_args, prompt


def _strip_flag(args, flag):
    """Remove a flag and its value from args list (in place)."""
    i = 0
    while i < len(args):
        if args[i] == flag and i + 1 < len(args):
            del args[i:i + 2]
        elif args[i].startswith(f"{flag}="):
            del args[i]
        else:
            i += 1


def _extract_flag(args, flag):
    """Remove a flag from args and return its value (or None)."""
    i = 0
    while i < len(args):
        if args[i] == flag and i + 1 < len(args):
            val = args[i + 1]
            del args[i:i + 2]
            return val
        elif args[i].startswith(f"{flag}="):
            val = args[i].split("=", 1)[1]
            del args[i]
            return val
        else:
            i += 1
    return None


if __name__ == "__main__":
    main()
