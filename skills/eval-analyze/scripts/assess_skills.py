#!/usr/bin/env python3
"""Extract eval-relevant metadata from all project skills.

Reads all SKILL.md files, extracts deterministic facts (tools, scripts,
existing eval configs), and outputs structured profiles. Judgment about
eval-worthiness is left to the LLM in SKILL.md.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/assess_skills.py [--json]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import sys
from pathlib import Path

import yaml

from agent_eval.config import discover_configs

sys.path.insert(0, str(Path(__file__).parent))
from find_skills import list_skills

FILE_TOOLS = {"Write", "Edit", "NotebookEdit"}
EXEC_TOOLS = {"Bash"}
AGENT_TOOLS = {"Agent"}
ORCHESTRATION_TOOLS = {"Skill"}
ALL_TOOLS = FILE_TOOLS | EXEC_TOOLS | AGENT_TOOLS | ORCHESTRATION_TOOLS

_SCRIPT_EXTS = {".py", ".sh", ".bash", ".js", ".ts", ".rb"}

BODY_EXCERPT_LIMIT = 500


def _parse_frontmatter(content):
    """Extract frontmatter dict from SKILL.md content."""
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1])
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def _parse_tools(fm):
    """Extract allowed-tools list from frontmatter.

    Missing or null allowed-tools means unrestricted (all tools) in Claude Code.
    """
    tools_raw = fm.get("allowed-tools")
    if tools_raw is None:
        return ALL_TOOLS
    if isinstance(tools_raw, list):
        return {str(t) for t in tools_raw if isinstance(t, (str, int, float))}
    if isinstance(tools_raw, str) and tools_raw.strip():
        return {t.strip() for t in tools_raw.split(",") if t.strip()}
    return ALL_TOOLS


def _extract_body(content):
    """Extract the SKILL.md body (after frontmatter)."""
    if not content.startswith("---"):
        return content
    parts = content.split("---", 2)
    return parts[2] if len(parts) >= 3 else content


def _count_scripts(skill_path):
    """Count script/code files in the skill directory tree."""
    skill_dir = Path(skill_path).parent
    count = 0
    for f in skill_dir.rglob("*"):
        if f.is_file() and f.suffix in _SCRIPT_EXTS:
            count += 1
    return count


def _build_eval_names():
    """Build the set of eval names from discover_configs (skill: field)."""
    return {r.eval_name for r in discover_configs(Path.cwd())}


def assess_all():
    """Extract metadata profiles for all project skills."""
    skills = list_skills()
    eval_names = _build_eval_names()
    results = []

    for skill in skills:
        path = skill["path"]
        try:
            content = Path(path).read_text(errors="replace")

            fm = _parse_frontmatter(content)
            tools = _parse_tools(fm)
            body = _extract_body(content)

            has_eval = skill["dir_name"] in eval_names

            meta = {
                "name": skill["name"],
                "dir_name": skill["dir_name"],
                "path": skill["path"],
                "description": skill.get("description", ""),
                "allowed_tools": sorted(tools),
                "produces_files": bool(tools & FILE_TOOLS),
                "uses_bash": bool(tools & EXEC_TOOLS),
                "uses_agents": bool(tools & AGENT_TOOLS),
                "uses_orchestration": bool(tools & ORCHESTRATION_TOOLS),
                "script_count": _count_scripts(path),
                "has_existing_eval": has_eval,
                "skill_body_excerpt": "<<<EXCERPT>>>"
                + body.strip()[:BODY_EXCERPT_LIMIT]
                + "<<<END_EXCERPT>>>",
            }

            if has_eval:
                meta["recommendation"] = "EXISTS"

            results.append(meta)
        except Exception as exc:
            print(f"  WARNING: failed to assess {path}: {exc}", file=sys.stderr)
            continue

    results.sort(key=lambda r: r["dir_name"])
    return results


def print_report(results):
    """Print a human-readable listing of skill profiles."""
    print("Skill Profiles")
    print("=" * 65)

    for r in results:
        status = "[EXISTS]" if r.get("recommendation") == "EXISTS" else ""
        tools_str = ", ".join(r["allowed_tools"])
        print(f"\n  {r['name']} {status}")
        print(f"    {r['description']}")
        print(f"    tools: {tools_str}")
        flags = []
        if r["produces_files"]:
            flags.append("files")
        if r["uses_bash"]:
            flags.append("bash")
        if r["uses_agents"]:
            flags.append("agents")
        if r["uses_orchestration"]:
            flags.append("orchestration")
        if flags:
            print(f"    capabilities: {', '.join(flags)}")
        print(f"    scripts: {r['script_count']}")

    total = len(results)
    exists = sum(1 for r in results if r.get("recommendation") == "EXISTS")
    print(f"\n{total} skills found ({exists} already have evals)")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON instead of formatted report")
    args = parser.parse_args()

    results = assess_all()

    if args.json:
        json.dump(results, sys.stdout, indent=2)
        print()
    elif not results:
        print("No skills found in the project.", file=sys.stderr)
    else:
        print_report(results)


if __name__ == "__main__":
    main()
