#!/usr/bin/env python3
"""Log eval run results to MLflow.

Reads summary.yaml and run_result.json, logs params, metrics,
artifacts, per-case results table, and creates the main orchestrator
trace from stdout.log.  Also links all experiment traces to the run.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/log_results.py \\
        --run-id <id> \\
        --config eval.yaml
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

try:
    import mlflow
    from mlflow import MlflowClient
except ImportError:
    print("MLflow not installed. Install with: pip install 'mlflow[genai]'",
          file=sys.stderr)
    sys.exit(0)

from agent_eval.config import EvalConfig
from agent_eval.mlflow.experiment import resolve_tracking_uri


# ── Trace builder (extracted to agent_eval/mlflow/trace_builder.py) ──
from agent_eval.mlflow.trace_builder import build_trace, log_trace



# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", default="eval.yaml")
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)
    mlflow.set_tracking_uri(resolve_tracking_uri(config))
    runs_dir = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
    run_dir = runs_dir / args.run_id

    # Load summary
    summary_path = run_dir / "summary.yaml"
    if not summary_path.exists():
        print(f"ERROR: no summary found at {summary_path}", file=sys.stderr)
        sys.exit(1)

    with open(summary_path) as f:
        summary = yaml.safe_load(f) or {}

    # Load execution metadata
    run_result = {}
    run_result_path = run_dir / "run_result.json"
    if run_result_path.exists():
        with open(run_result_path) as f:
            run_result = json.load(f)

    # Set experiment
    experiment_name = config.mlflow.experiment or config.name
    mlflow.set_experiment(experiment_name)
    client = MlflowClient()

    # Resolve experiment ID
    exp = mlflow.get_experiment_by_name(experiment_name)
    experiment_id = exp.experiment_id if exp else "0"

    with mlflow.start_run(run_name=args.run_id) as run:
        mlflow_run_id = run.info.run_id

        # ── Params ───────────────────────────────────────────────
        params = {
            "skill": config.skill,
            "runner": config.runner.type,
            "run_id": args.run_id,
            "model": run_result.get("model", ""),
        }
        if run_result.get("subagent_model"):
            params["subagent_model"] = run_result["subagent_model"]
        if run_result.get("agent"):
            params["agent"] = run_result["agent"]
        for key, value in params.items():
            if value:
                mlflow.log_param(key, value)

        # ── Execution metrics ────────────────────────────────────
        if run_result.get("duration_s"):
            mlflow.log_metric("duration_s", run_result["duration_s"])
        if run_result.get("cost_usd"):
            mlflow.log_metric("cost_usd", run_result["cost_usd"])
        if run_result.get("num_turns"):
            mlflow.log_metric("num_turns", run_result["num_turns"])
        token_usage = run_result.get("token_usage", {})
        if token_usage:
            for key in ("input", "output", "cache_read", "cache_create"):
                val = token_usage.get(key)
                if val is not None:
                    mlflow.log_metric(f"tokens/{key}", val)

        # ── Per-model cost and token breakdown ───────────────────
        per_model = run_result.get("per_model_usage", {})
        if per_model:
            import re
            for model_name, stats in per_model.items():
                # Sanitize model name for MLflow metric keys:
                # only alphanumerics, underscores, dashes, periods, spaces,
                # colons, and slashes are allowed.
                safe_name = re.sub(r"[^A-Za-z0-9_\-\. :/]", "-", model_name)
                prefix = f"model/{safe_name}"
                if stats.get("cost_usd") is not None:
                    mlflow.log_metric(f"{prefix}/cost_usd", stats["cost_usd"])
                for key in ("input", "output", "cache_read", "cache_create"):
                    val = stats.get(key)
                    if val is not None:
                        mlflow.log_metric(f"{prefix}/tokens/{key}", val)

        # ── Judge metrics ────────────────────────────────────────
        judges = summary.get("judges", {})
        metric_count = 0
        for judge_name, agg in judges.items():
            if isinstance(agg, dict):
                if agg.get("pass_rate") is not None:
                    mlflow.log_metric(f"{judge_name}/pass_rate", agg["pass_rate"])
                    metric_count += 1
                if agg.get("mean") is not None:
                    mlflow.log_metric(f"{judge_name}/mean", agg["mean"])
                    metric_count += 1

        # ── Tags ─────────────────────────────────────────────────
        has_regressions = False
        if config.thresholds:
            for judge_name, threshold in config.thresholds.items():
                agg = judges.get(judge_name, {})
                if not isinstance(agg, dict):
                    continue
                if "min_pass_rate" in threshold:
                    rate = agg.get("pass_rate")
                    if rate is not None and rate < threshold["min_pass_rate"]:
                        has_regressions = True
                if "min_mean" in threshold:
                    mean = agg.get("mean")
                    if mean is not None and mean < threshold["min_mean"]:
                        has_regressions = True
        mlflow.set_tag("regressions_detected", "yes" if has_regressions else "no")
        mlflow.set_tag("num_judges", str(len(judges)))
        for tag_key, tag_value in (config.mlflow.tags or {}).items():
            mlflow.set_tag(tag_key, str(tag_value))

        # ── Artifacts ────────────────────────────────────────────
        if summary_path.exists():
            mlflow.log_artifact(str(summary_path))

        # Log input files for from-traces extraction.
        for name in ("batch.yaml", "case_order.yaml"):
            p = run_dir / name
            if p.exists():
                mlflow.log_artifact(str(p), "inputs")
        cases_dir = run_dir / "cases"
        if cases_dir.is_dir():
            for case_dir in sorted(cases_dir.iterdir()):
                if not case_dir.is_dir():
                    continue
                inp = case_dir / "input.yaml"
                if inp.exists():
                    mlflow.log_artifact(str(inp), f"inputs/{case_dir.name}")

        # ── Per-case results table ───────────────────────────────
        per_case = summary.get("per_case", {})
        if per_case:
            table_rows = []
            for case_id, case_results in per_case.items():
                if not isinstance(case_results, dict):
                    continue
                for judge_name, result in case_results.items():
                    if not isinstance(result, dict):
                        continue
                    table_rows.append({
                        "case_id": case_id,
                        "judge": judge_name,
                        "value": result.get("value"),
                        "rationale": str(result.get("rationale", ""))[:500],
                    })
            if table_rows:
                columns = {}
                for key in table_rows[0]:
                    columns[key] = [row[key] for row in table_rows]
                mlflow.log_table(columns, artifact_file="per_case_results.json")

    # ── Traces from stdout.log ─────────────────────────────────
    main_trace_id = None
    exec_mode = run_result.get("execution_mode", "batch")

    if exec_mode == "case":
        # Per-case mode: each case has its own stdout.log
        cases_dir = run_dir / "cases"
        if cases_dir.exists():
            for case_dir in sorted(d for d in cases_dir.iterdir() if d.is_dir()):
                case_stdout = case_dir / "stdout.log"
                if not case_stdout.exists():
                    continue
                case_id = case_dir.name
                case_result = run_result.get("per_case", {}).get(case_id, run_result)
                trace_name = f"{config.skill} ({case_id})" if config.skill else case_id
                trace_dict = build_trace(case_stdout, case_result, case_id,
                                         experiment_id, trace_name=trace_name)
                if trace_dict:
                    tid = log_trace(trace_dict)
                    if tid:
                        if not main_trace_id:
                            main_trace_id = tid
                        num_spans = len(trace_dict["data"]["spans"])
                        print(f"TRACE: {tid} ({num_spans} spans) — {case_id}")
    else:
        # Batch mode: one stdout.log for the entire run
        stdout_path = run_dir / "stdout.log"
        if stdout_path.exists() and run_result:
            trace_name = f"{config.skill} ({args.run_id})" if config.skill else ""
            trace_dict = build_trace(stdout_path, run_result, args.run_id,
                                     experiment_id, trace_name=trace_name)
            if trace_dict:
                main_trace_id = log_trace(trace_dict)
                if main_trace_id:
                    num_spans = len(trace_dict["data"]["spans"])
                    duration_s = run_result.get("duration_s", 0)
                    print(f"TRACE: {main_trace_id} ({num_spans} spans, {duration_s:.0f}s)")

    # ── Link traces to run ───────────────────────────────────────
    trace_ids = []
    if main_trace_id:
        trace_ids.append(main_trace_id)
    try:
        all_traces = client.search_traces(experiment_ids=[experiment_id],
                                          max_results=500)
        for t in all_traces:
            tags = t.info.tags or {}
            if tags.get("eval_run_id") == args.run_id:
                if t.info.trace_id not in trace_ids:
                    trace_ids.append(t.info.trace_id)
        if trace_ids:
            client.link_traces_to_run(run_id=mlflow_run_id, trace_ids=trace_ids)
            print(f"LINKED: {len(trace_ids)} traces to run {mlflow_run_id}")
    except Exception as e:
        print(f"WARNING: failed to link traces: {e}", file=sys.stderr)

    print(f"EXPERIMENT: {experiment_name}")
    print(f"RUN: {mlflow_run_id}")
    print(f"PARAMS: {len(params)}")
    print(f"METRICS: {metric_count}")
    print(f"TABLE: per_case_results ({len(per_case)} cases)")


if __name__ == "__main__":
    main()
