#!/usr/bin/env python3
"""Collect artifacts from workspace and distribute to per-case directories.

Reads output paths from eval.yaml. Maps files to cases using
case_order.yaml (positional: Nth file group → Nth case).

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/collect.py \\
        --config eval.yaml \\
        --workspace /tmp/agent-eval/test-001 \\
        --output eval/runs/test-001
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig
from agent_eval.events import (
    DEFAULT_RESULT_CAP, parse_stream_events, merge_subagent_transcripts,
)

# Files/dirs created by the harness infrastructure, not by the skill
_HARNESS_PATHS = {
    ".claude", ".git", ".work", "subagents", "hooks",
    "stdout.log", "stderr.log",
    "run_result.json", "batch.yaml", "case_order.yaml",
}


def _safe_path_component(value, field):
    """Reject path components that escape the parent directory."""
    p = Path(str(value))
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"{field} must be a relative path without '..': {value}")
    return str(p)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)
    workspace = Path(args.workspace)
    output_dir = Path(args.output)

    # Per-case mode: outputs are already in case workspaces
    # (Works for both skill and prompt execution in case mode)
    if config.execution.mode == "case":
        _collect_per_case(workspace, output_dir, config)
        return

    # ── Batch mode (below) ───────────────────────────────────────

    # Load case order
    order_path = workspace / "case_order.yaml"
    if not order_path.exists():
        print("ERROR: no case_order.yaml in workspace", file=sys.stderr)
        sys.exit(1)

    with open(order_path) as f:
        case_order = yaml.safe_load(f)

    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}

    # Validate case IDs from case_order
    for entry in case_order:
        cid = entry["case_id"] if isinstance(entry, dict) else entry
        _safe_path_component(cid, "case_id")

    # Collect from each output path defined in config (directory or file)
    for output_cfg in config.outputs:
        out_path = _safe_path_component(output_cfg.path or ".", "output path")
        src = workspace / out_path
        if not src.exists():
            continue

        # Handle single-file output paths
        if src.is_file():
            files = [src]
            src_dir = src.parent
        else:
            src_dir = src
            # Get all files in the output directory (including subdirs)
            files = sorted(f for f in src_dir.rglob("*") if f.is_file())
        if not files:
            continue

        if output_cfg.batch_pattern:
            # Batch mode: use the configured pattern to map files to cases
            mapping = _collect_batch(files, case_order, output_cfg.batch_pattern)
        else:
            # Auto-detect mode: group files by common prefix
            groups = _group_files(files, len(case_order))
            mapping = {}
            for i, group in enumerate(groups):
                if i >= len(case_order):
                    break
                entry = case_order[i]
                cid = entry["case_id"] if isinstance(entry, dict) else entry
                mapping[cid] = group

        for case_id, matched_files in mapping.items():
            if not matched_files:
                continue
            case_output = output_dir / "cases" / case_id / out_path
            case_output.mkdir(parents=True, exist_ok=True)

            for src_file in matched_files:
                # Preserve subdirectory structure relative to src_dir
                rel = src_file.relative_to(src_dir)
                dest = case_output / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest)

            results.setdefault(case_id, {})[out_path] = len(matched_files)

    # Save collection summary
    with open(output_dir / "collection.json", "w") as f:
        json.dump(results, f, indent=2)

    for case_id, info in sorted(results.items()):
        counts = ", ".join(f"{k}={v}" for k, v in info.items())
        print(f"  {case_id}: {counts}")

    # Generate events.json from run-level stdout.log (shared across all cases)
    if config.traces.events:
        _generate_events_json(workspace, output_dir, config)

    if not results:
        print("WARNING: no artifacts collected", file=sys.stderr)


def _collect_per_case(workspace, output_dir, config):
    """Collect artifacts from per-case workspaces.

    In per-case mode, each case has its own workspace at
    workspace/cases/{case_id}/. The skill writes outputs relative to
    its cwd (the case workspace). We scan for output files matching
    the configured output paths and copy them to the run output dir.
    """
    cases_dir = workspace / "cases"
    if not cases_dir.exists():
        print("ERROR: no cases/ directory in workspace", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}

    for case_dir in sorted(d for d in cases_dir.iterdir() if d.is_dir()):
        case_id = case_dir.name

        for output_cfg in config.outputs:
            if not output_cfg.path:
                continue  # Skip tool-only outputs (no filesystem path)
            out_path = _safe_path_component(output_cfg.path, "output path")
            src = case_dir / out_path

            if not src.exists():
                continue

            if src.is_file():
                files = [src]
                src_base = src.parent
            else:
                files = sorted(f for f in src.rglob("*") if f.is_file())
                src_base = src

            if not files:
                continue

            case_output = output_dir / "cases" / case_id / out_path
            case_output.mkdir(parents=True, exist_ok=True)

            for f in files:
                # Reject symlinks (CWE-59)
                if f.is_symlink():
                    continue
                rel = f.relative_to(src_base)
                dest = case_output / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)

            results.setdefault(case_id, {})[out_path] = len(files)

        # Collect files modified in-place during execution
        modified = _collect_modified_files(case_dir, config)
        if modified:
            mod_output = output_dir / "cases" / case_id / "_modified"
            mod_output.mkdir(parents=True, exist_ok=True)
            for rel_path, abs_path in modified:
                dest = mod_output / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abs_path, dest)
            results.setdefault(case_id, {})["_modified"] = len(modified)

        # Generate events.json from stdout.log
        if config.traces.events:
            _generate_events_json(case_dir, output_dir / "cases" / case_id,
                                  config)

    # Save collection summary
    with open(output_dir / "collection.json", "w") as f:
        json.dump(results, f, indent=2)

    for case_id, info in sorted(results.items()):
        counts = ", ".join(f"{k}={v}" for k, v in info.items())
        print(f"  {case_id}: {counts}")

    if not results:
        print("WARNING: no artifacts collected", file=sys.stderr)


def _generate_events_json(case_dir, output_case_dir, config):
    """Parse stdout.log into events.json using atomic write."""
    # In case mode, execute.py writes stdout.log to the output directory,
    # not the workspace. Check there first, fall back to workspace.
    stdout_path = output_case_dir / "stdout.log"
    if not stdout_path.exists():
        stdout_path = case_dir / "stdout.log"
    if not stdout_path.exists():
        output_case_dir.mkdir(parents=True, exist_ok=True)
        dest = output_case_dir / "events.json"
        fd, tmp_path = tempfile.mkstemp(dir=str(output_case_dir), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump([], f)
        os.rename(tmp_path, str(dest))
        return

    try:
        stdout_text = stdout_path.read_text()
    except OSError:
        return

    events = parse_stream_events(stdout_text)

    subagent_dir = case_dir / "subagents"
    if subagent_dir.is_dir():
        events = merge_subagent_transcripts(events, str(subagent_dir),
                                            result_cap=DEFAULT_RESULT_CAP)

    output_case_dir.mkdir(parents=True, exist_ok=True)
    dest = output_case_dir / "events.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(output_case_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(events, f, indent=2)
        os.rename(tmp_path, str(dest))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _collect_modified_files(case_dir, config):
    """Find files modified or created during skill execution.

    Uses git diff against the initial commit (created by workspace.py)
    to detect in-place edits. Filters out harness infrastructure and
    configured output directories.
    """
    git_dir = case_dir / ".git"
    if not git_dir.exists():
        return []

    output_prefixes = {
        tuple(Path(o.path).parts)
        for o in config.outputs if o.path
    }

    try:
        # Modified tracked files
        diff = subprocess.run(
            ["git", "-C", str(case_dir), "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        # New untracked files (excluding harness paths)
        untracked = subprocess.run(
            ["git", "-C", str(case_dir), "ls-files",
             "--others", "--exclude-standard"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  {case_dir.name}: no modified files (git diff failed: {e})",
              file=sys.stderr)
        return []

    all_changed = set()
    for line in (diff.stdout + untracked.stdout).splitlines():
        line = line.strip()
        if line:
            all_changed.add(line)

    result = []
    for rel in sorted(all_changed):
        parts = Path(rel).parts
        if not parts:
            continue
        if ".." in parts:
            continue
        if parts[0] in _HARNESS_PATHS:
            continue
        if any(parts[:len(op)] == op for op in output_prefixes):
            continue
        candidate = case_dir / rel
        if candidate.is_symlink():
            continue
        has_symlink_ancestor = False
        for p in candidate.parents:
            if p == case_dir:
                break
            if p.is_symlink():
                has_symlink_ancestor = True
                break
        if has_symlink_ancestor:
            continue
        abs_path = candidate.resolve()
        if not abs_path.is_relative_to(case_dir.resolve()):
            continue
        if abs_path.is_file():
            result.append((rel, abs_path))

    return result


def _collect_batch(files, case_order, batch_pattern):
    """Map files to cases using a batch_pattern from eval.yaml.

    Args:
        files: sorted list of Path objects in the output directory
        case_order: list of {"case_id": str, "entry_count": int} dicts
        batch_pattern: pattern with {n} placeholder (1-based index),
                       or "*" for shared directories

    Returns:
        dict mapping case_id → list of matching Path objects
    """
    # Shared: all files go to every case
    if batch_pattern == "*":
        return {
            (e["case_id"] if isinstance(e, dict) else e): list(files)
            for e in case_order
        }

    result = {}
    batch_idx = 1
    for entry in case_order:
        case_id = entry["case_id"] if isinstance(entry, dict) else entry
        count = entry.get("entry_count", 1) if isinstance(entry, dict) else 1

        matching = []
        for n in range(batch_idx, batch_idx + count):
            try:
                prefix = batch_pattern.format(n=n)
            except (KeyError, ValueError):
                prefix = batch_pattern.replace("{n}", str(n))
            matching.extend(f for f in files if f.name.startswith(prefix))

        if matching:
            result[case_id] = matching
        batch_idx += count

    return result


def _group_files(files, num_cases):
    """Group files into per-case bundles.

    Tries to detect a common ID prefix pattern (e.g., "RFE-001", "TASK-002").
    If found, groups by prefix. Otherwise, distributes one file per case.
    """
    import re

    # Try to find a common prefix pattern: WORD-NNN at start of filename
    prefixes = {}
    for f in files:
        match = re.match(r'^([A-Za-z]+-\d+)', f.stem)
        if match:
            prefix = match.group(1)
            prefixes.setdefault(prefix, []).append(f)

    # If we found prefix groups matching the case count, use them
    if prefixes and len(prefixes) >= num_cases * 0.5:
        return [prefixes[k] for k in sorted(prefixes.keys())]

    # Fallback: one file per case (positional)
    return [[f] for f in files]


if __name__ == "__main__":
    main()
