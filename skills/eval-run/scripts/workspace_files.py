"""Helpers for copying input files into eval workspaces.

Extracted so tests can import without triggering agent_eval._bootstrap
side effects from workspace.py.
"""

import shutil


def _copy_input_files(case_dir, workspace, config):
    """Copy workspace files from the case directory into the workspace.

    Iterates ``config.dataset.workspace.files`` and copies each listed
    path from *case_dir* into *workspace*, preserving relative structure.
    Directory entries are copied recursively.  Symlinks are skipped to
    prevent escaping the case directory.
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
        src = case_dir / entry
        if src.is_symlink():
            continue
        if src.is_dir():
            _copy_tree(src, case_dir, workspace, case_root)
        elif src.is_file():
            if not src.resolve().is_relative_to(case_root):
                continue
            rel = src.relative_to(case_dir)
            dst = workspace / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _copy_tree(src_dir, case_dir, workspace, case_root):
    """Recursively copy a directory, skipping symlinks."""
    resolved_root = src_dir.resolve()
    for item in src_dir.rglob("*"):
        if item.is_symlink():
            continue
        if not item.is_file():
            continue
        if not item.resolve().is_relative_to(resolved_root):
            continue
        rel = item.relative_to(case_dir)
        dst = workspace / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dst)
