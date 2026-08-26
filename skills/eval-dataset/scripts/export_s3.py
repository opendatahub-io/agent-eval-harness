#!/usr/bin/env python3
"""CLI to export a dataset with shared workspace.files materialized, for S3/EvalHub.

Thin wrapper around :func:`agent_eval.evalhub.export.export_dataset`. Materializes
shared ``{dest, source}`` ``dataset.workspace.files`` into each per-case directory
so the dataset is self-contained, then optionally uploads to S3 (or prints an
``aws s3 sync`` hint). Run from the project root so ``source`` paths resolve.

Usage:
    python3 export_s3.py --config eval.yaml --out dataset-export \\
        [--s3-bucket <bucket> --s3-prefix <prefix>]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import sys
from pathlib import Path

from agent_eval.config import EvalConfig
from agent_eval.evalhub.export import export_dataset


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="Path to eval.yaml")
    parser.add_argument("--out", required=True, help="Output staging directory")
    parser.add_argument("--s3-bucket", default=None,
                        help="Upload the staging dir to this S3 bucket (needs boto3)")
    parser.add_argument("--s3-prefix", default=None,
                        help="S3 key prefix for the upload (required with --s3-bucket)")
    args = parser.parse_args()

    config = EvalConfig.from_yaml(Path(args.config))
    out = Path(args.out)

    s3_client = None
    if args.s3_bucket:
        if args.s3_prefix is None:
            parser.error("--s3-prefix is required with --s3-bucket")
        try:
            import boto3
        except ImportError:
            print("ERROR: boto3 is required for --s3-bucket upload. Install it, or "
                  "omit --s3-bucket and run `aws s3 sync` on the staging dir yourself.",
                  file=sys.stderr)
            raise SystemExit(1) from None
        s3_client = boto3.client("s3")

    info = export_dataset(
        config, out, s3_client=s3_client,
        bucket=args.s3_bucket, prefix=args.s3_prefix,
    )
    print(f"Exported {info.num_cases} case(s) to {info.dest}")
    for case_id in info.case_ids:
        print(f"  {case_id}")
    if s3_client is None:
        print()
        print("To publish, sync the staging dir to the bucket/prefix your EvalHub "
              "benchmark parameters reference:")
        print(f"  aws s3 sync {out} s3://<bucket>/<prefix>/")


if __name__ == "__main__":
    main()
