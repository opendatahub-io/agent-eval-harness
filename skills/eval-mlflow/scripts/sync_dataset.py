#!/usr/bin/env python3
"""Sync evaluation dataset to MLflow.

Reads case directories and a schema mapping to build MLflow records
with inputs and expectations. The mapping is produced by the LLM agent
interpreting dataset.schema — this script applies it deterministically.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/sync_dataset.py \\
        --config eval.yaml \\
        --mapping tmp/schema_mapping.json \\
        [--dataset-name my-eval]

Schema mapping format (tmp/schema_mapping.json):
    {
        "inputs": {
            "prompt": "input.yaml:prompt",
            "context": "input.yaml:clarifying_context"
        },
        "expectations": {
            "reference_rfe": "reference-rfe.md:__file__",
            "reference_review": "reference-review.md:__file__"
        }
    }

    - "filename:field_path" extracts a YAML/JSON field
    - "filename:__file__" uses the entire file content
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

try:
    import mlflow  # noqa: F401
except ImportError:
    print("MLflow not installed. Install with: pip install 'mlflow[genai]'",
          file=sys.stderr)
    sys.exit(0)

from agent_eval.config import EvalConfig
from agent_eval.mlflow.datasets import get_or_create_dataset, sync_records
from agent_eval.mlflow.experiment import resolve_tracking_uri


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="eval.yaml")
    parser.add_argument("--mapping", required=True,
                        help="Path to schema_mapping.json (produced by the agent)")
    parser.add_argument("--dataset-name", default=None,
                        help="Dataset name (default: config name)")
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)
    mlflow.set_tracking_uri(resolve_tracking_uri(config))

    # Load mapping
    mapping_path = Path(args.mapping)
    if not mapping_path.exists():
        print(f"ERROR: mapping file not found: {mapping_path}", file=sys.stderr)
        sys.exit(1)

    with open(mapping_path) as f:
        mapping = json.load(f)

    input_mapping = mapping.get("inputs", {})
    expectation_mapping = mapping.get("expectations", {})

    if not input_mapping:
        print("ERROR: mapping has no 'inputs' section", file=sys.stderr)
        sys.exit(1)

    # Find case directories
    cases_dir = Path(config.dataset_path)
    if not cases_dir.exists():
        print(f"ERROR: dataset path not found: {cases_dir}", file=sys.stderr)
        sys.exit(1)

    case_dirs = sorted(d for d in cases_dir.iterdir() if d.is_dir())
    if not case_dirs:
        print("ERROR: no case directories found", file=sys.stderr)
        sys.exit(1)

    # Preview first case to validate mapping
    first_record = _extract_record(case_dirs[0], input_mapping, expectation_mapping)
    if not first_record or not first_record.get("inputs"):
        print(f"ERROR: mapping produced no inputs for {case_dirs[0].name}",
              file=sys.stderr)
        print(f"  Input mapping: {input_mapping}", file=sys.stderr)
        print(f"  Files in case: {[f.name for f in case_dirs[0].iterdir()]}", file=sys.stderr)
        sys.exit(1)

    print(f"Preview ({case_dirs[0].name}):")
    for key, value in first_record["inputs"].items():
        preview = str(value)[:80].replace("\n", " ")
        print(f"  inputs.{key}: {preview}...")

    # Build all records
    records = []
    skipped = 0
    for case_dir in case_dirs:
        record = _extract_record(case_dir, input_mapping, expectation_mapping)
        if record and record.get("inputs"):
            records.append(record)
        else:
            skipped += 1
            print(f"  WARNING: skipped {case_dir.name} (no inputs extracted)",
                  file=sys.stderr)

    # Sync to MLflow
    dataset_name = args.dataset_name or config.name or "eval-dataset"
    experiment_name = config.mlflow.experiment or config.name

    dataset = get_or_create_dataset(dataset_name, experiment_name)
    if not dataset:
        print("ERROR: failed to get/create MLflow dataset", file=sys.stderr)
        sys.exit(1)

    count = sync_records(dataset, records)

    print(f"DATASET: {dataset_name}")
    print(f"RECORDS: {count}")
    if skipped:
        print(f"SKIPPED: {skipped}")
    print(f"STATUS: {'synced' if count > 0 else 'error'}")


def _extract_record(case_dir, input_mapping, expectation_mapping):
    """Extract inputs and expectations from a case directory using the mapping."""
    record = {"inputs": {}, "expectations": {}}

    for field_name, source in input_mapping.items():
        value = _extract_field(case_dir, source)
        if value is not None:
            record["inputs"][field_name] = value

    for field_name, source in expectation_mapping.items():
        value = _extract_field(case_dir, source)
        if value is not None:
            record["expectations"][field_name] = value

    return record


def _extract_field(case_dir, source):
    """Extract a field from a case directory.

    Source format: "filename:field_path" or "filename:__file__"
    """
    if ":" not in source:
        return None

    filename, field_path = source.split(":", 1)
    file_path = case_dir / filename

    if not file_path.exists():
        return None

    # Whole file content
    if field_path == "__file__":
        try:
            return file_path.read_text()
        except Exception:
            return None

    # YAML/JSON field extraction
    try:
        if file_path.suffix in (".yaml", ".yml"):
            with open(file_path) as f:
                data = yaml.safe_load(f)
        elif file_path.suffix == ".json":
            with open(file_path) as f:
                data = json.load(f)
        else:
            return None

        # Navigate dotted path
        for part in field_path.split("."):
            if isinstance(data, dict):
                data = data.get(part)
            else:
                return None
        return data
    except Exception:
        return None


if __name__ == "__main__":
    main()
