"""Helpers for copying input files into eval workspaces.

Extracted so tests can import without triggering agent_eval._bootstrap
side effects from workspace.py.
"""

import shutil
import sys
from pathlib import Path

# Files that contain evaluation material (answer keys, gold standards,
# annotations for judges).  These must never be copied into the solver
# workspace — the solver would have direct access to the answers.
_EVAL_ONLY_NAMES = {"answers", "annotations", "reference", "expected", "gold"}


def _copy_input_files(case_dir, workspace, config):
    """Copy workspace files from the case directory into the workspace.

    Iterates ``config.dataset.workspace.files`` and copies each listed
    path from *case_dir* into *workspace*, preserving relative structure.
    Directory entries are copied recursively.  Symlinks are skipped to
    prevent escaping the case directory.  Evaluation-only files (answer
    keys, annotations, gold standards) are silently skipped.
    """
    ds = getattr(config, "dataset", None)
    if ds is None:
        return
    ws = getattr(ds, "workspace", None)
    if ws is None:
        return
    files = ws.files or []
    if not files:
        return

    case_root = case_dir.resolve()
    for entry in files:
        # Reject "." — it copies the entire case dir including eval material
        if entry == "." or not Path(entry).parts:
            print(f"WARNING: skipping workspace.files entry '{entry}' "
                  f"(copies entire case dir including eval material)",
                  file=sys.stderr)
            continue
        src = case_dir / entry
        if src.is_symlink():
            continue
        # Skip eval-only files at any level
        if _is_eval_only(src):
            continue
        if src.is_dir():
            if not src.resolve().is_relative_to(case_root):
                continue
            _copy_tree(src, case_dir, workspace)
        elif src.is_file():
            if not src.resolve().is_relative_to(case_root):
                continue
            rel = src.relative_to(case_dir)
            dst = workspace / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _is_eval_only(path):
    """Check if a path matches an evaluation-only filename pattern."""
    return path.stem.lower() in _EVAL_ONLY_NAMES


def _copy_tree(src_dir, case_dir, workspace):
    """Recursively copy a directory, skipping symlinks and eval-only files."""
    resolved_root = src_dir.resolve()
    for item in src_dir.rglob("*"):
        if item.is_symlink():
            continue
        if not item.is_file():
            continue
        if not item.resolve().is_relative_to(resolved_root):
            continue
        if _is_eval_only(item):
            continue
        rel = item.relative_to(case_dir)
        dst = workspace / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dst)
