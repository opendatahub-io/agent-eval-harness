#!/usr/bin/env python3
"""Deterministic dataset audit CLI (V1 task-generation validity).

Thin wrapper around :mod:`agent_eval.dataset_audit`. Runs every audit check
over the dataset, writes ``dataset_audit.yaml`` at the dataset ROOT (a file —
invisible to dir-only case discovery), and prints a readable findings summary
with the composition skew tables.

Findings are triage input, not a gate: exit code is 0 even with findings
unless ``--strict`` is passed (then any warning/error exits 1).

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/audit_dataset.py --config eval.yaml \\
        [--dataset <dir>] [--duplicate-threshold 0.85] \\
        [--difficulty-values easy,medium,hard] [--timestamp <iso>] [--strict]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import sys
from pathlib import Path

from agent_eval.config import EvalConfig
from agent_eval.dataset_audit import (
    DEFAULT_DUPLICATE_THRESHOLD,
    run_audit,
    write_audit,
)

#: Findings printed per check before eliding.
MAX_FINDINGS_SHOWN = 5


def _print_table(title, table):
    if not table:
        return
    print(f"  {title}:")
    for key, value in table.items():
        print(f"    {key}: {value}")


def _print_report(audit, audit_path):
    print(f"Dataset audit — {audit['dataset_path']}")
    print(f"Cases: {audit['summary']['cases']}")
    print()
    for name, check in audit["checks"].items():
        status = check.get("status", "?")
        count = check.get("finding_count", 0)
        line = f"[{status}] {name}"
        if status == "skipped":
            line += f" — {check.get('reason', '')}"
        elif count:
            line += f" — {count} finding(s)"
        print(line)
        if check.get("label"):
            print(f"  note: {check['label']}")
        for finding in check.get("findings", [])[:MAX_FINDINGS_SHOWN]:
            severity = finding.get("severity", "warning")
            case = finding.get("case") or ", ".join(
                finding.get("cases", [])) or "-"
            print(f"  {severity.upper()} {case}: {finding.get('message', '')}")
        if count > MAX_FINDINGS_SHOWN:
            print(f"  … {count - MAX_FINDINGS_SHOWN} more finding(s) — "
                  "see dataset_audit.yaml")
        if name == "composition":
            _print_table("by category", check.get("by_category", {}))
            for row in check.get("seeds", []) or []:
                print(f"    seed '{row['category']}': requested "
                      f"{row['requested']}, realized {row['realized']}")
            _print_table("by difficulty", check.get("by_difficulty", {}))
        if name == "conditional_judges":
            for row in check.get("judges", []) or []:
                detail = f"    {row['judge']}: {row.get('coverage', '?')}"
                if "true" in row:
                    detail += (f" (true={row['true']}, false={row['false']}, "
                               f"errors={row['errors']})")
                print(detail)
    print()
    summary = audit["summary"]
    print(f"Summary: {summary['errors']} error(s), "
          f"{summary['warnings']} warning(s), {summary['info']} info")
    print(f"Audit written to: {audit_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Deterministic dataset audit (writes dataset_audit.yaml "
                    "at the dataset root)")
    parser.add_argument("--config", required=True, help="Path to eval.yaml")
    parser.add_argument(
        "--dataset", default=None,
        help="Override the dataset root (default: dataset.path from the "
             "config)")
    parser.add_argument(
        "--duplicate-threshold", type=float,
        default=DEFAULT_DUPLICATE_THRESHOLD,
        help="Near-duplicate similarity threshold in (0, 1] "
             f"(default: {DEFAULT_DUPLICATE_THRESHOLD})")
    parser.add_argument(
        "--difficulty-values", default=None,
        help="Comma-separated difficulty vocabulary (default: "
             "easy,medium,hard); only validated when a case declares a "
             "difficulty field")
    parser.add_argument(
        "--timestamp", default=None,
        help="ISO timestamp recorded as generated_at (default: system "
             "clock)")
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit 1 when the audit reports any warning or error "
             "(default: exit 0 — findings are triage input, not a gate)")
    args = parser.parse_args()

    if not (0 < args.duplicate_threshold <= 1):
        print("ERROR: --duplicate-threshold must be in (0, 1], got "
              f"{args.duplicate_threshold}", file=sys.stderr)
        sys.exit(2)

    difficulty_values = None
    if args.difficulty_values is not None:
        difficulty_values = [v.strip() for v in
                             args.difficulty_values.split(",") if v.strip()]
        if not difficulty_values:
            print("ERROR: --difficulty-values must list non-empty values",
                  file=sys.stderr)
            sys.exit(2)

    config = EvalConfig.from_yaml(args.config)
    dataset_root = (Path(args.dataset).resolve() if args.dataset
                    else config.resolve_path(config.dataset.path))
    if not dataset_root.is_dir():
        print(f"ERROR: dataset path not found: {dataset_root}",
              file=sys.stderr)
        sys.exit(1)

    audit = run_audit(
        config,
        dataset_root=dataset_root,
        duplicate_threshold=args.duplicate_threshold,
        difficulty_values=difficulty_values,
        now=args.timestamp,
    )
    audit_path = write_audit(audit, dataset_root)
    _print_report(audit, audit_path)

    summary = audit["summary"]
    if args.strict and (summary["errors"] or summary["warnings"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
