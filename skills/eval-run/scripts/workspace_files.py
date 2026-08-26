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
    Directory entries are copied recursively; nested symlinks are skipped
    with a warning.

    A *listed* file symlink whose resolved target is in the current case,
    a configured ``runner.plugin_dirs`` entry, or a project companion path
    outside the sibling-case dataset directory is materialized as a regular
    file (the live SKILL.md companion-file pattern). Listed directory
    symlinks, escaping/dangling/looping links, and nested (unlisted)
    symlinks are skipped with a warning (CWE-59).
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
    plugin_roots = _plugin_roots(config)
    project = Path(getattr(config, "project_root", Path.cwd())).resolve()
    for entry in files:
        # Shared {dest, source} entries (WorkspaceFile) are materialized
        # separately by agent_eval.workspace_provisioning; here we only handle
        # plain per-case string paths relative to the case directory.
        if not isinstance(entry, str):
            continue
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
                if not _is_listed_symlink_target_allowed(
                    target, case_root, plugin_roots, project
                ):
                    _warn_skip(
                        display,
                        f"resolves outside allowed companion paths: {target}",
                    )
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, dest)
        elif src.is_dir():
            _copy_tree(src, dest, display_prefix=display)
        elif src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


def _plugin_roots(config):
    """Validated ``runner.plugin_dirs`` roots for symlink targets."""
    project = Path(getattr(config, "project_root", Path.cwd())).resolve()
    runner = getattr(config, "runner", None)
    config_dir = getattr(config, "config_dir", None)
    roots = []
    for configured in getattr(runner, "plugin_dirs", None) or []:
        try:
            roots.append(
                resolve_plugin_path(configured, project, config_dir).resolve()
            )
        except (ValueError, OSError, TypeError):
            continue
    return roots


def _is_listed_symlink_target_allowed(target, case_root, plugin_roots, project):
    """Whether a listed file-symlink target may be copied into the workspace."""
    if target.is_relative_to(case_root):
        return True
    if any(target.is_relative_to(root) for root in plugin_roots):
        return True
    if not target.is_relative_to(project):
        return False
    # Project companions (e.g. skills/) are allowed, but not sibling cases.
    cases_dir = case_root.parent
    if target.is_relative_to(cases_dir) and not target.is_relative_to(case_root):
        return False
    return True


def _warn_skip(display, reason):
    print(
        f"WARNING: skipping workspace.files path {display}: {reason}",
        file=sys.stderr,
    )


def _copy_tree(src_dir, dest_dir, *, display_prefix=""):
    """Recursively copy a directory, skipping nested symlinks with a warning."""
    resolved_root = src_dir.resolve()
    for item in src_dir.rglob("*"):
        rel = item.relative_to(src_dir)
        display = f"{display_prefix}/{rel}" if display_prefix else str(rel)
        if item.is_symlink():
            _warn_skip(display, "nested symlink is not copied")
            continue
        if not item.is_file():
            continue
        try:
            resolved = item.resolve(strict=True)
        except (OSError, RuntimeError):
            _warn_skip(display, "could not resolve nested file")
            continue
        if not resolved.is_relative_to(resolved_root):
            _warn_skip(display, "nested file resolves outside its directory")
            continue
        dest = dest_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)
