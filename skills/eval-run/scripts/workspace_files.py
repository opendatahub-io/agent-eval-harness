"""Helpers for copying input files into eval workspaces.

Extracted so tests can import without triggering agent_eval._bootstrap
side effects from workspace.py.
"""

import shutil
from pathlib import Path


def _copy_input_files(case_dir, workspace, config):
    """Copy the input files directory into the workspace.

    If the case directory contains a subdirectory matching
    ``config.dataset_input_files_dir`` (default: ``files/``), its contents
    are recursively copied into the workspace root, preserving relative
    paths.  This allows test cases to ship source code, config files, or
    other artifacts the agent needs without embedding them in input.yaml.

    Symlinks are skipped to prevent copying targets outside the files
    directory.
    """
    dir_name = getattr(config, "dataset_input_files_dir", "files") or "files"
    files_dir = case_dir / dir_name
    if not files_dir.is_dir():
        return
    resolved_root = files_dir.resolve()
    for item in files_dir.rglob("*"):
        if item.is_symlink():
            continue
        if not item.is_file():
            continue
        if not item.resolve().is_relative_to(resolved_root):
            continue
        rel = item.relative_to(files_dir)
        dst = workspace / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dst)
