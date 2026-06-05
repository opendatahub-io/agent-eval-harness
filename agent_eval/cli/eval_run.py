#!/usr/bin/env python3
"""Deterministic eval pipeline — no Claude Code orchestration needed.

Chains the pipeline scripts in sequence: preflight → workspace → execute →
collect → score → report.  Fails fast on any error.

Usage:
    agent-eval run --config eval.yaml --model opus
    agent-eval run --config eval.yaml --model opus --run-id 2026-06-04-opus
    agent-eval run --config eval.yaml --model opus --baseline 2026-06-03-opus --open
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from agent_eval.config import EvalConfig


def _run_step(label, module, args, *, capture_stdout=False):
    """Run a pipeline module as a subprocess.

    Returns captured stdout when capture_stdout=True, otherwise None.
    """
    print(f"\n{'─' * 60}", file=sys.stderr)
    print(f"  {label}", file=sys.stderr)
    print(f"{'─' * 60}", file=sys.stderr)
    result = subprocess.run(
        [sys.executable, "-m", module] + args,
        capture_output=capture_stdout,
        text=capture_stdout,
    )
    if result.returncode != 0:
        if capture_stdout and result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        print(f"\nFAILED: {label} (exit {result.returncode})", file=sys.stderr)
        sys.exit(result.returncode)
    if capture_stdout:
        return result.stdout
    return None


def main():
    parser = argparse.ArgumentParser(
        prog="agent-eval run",
        description="Run a deterministic eval pipeline: preflight → workspace → "
                    "execute → collect → score → report.",
    )
    parser.add_argument("--config", required=True,
                        help="Path to eval.yaml")
    parser.add_argument("--model", default=None,
                        help="Skill model (default: models.skill from config)")
    parser.add_argument("--run-id", default=None,
                        help="Run identifier (default: YYYY-MM-DD-<model>)")
    parser.add_argument("--baseline", default=None,
                        help="Baseline run-id for pairwise comparison")
    parser.add_argument("--cases", nargs="*", default=None,
                        help="Specific case IDs to run")
    parser.add_argument("--subagent-model", default=None,
                        help="Model for subagents")
    parser.add_argument("--effort", default=None,
                        choices=["low", "medium", "high", "xhigh", "max"],
                        help="Claude Code reasoning effort")
    parser.add_argument("--parallelism", type=int, default=None,
                        help="Max parallel case executions")
    parser.add_argument("--open", action="store_true",
                        help="Open HTML report in browser when done")
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)

    model = args.model or config.models.skill
    if not model:
        print("ERROR: no model specified. Set --model or models.skill in eval.yaml.",
              file=sys.stderr)
        sys.exit(1)

    run_id = args.run_id or f"{date.today().isoformat()}-{model}"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        print(f"ERROR: run-id must match [A-Za-z0-9._-]+: {run_id!r}",
              file=sys.stderr)
        sys.exit(1)

    eval_name = config.skill or config.name
    runs_base = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
    output_dir = runs_base / eval_name / run_id

    print(f"Eval: {eval_name}", file=sys.stderr)
    print(f"Model: {model}", file=sys.stderr)
    print(f"Run ID: {run_id}", file=sys.stderr)
    print(f"Output: {output_dir}", file=sys.stderr)

    # 1. Preflight — force clean, no user prompts
    preflight_args = ["--config", args.config, "--run-id", run_id,
                      "--clean", "--force"]
    if args.baseline:
        preflight_args.extend(["--baseline", args.baseline])
    _run_step("Preflight", "agent_eval.run.preflight", preflight_args)

    # 2. Workspace — capture stdout to extract workspace path
    workspace_args = ["--config", args.config, "--run-id", run_id]
    if args.cases:
        workspace_args.extend(["--cases"] + args.cases)
    stdout = _run_step("Workspace", "agent_eval.run.workspace",
                       workspace_args, capture_stdout=True)

    workspace_path = None
    for line in stdout.splitlines():
        if line.startswith("WORKSPACE: "):
            workspace_path = line.split("WORKSPACE: ", 1)[1].strip()
            break
    if not workspace_path:
        print("ERROR: workspace.py did not emit WORKSPACE path", file=sys.stderr)
        sys.exit(1)
    print(f"Workspace: {workspace_path}", file=sys.stderr)

    # 3. Execute
    execute_args = [
        "--config", args.config,
        "--workspace", workspace_path,
        "--skill", config.skill,
        "--model", model,
        "--output", str(output_dir),
    ]
    if args.subagent_model:
        execute_args.extend(["--subagent-model", args.subagent_model])
    if args.effort:
        execute_args.extend(["--effort", args.effort])
    if args.parallelism is not None:
        execute_args.extend(["--parallelism", str(args.parallelism)])
    if config.mlflow.experiment:
        execute_args.extend(["--mlflow-experiment", config.mlflow.experiment])
    _run_step("Execute", "agent_eval.run.execute", execute_args)

    # 4. Collect
    collect_args = [
        "--config", args.config,
        "--workspace", workspace_path,
        "--output", str(output_dir),
    ]
    _run_step("Collect", "agent_eval.run.collect", collect_args)

    # 5. Score judges
    score_args = ["judges", "--run-id", run_id, "--config", args.config]
    _run_step("Score (judges)", "agent_eval.run.score", score_args)

    # 6. Pairwise comparison (if baseline provided)
    if args.baseline:
        pairwise_args = ["pairwise",
                         "--run-id", run_id,
                         "--baseline", args.baseline,
                         "--config", args.config]
        _run_step("Score (pairwise)", "agent_eval.run.score", pairwise_args)

    # 7. Report
    report_args = ["--run-id", run_id, "--config", args.config]
    if args.baseline:
        report_args.extend(["--baseline", args.baseline])
    if args.open:
        report_args.append("--open")
    _run_step("Report", "agent_eval.run.report", report_args)

    print(f"\nDone. Results at: {output_dir}", file=sys.stderr)
    print(f"Report: {output_dir / 'report.html'}", file=sys.stderr)


if __name__ == "__main__":
    main()
