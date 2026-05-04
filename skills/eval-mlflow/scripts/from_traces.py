#!/usr/bin/env python3
"""Extract inputs from MLflow traces for dataset generation.

Searches production traces from the configured experiment and extracts
skill invocation inputs. Outputs YAML to stdout for the eval-dataset
agent to create case directories from.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/from_traces.py \\
        --config eval.yaml \\
        [--experiment my-experiment] \\
        [--count 10] \\
        [--min-duration 5]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import sys

import yaml

try:
    import mlflow
except ImportError:
    print("MLflow not installed. Install with: pip install 'mlflow[genai]'",
          file=sys.stderr)
    sys.exit(0)

from agent_eval.config import EvalConfig
from agent_eval.mlflow.experiment import get_experiment_id, resolve_tracking_uri
from agent_eval.mlflow.traces import extract_trace_inputs


def _attach_input_artifacts(extracted, experiment_id):
    """Enrich extracted traces with input artifacts from linked MLflow runs.

    For each trace with an eval_run_id tag, finds the MLflow run that logged
    the eval results and downloads batch.yaml or per-case input.yaml artifacts.
    """
    import os
    import tempfile

    client = mlflow.MlflowClient()

    run_ids_by_name = {}
    for entry in extracted:
        eval_run_id = entry.get("eval_run_id")
        if not eval_run_id or eval_run_id in run_ids_by_name:
            continue
        try:
            runs = mlflow.search_runs(
                experiment_ids=[experiment_id],
                filter_string=f"tags.mlflow.runName = '{eval_run_id}'",
            )
            if not runs.empty:
                run_ids_by_name[eval_run_id] = list(runs.run_id)
        except Exception:
            continue

    if not run_ids_by_name:
        return

    from pathlib import Path

    for entry in extracted:
        eval_run_id = entry.get("eval_run_id")
        mlflow_run_ids = run_ids_by_name.get(eval_run_id, [])

        best_batch = None
        best_batch_size = 0
        best_cases = {}

        for mlflow_run_id in mlflow_run_ids:
            try:
                artifacts = client.list_artifacts(mlflow_run_id, "inputs")
            except Exception:
                continue

            if not artifacts:
                continue

            with tempfile.TemporaryDirectory() as tmp:
                try:
                    local_path = client.download_artifacts(
                        mlflow_run_id, "inputs", tmp)
                except Exception:
                    continue

                inputs_dir = Path(local_path)

                batch_path = inputs_dir / "batch.yaml"
                if batch_path.exists():
                    content = batch_path.read_text()
                    if len(content) > best_batch_size:
                        best_batch = yaml.safe_load(content)
                        best_batch_size = len(content)

                for case_dir in sorted(inputs_dir.iterdir()):
                    if case_dir.is_dir():
                        inp = case_dir / "input.yaml"
                        if inp.exists():
                            best_cases[case_dir.name] = (
                                yaml.safe_load(inp.read_text())
                            )

        if best_batch is not None:
            entry["batch_yaml"] = best_batch
        if best_cases:
            entry["case_inputs"] = best_cases

    enriched = sum(1 for e in extracted if "batch_yaml" in e or "case_inputs" in e)
    if enriched:
        print(f"Enriched {enriched}/{len(extracted)} traces with input artifacts",
              file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="eval.yaml")
    parser.add_argument("--experiment", default=None,
                        help="Experiment name (default: from eval.yaml)")
    parser.add_argument("--count", type=int, default=10,
                        help="Max traces to extract")
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)
    mlflow.set_tracking_uri(resolve_tracking_uri(config))
    experiment_name = args.experiment or config.mlflow.experiment or config.name

    if not experiment_name:
        print("ERROR: no experiment name (set mlflow.experiment in eval.yaml "
              "or pass --experiment)", file=sys.stderr)
        sys.exit(1)

    # Search traces
    exp_id = get_experiment_id(experiment_name)
    if not exp_id:
        print(f"ERROR: experiment '{experiment_name}' not found", file=sys.stderr)
        sys.exit(2)

    try:
        traces = mlflow.search_traces(
            experiment_ids=[exp_id],
            max_results=args.count * 2,  # fetch extra in case some are filtered
        )
    except Exception as e:
        print(f"ERROR: failed to search traces: {e}", file=sys.stderr)
        sys.exit(1)

    if traces.empty:
        print(f"No traces found in experiment '{experiment_name}'", file=sys.stderr)
        sys.exit(2)

    # Extract inputs
    extracted = extract_trace_inputs(traces, max_results=args.count)

    if not extracted:
        print("No usable inputs extracted from traces", file=sys.stderr)
        sys.exit(2)

    # Enrich with input artifacts from linked MLflow runs.
    _attach_input_artifacts(extracted, exp_id)

    # Output as YAML
    yaml.dump(extracted, sys.stdout, default_flow_style=False,
              allow_unicode=True, width=120)

    print(f"\n# Extracted {len(extracted)} inputs from {len(traces)} traces",
          file=sys.stderr)


if __name__ == "__main__":
    main()
