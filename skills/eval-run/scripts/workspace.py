#!/usr/bin/env python3
"""Prepare an isolated workspace for skill evaluation.

Reads eval.yaml for dataset path and output directories.
For each case, includes the full input file content in batch.yaml —
no field extraction or schema interpretation.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/workspace.py \\
        --config eval.yaml \\
        --run-id test-001 \\
        [--cases case-001] \\
        [--symlinks scripts,.claude,CLAUDE.md]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig
from workspace_files import _copy_input_files


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cases", nargs="*", default=None)
    parser.add_argument(
        "--symlinks",
        default=None,
        help="Comma-separated dirs/files to symlink into workspace "
        "(default: scripts,.claude,CLAUDE.md,.context,skills)",
    )
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)

    cases_dir = config.resolve_path(config.dataset.path)
    if not cases_dir.exists():
        print(f"ERROR: dataset path not found: {cases_dir}", file=sys.stderr)
        sys.exit(1)

    # Find cases (each subdirectory is a case)
    case_dirs = sorted(d for d in cases_dir.iterdir() if d.is_dir())
    if args.cases:
        filter_set = set(args.cases)
        case_dirs = [c for c in case_dirs if c.name in filter_set]

    if not case_dirs:
        print("ERROR: no cases found", file=sys.stderr)
        sys.exit(1)

    # Validate run-id
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.run_id):
        print("ERROR: run-id must match [A-Za-z0-9._-]+", file=sys.stderr)
        sys.exit(1)

    # Create workspace in secure temp directory
    base_dir = (Path(tempfile.gettempdir()) / "agent-eval").resolve()
    workspace = (base_dir / args.run_id).resolve()
    if base_dir not in workspace.parents and workspace != base_dir:
        print("ERROR: invalid run-id path", file=sys.stderr)
        sys.exit(1)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, mode=0o700)

    # Initialize a bare git repo so Claude Code subagents can discover
    # the project root and load .claude/settings.json with the expanded
    # permission patterns (e.g. /private/tmp variants on macOS).
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)

    # Branch on execution mode
    if config.execution.mode == "case":
        _create_per_case_workspace(workspace, case_dirs, config, args)
        return

    # ── Batch mode (below) ───────────────────────────────────────

    if config.dataset.workspace.files:
        print(
            "WARNING: dataset.workspace.files is ignored in batch mode — "
            "files are per-case but batch mode uses a shared workspace. "
            "Use execution.mode: case instead.",
            file=sys.stderr,
        )

    # Create output directories from config
    for output in config.outputs:
        if output.path and output.path != ".":
            out = workspace / output.path
            # If the path has a file extension (e.g., review-report.html),
            # create the parent directory instead of treating it as a dir.
            if out.suffix:
                out.parent.mkdir(parents=True, exist_ok=True)
            else:
                out.mkdir(parents=True, exist_ok=True)

    # Build batch entries — include full input file content per case
    batch_entries = []
    case_order = []

    for case_dir in case_dirs:
        # Find the input file (first .yaml or .json in the case dir)
        input_content = _read_input(case_dir, config)
        if input_content is None:
            continue

        # Flatten list inputs so batch.yaml is a single flat list
        if isinstance(input_content, list):
            batch_entries.extend(input_content)
            case_order.append(
                {"case_id": case_dir.name, "entry_count": len(input_content)}
            )
        else:
            batch_entries.append(input_content)
            case_order.append({"case_id": case_dir.name, "entry_count": 1})

    # Write batch.yaml
    with open(workspace / "batch.yaml", "w") as f:
        yaml.dump(
            batch_entries, f, default_flow_style=False, allow_unicode=True, width=120
        )

    # Write case order
    with open(workspace / "case_order.yaml", "w") as f:
        yaml.dump(case_order, f, default_flow_style=False)

    # Symlink project resources into workspace
    # Skip .claude when tool hooks are configured — _setup_tool_hooks
    # creates its own .claude/settings.json and symlinking would write
    # into the project's .claude/ directory instead
    project_root = Path.cwd()
    default_symlinks = ["scripts", ".claude", "CLAUDE.md", ".context", "skills"]
    # Always skip .claude symlink — we create our own settings.json
    # (for SubagentStop hook at minimum, plus tool interception if configured)
    skip_symlinks = {".claude"}
    symlink_names = (
        [s.strip() for s in args.symlinks.split(",") if s.strip()]
        if args.symlinks
        else default_symlinks
    )
    for name in symlink_names:
        if name in skip_symlinks:
            continue
        p = Path(name)
        if p.is_absolute() or ".." in p.parts:
            print(f"WARNING: skipping invalid symlink entry: {name}", file=sys.stderr)
            continue
        target = project_root / name
        link = workspace / name
        if target.exists():
            link.symlink_to(target.resolve())

    # When .claude is skipped for hooks, symlink subdirectories (e.g. skills/)
    if ".claude" in skip_symlinks:
        claude_dir = project_root / ".claude"
        if claude_dir.is_dir():
            for sub in claude_dir.iterdir():
                if sub.is_dir() and sub.name != "settings.json":
                    link = workspace / ".claude" / sub.name
                    if not link.exists():
                        link.parent.mkdir(parents=True, exist_ok=True)
                        link.symlink_to(sub.resolve())

    # Runner-specific workspace configuration (hooks, permissions, config files)
    _runner_setup(workspace, config)

    print(f"WORKSPACE: {workspace}")
    print(f"CASES: {len(case_dirs)}")
    print(f"BATCH: {workspace / 'batch.yaml'}")


def _create_per_case_workspace(workspace, case_dirs, config, args):
    """Create a separate workspace per case for per-case execution.

    Each case gets its own workspace subdirectory with:
    - All files from the dataset case directory (input.yaml, strategy.md, etc.)
    - Symlinked project resources (scripts, skills, .context, CLAUDE.md)
    - Tool interception hooks and SubagentStop hook
    - Output directories from eval.yaml
    """
    project_root = Path.cwd()
    default_symlinks = ["scripts", ".claude", "CLAUDE.md", ".context", "skills"]
    symlink_names = (
        [s.strip() for s in args.symlinks.split(",") if s.strip()]
        if args.symlinks
        else default_symlinks
    )

    case_order = []

    for case_dir in case_dirs:
        case_id = case_dir.name
        case_ws = workspace / "cases" / case_id
        case_ws.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(case_ws)], check=True)

        # Copy only the input file and answers.yaml into the workspace.
        # Companion files (e.g., source code for autofix skills) should
        # use dataset.input_files_dir (see #70) to explicitly declare
        # which files the skill needs. Everything else (gold standards,
        # reference docs, annotations) is evaluation material.
        input_src = _find_input_file(case_dir)
        if input_src:
            shutil.copy2(input_src, case_ws / input_src.name)
        answers_src = case_dir / "answers.yaml"
        if answers_src.is_file():
            shutil.copy2(answers_src, case_ws / "answers.yaml")

        # Copy input files directory if present (e.g., source code for the
        # agent to work on). Contents are placed at the workspace root,
        # preserving relative paths within the directory.
        _copy_input_files(case_dir, case_ws, config)

        # Create output directories
        for output in config.outputs:
            if output.path and output.path != ".":
                out = case_ws / output.path
                if out.suffix:
                    out.parent.mkdir(parents=True, exist_ok=True)
                else:
                    out.mkdir(parents=True, exist_ok=True)

        # Snapshot initial state so collect.py can diff for in-place edits
        _git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "eval-harness",
            "GIT_AUTHOR_EMAIL": "eval@harness",
            "GIT_COMMITTER_NAME": "eval-harness",
            "GIT_COMMITTER_EMAIL": "eval@harness",
        }
        subprocess.run(
            ["git", "-C", str(case_ws), "add", "-A"], check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(case_ws),
                "commit",
                "-q",
                "-m",
                "initial",
                "--allow-empty",
            ],
            check=True,
            capture_output=True,
            env=_git_env,
        )

        # Symlink project resources (skip .claude — we create our own)
        for name in symlink_names:
            if name == ".claude":
                continue
            p = Path(name)
            if p.is_absolute() or ".." in p.parts:
                continue
            target = project_root / name
            link = case_ws / name
            if target.exists() and not link.exists():
                link.symlink_to(target.resolve())

        # Symlink .claude subdirectories (skills/, etc.)
        claude_dir = project_root / ".claude"
        if claude_dir.is_dir():
            for sub in claude_dir.iterdir():
                if sub.is_dir() and sub.name != "settings.json":
                    link = case_ws / ".claude" / sub.name
                    if not link.exists():
                        link.parent.mkdir(parents=True, exist_ok=True)
                        link.symlink_to(sub.resolve())

        # Runner-specific workspace configuration
        _runner_setup(case_ws, config)

        case_order.append({"case_id": case_id})

    # Write case order at the parent workspace level
    with open(workspace / "case_order.yaml", "w") as f:
        yaml.dump(case_order, f, default_flow_style=False)

    print(f"WORKSPACE: {workspace}")
    print(f"MODE: case")
    print(f"CASES: {len(case_dirs)}")
    for entry in case_order:
        print(f"  {entry['case_id']}: {workspace / 'cases' / entry['case_id']}")


def _find_input_file(case_dir):
    """Find the input file in a case directory. Returns Path or None."""
    for suffix in (".yaml", ".yml", ".json"):
        candidate = case_dir / f"input{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _read_input(case_dir, config=None):
    """Read the input file from a case directory.

    Returns the parsed content (dict) or None if no input file found.
    Prefers files named 'input.*', then falls back to first parseable file.
    Skips known non-input files like answers.yaml and reference.*.
    When *config* is provided, workspace file roots are also skipped.
    """
    _SKIP_NAMES = {"answers", "reference", "expected", "gold"}
    if config is not None:
        ws = getattr(getattr(config, "dataset", None), "workspace", None)
        _SKIP_NAMES = _SKIP_NAMES | {
            Path(f).parts[0] for f in (getattr(ws, "files", None) or [])
        }

    # First pass: look for a file named 'input.*'
    for suffix in (".yaml", ".yml", ".json"):
        candidate = case_dir / f"input{suffix}"
        if candidate.is_file():
            data = _parse_file(candidate)
            if data is not None:
                return data

    # Second pass: first parseable data file, skipping known non-inputs
    for name in sorted(case_dir.iterdir()):
        if not name.is_file() or name.stem in _SKIP_NAMES:
            continue
        if name.suffix in (".yaml", ".yml", ".json"):
            data = _parse_file(name)
            if data is not None:
                return data
    return None


def _parse_file(path):
    """Parse a YAML or JSON file, returning the data or None on error."""
    try:
        if path.suffix in (".yaml", ".yml"):
            with open(path) as f:
                return yaml.safe_load(f)
        elif path.suffix == ".json":
            import json

            with open(path) as f:
                return json.load(f)
    except Exception as e:
        print(f"WARNING: failed to parse {path}: {e}", file=sys.stderr)
    return None


def _runner_setup(workspace, config):
    """Delegate workspace configuration to the runner implementation.

    Each runner writes its own config files, hooks, and permissions.
    """
    from agent_eval.agent import RUNNERS
    runner_cls = RUNNERS.get(config.runner.type)
    if not runner_cls:
        print(f"WARNING: unknown runner '{config.runner.type}', "
              f"skipping workspace setup", file=sys.stderr)
        return
    runner = runner_cls.from_config(config)
    runner.setup_workspace(
        workspace, config,
        project_root=Path.cwd(),
        interceptor_src=Path(__file__).parent / "tools.py",
    )


if __name__ == "__main__":
    main()
