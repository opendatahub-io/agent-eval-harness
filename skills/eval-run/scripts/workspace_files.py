"""Helpers for copying input files into eval workspaces.

Extracted so tests can import the copier without pulling in workspace.py.
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import shutil
import sys
from pathlib import Path

from agent_eval.config import resolve_plugin_path


def _copy_input_files(case_dir, workspace, config):
    """Copy workspace files from the case directory into the workspace.

    Iterates ``config.dataset.workspace.files`` and copies each listed
    path from *case_dir* into *workspace*, preserving relative structure.
    Directory entries are copied recursively; nested symlinks are skipped.

    A *listed* file symlink whose resolved target stays inside the project
    root or a configured ``runner.plugin_dirs`` entry is materialized as a
    regular file (the live SKILL.md companion-file pattern). Listed
    directory symlinks, escaping/dangling/looping links, and nested
    (unlisted) symlinks are skipped with a warning (CWE-59).
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
    allowed_roots = _allowed_roots(config, case_root)
    for entry in files:
        rel = Path(entry)
        if rel.is_absolute() or ".." in rel.parts:
            _warn_skip(str(entry), "escapes the case directory")
            continue
        src = case_dir / rel
        dest = workspace / rel
        display = str(rel)
        if src.is_symlink():
            try:
                target = src.resolve(strict=True)
            except (OSError, RuntimeError):
                _warn_skip(display, "dangling or looping symlink")
                continue
            if target.is_dir():
                _warn_skip(display, "directory symlink is not copied")
            elif target.is_file():
                if not _is_contained(target, allowed_roots):
                    _warn_skip(
                        display,
                        f"resolves outside project/plugin: {target}",
                    )
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, dest)
        elif src.is_dir():
            _copy_tree(src, dest)
        elif src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


def _allowed_roots(config, case_root):
    """Roots a listed file-symlink target may resolve into."""
    roots = [case_root]
    project = Path(getattr(config, "project_root", Path.cwd())).resolve()
    roots.append(project)
    runner = getattr(config, "runner", None)
    config_dir = getattr(config, "config_dir", None)
    for configured in getattr(runner, "plugin_dirs", None) or []:
        try:
            roots.append(
                resolve_plugin_path(configured, project, config_dir).resolve()
            )
        except (ValueError, OSError, TypeError):
            continue
    return roots


def _is_contained(path, roots):
    return any(path.is_relative_to(root) for root in roots)


def _warn_skip(display, reason):
    print(
        f"WARNING: skipping workspace.files path {display}: {reason}",
        file=sys.stderr,
    )


def _copy_tree(src_dir, dest_dir):
    """Recursively copy a directory, skipping nested symlinks."""
    resolved_root = src_dir.resolve()
    for item in src_dir.rglob("*"):
        if item.is_symlink() or not item.is_file():
            continue
        try:
            resolved = item.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(resolved_root):
            continue
        dest = dest_dir / item.relative_to(src_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)
