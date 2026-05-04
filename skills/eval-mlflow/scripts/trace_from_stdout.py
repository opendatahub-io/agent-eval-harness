#!/usr/bin/env python3
"""Create an MLflow trace from a Claude Code stream-json stdout log.

The stream-json output from `claude --print --output-format stream-json`
contains all conversation events (system, user, assistant, result) with
timing and tool call data. This script builds a single MLflow trace with
the full execution summary and tool call inventory.

Usage:
    python3 trace_from_stdout.py \\
        --stdout <path/to/stdout.log> \\
        --run-result <path/to/run_result.json> \\
        --run-id <eval-run-id> \\
        [--experiment <name>]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import os
import sys

try:
    import mlflow
except ImportError:
    print("MLflow not installed", file=sys.stderr)
    sys.exit(0)

mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"))


def parse_events(stdout_path):
    """Parse stream-json events from stdout.log."""
    events = []
    with open(stdout_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def extract_summary(events, run_result):
    """Extract a structured summary from stream-json events."""
    # Find the initial prompt
    prompt = ""
    for e in events:
        if e.get("type") == "user":
            content = e.get("message", {}).get("content", "")
            if isinstance(content, list):
                prompt = " ".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
            elif isinstance(content, str):
                prompt = content
            break

    # Extract tool calls (root-level only — skip foreground subagent messages
    # streamed by Claude Code >= 2.1.108)
    tool_calls = []
    for e in events:
        if e.get("type") != "assistant":
            continue
        if e.get("parent_tool_use_id"):
            continue
        for block in e.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            tool_name = block.get("name", "unknown")
            tool_input = block.get("input", {})
            tool_calls.append({
                "tool": tool_name,
                "summary": _summarize_input(tool_name, tool_input),
            })

    # Count tool usage
    tool_counts = {}
    for tc in tool_calls:
        tool_counts[tc["tool"]] = tool_counts.get(tc["tool"], 0) + 1

    # Build response summary from the last result event
    final_result = None
    for e in reversed(events):
        if e.get("type") == "result":
            final_result = e
            break

    response = {
        "exit_code": run_result.get("exit_code"),
        "cost_usd": run_result.get("cost_usd"),
        "duration_s": run_result.get("duration_s"),
        "num_turns": run_result.get("num_turns"),
        "model": run_result.get("model"),
        "subagent_model": run_result.get("subagent_model"),
        "tool_counts": tool_counts,
        "total_tool_calls": len(tool_calls),
    }

    intermediate = {
        "tool_calls": tool_calls[:50],  # First 50 for reference
        "token_usage": run_result.get("token_usage", {}),
    }

    return prompt, response, intermediate


def _summarize_input(tool_name, tool_input):
    """One-line summary of a tool call."""
    if tool_name == "Bash":
        return tool_input.get("command", "")[:120]
    elif tool_name in ("Write", "Edit", "Read"):
        return tool_input.get("file_path", "")
    elif tool_name == "Agent":
        return tool_input.get("description", "")
    elif tool_name == "Skill":
        return tool_input.get("skill", "")
    elif tool_name in ("Glob", "Grep"):
        return tool_input.get("pattern", "")
    else:
        return str(tool_input)[:120]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stdout", required=True, help="Path to stdout.log")
    parser.add_argument("--run-result", required=True, help="Path to run_result.json")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--experiment", default=None)
    args = parser.parse_args()

    with open(args.run_result) as f:
        run_result = json.load(f)

    events = parse_events(args.stdout)
    print(f"Parsed {len(events)} events from stdout", file=sys.stderr)

    experiment = args.experiment or os.environ.get("MLFLOW_EXPERIMENT_NAME", "Default")
    mlflow.set_experiment(experiment)

    prompt, response, intermediate = extract_summary(events, run_result)

    duration_ms = int(run_result.get("duration_s", 0) * 1000)

    trace_id = mlflow.log_trace(
        name=f"rfe.speedrun ({args.run_id})",
        request=prompt[:1000],
        response=response,
        attributes={
            "run_id": args.run_id,
            "model": run_result.get("model", ""),
            "cost_usd": str(run_result.get("cost_usd", 0)),
        },
        tags={
            "eval_run_id": args.run_id,
            "source": "stream-json",
        },
        execution_time_ms=duration_ms,
    )

    print(f"TRACE: {trace_id}")
    print(f"COST: ${run_result.get('cost_usd', 0):.2f}")
    print(f"DURATION: {run_result.get('duration_s', 0):.0f}s")
    print(f"TOOL_CALLS: {response['total_tool_calls']}")


if __name__ == "__main__":
    main()
