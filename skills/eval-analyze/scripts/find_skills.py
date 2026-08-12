#!/usr/bin/env python3
"""Find skills in the current project.

Reads .claude-plugin/plugin.json for custom skill paths, falls back to
default locations (.claude/skills, skills). Excludes eval harness skills.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/find_skills.py [--name <skill>]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import sys
from glob import glob
from pathlib import Path

import yaml

# Default directories where skills live in a project
DEFAULT_SKILL_DIRS = [".claude/skills", "skills"]

# Skills from the eval harness — excluded from discovery
HARNESS_SKILLS = {"eval-setup", "eval-analyze", "eval-dataset", "eval-run",
                   "eval-review", "eval-mlflow", "eval-optimize"}


def _resolve_under_cwd(raw, base):
    """Resolve a path relative to base, rejecting traversal outside CWD."""
    candidate = (base / Path(raw)).resolve()
    try:
        candidate.relative_to(Path.cwd().resolve())
    except ValueError:
        return None
    return candidate


def _is_under_cwd(path, trusted_roots=()):
    """Resolve path and validate it is within CWD or a trusted root.

    Always resolves symlinks before checking containment — a lexical
    prefix match on an unresolved path would let a symlink escape CWD.
    Returns the resolved Path if valid, None otherwise.

    trusted_roots: resolved Paths for skill directories discovered via
    plugin.json or marketplace.json (already validated by _resolve_under_cwd).
    """
    resolved = Path(path).resolve()
    cwd = Path.cwd().resolve()
    try:
        resolved.relative_to(cwd)
        return resolved
    except ValueError:
        pass
    for root in trusted_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    return None


def _skills_from_plugin_json(plugin_json):
    """Extract skill directories from a plugin.json file.

    Returns a list of skill dir paths (relative to CWD) or None.
    """
    try:
        with open(plugin_json) as f:
            manifest = json.load(f)
        skills_field = manifest.get("skills")
        if skills_field:
            plugin_root = plugin_json.parent.parent
            if isinstance(skills_field, str):
                p = _resolve_under_cwd(skills_field, plugin_root)
                return [str(p)] if p else None
            elif isinstance(skills_field, list):
                result = []
                for s in skills_field:
                    if not isinstance(s, str):
                        continue
                    p = _resolve_under_cwd(s, plugin_root)
                    if p:
                        result.append(str(p))
                return result or None
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: failed to read {plugin_json}: {e}", file=sys.stderr)
    return None


def _discover_via_marketplace():
    """Follow marketplace.json source paths to find nested plugin skill dirs."""
    marketplace = Path(".claude-plugin/marketplace.json")
    if not marketplace.exists():
        return []

    try:
        with open(marketplace) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: failed to read {marketplace}: {e}", file=sys.stderr)
        return []

    dirs = []
    for plugin in data.get("plugins", []):
        source = plugin.get("source", "")
        # Marketplace entries can reference remote plugins with a structured
        # source object. Only string sources name local paths discoverable from
        # this checkout; remote entries are not validation errors.
        if not isinstance(source, str) or not source:
            continue
        source_path = _resolve_under_cwd(source, Path.cwd())
        if not source_path:
            continue
        nested_pj = source_path / ".claude-plugin" / "plugin.json"
        if nested_pj.exists():
            from_pj = _skills_from_plugin_json(nested_pj)
            if from_pj:
                dirs.extend(from_pj)
                continue
        # Default: <source>/skills/
        default_skills = source_path / "skills"
        if default_skills.is_dir():
            dirs.append(str(default_skills))
    return dirs


def get_skill_dirs():
    """Get skill directories for the current project.

    Priority:
    1. Root .claude-plugin/plugin.json 'skills' field
    2. Nested plugins discovered via marketplace.json source paths
    3. Default locations (.claude/skills, skills)
    """
    plugin_json = Path(".claude-plugin/plugin.json")
    from_root = _skills_from_plugin_json(plugin_json) if plugin_json.exists() else None
    from_root = [d for d in (from_root or []) if Path(d).is_dir()]
    if from_root:
        return from_root

    from_marketplace = _discover_via_marketplace()
    from_marketplace = [d for d in from_marketplace if Path(d).is_dir()]
    if from_marketplace:
        return from_marketplace

    return DEFAULT_SKILL_DIRS


def find_skill(name):
    """Find a skill's SKILL.md by name.

    Handles multiple naming conventions:
    - Directory name: "enhancer" -> skills/enhancer/SKILL.md
    - Plugin invocation: "skill:enhance" -> strip prefix, match directory or frontmatter name

    Returns the resolved Path to SKILL.md or None if not found.
    """
    candidates = [name]
    if ":" in name:
        candidates.append(name.split(":", 1)[1])

    skill_dirs = get_skill_dirs()
    trusted = frozenset(Path(d).resolve() for d in skill_dirs)

    for skills_dir in skill_dirs:
        for candidate in candidates:
            skill_path = Path(skills_dir) / candidate / "SKILL.md"
            resolved = _is_under_cwd(skill_path, trusted)
            if resolved and resolved.is_file():
                return resolved

        for path in sorted(glob(f"{skills_dir}/*/SKILL.md")):
            resolved = _is_under_cwd(path, trusted)
            if not resolved:
                print(f"  WARNING: skipping '{path}': it resolves outside the "
                      f"project (a symlink escaping the project boundary), "
                      f"excluded for safety. If this is an intended "
                      f"shared/monorepo skill, point a skills dir at its real "
                      f"location instead of symlinking it in.",
                      file=sys.stderr)
                continue
            try:
                with open(resolved) as f:
                    content = f.read()
                if content.startswith("---"):
                    fm = yaml.safe_load(content.split("---")[1])
                    fm_name = (fm or {}).get("name", "")
                    if fm_name == name or fm_name in candidates:
                        return resolved
            except Exception as e:
                print(f"  WARNING: failed to parse {path}: {e}", file=sys.stderr)
                continue
    return None


def list_skills():
    """List all project skills (excluding harness skills).

    Returns list of dicts: [{name, path, description}, ...]
    Path values are resolved (symlinks followed) to match the validation check.
    """
    skills = []
    skill_dirs = get_skill_dirs()
    trusted = frozenset(Path(d).resolve() for d in skill_dirs)

    for skills_dir in skill_dirs:
        for path in sorted(glob(f"{skills_dir}/*/SKILL.md")):
            resolved = _is_under_cwd(path, trusted)
            if not resolved:
                print(f"  WARNING: skipping '{path}': it resolves outside the "
                      f"project (a symlink escaping the project boundary), "
                      f"excluded for safety. If this is an intended "
                      f"shared/monorepo skill, point a skills dir at its real "
                      f"location instead of symlinking it in.",
                      file=sys.stderr)
                continue
            dir_name = resolved.parent.name
            if dir_name in HARNESS_SKILLS:
                continue
            desc = ""
            display_name = dir_name
            try:
                with open(resolved) as f:
                    content = f.read()
                if content.startswith("---"):
                    fm = yaml.safe_load(content.split("---")[1])
                    desc = (fm or {}).get("description", "")[:80]
                    fm_name = (fm or {}).get("name", "")
                    if fm_name:
                        display_name = fm_name
            except Exception as e:
                print(f"  WARNING: failed to parse {path}: {e}", file=sys.stderr)
            skills.append({"name": display_name, "dir_name": dir_name,
                           "path": str(resolved), "description": desc})
    return skills


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default=None,
                        help="Find a specific skill by name")
    args = parser.parse_args()

    if args.name:
        path = find_skill(args.name)
        if path:
            print(f"FOUND: {path}")
        else:
            print(f"NOT_FOUND: {args.name}")
            sys.exit(1)
    else:
        skills = list_skills()
        if skills:
            for s in skills:
                print(f"SKILL: {s['name']:<30} {s['description']}")
        else:
            print("NONE: no skills found")


if __name__ == "__main__":
    main()
