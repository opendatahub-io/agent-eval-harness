#!/usr/bin/env python3
"""Assess which project skills are worth creating evals for.

Reads all SKILL.md files, extracts eval-relevant metadata (tools, complexity,
output type), and scores each skill to recommend whether evals would add value.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/assess_skills.py [--json]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from agent_eval.config import discover_configs

# Import skill discovery from sibling module
sys.path.insert(0, str(Path(__file__).parent))
from find_skills import list_skills

COMPLEXITY_KEYWORDS = {
    "diagnose", "classify", "analyze", "investigate",
    "generate", "refactor", "migrate", "transform",
    "validate", "assess", "recommend", "orchestrate",
    "multi-step", "pipeline", "workflow",
}

FILE_TOOLS = {"Write", "Edit", "NotebookEdit"}
EXEC_TOOLS = {"Bash"}
AGENT_TOOLS = {"Agent"}
ORCHESTRATION_TOOLS = {"Skill"}
ALL_TOOLS = FILE_TOOLS | EXEC_TOOLS | AGENT_TOOLS | ORCHESTRATION_TOOLS


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

    Missing allowed-tools means unrestricted (all tools) in Claude Code.
    """
    if "allowed-tools" not in fm:
        return ALL_TOOLS
    tools_raw = fm["allowed-tools"]
    if isinstance(tools_raw, list):
        return {str(t) for t in tools_raw if isinstance(t, (str, int, float))}
    return {t.strip() for t in str(tools_raw).split(",") if t.strip()}


_COMPLEXITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in sorted(COMPLEXITY_KEYWORDS)) + r")\b",
    re.IGNORECASE,
)


def _find_complexity_signals(body):
    """Find complexity keywords in the skill body (word-boundary match)."""
    return sorted({m.group().lower() for m in _COMPLEXITY_RE.finditer(body)})


def _is_thin_wrapper(fm, tools, line_count):
    """Detect skills that are thin wrappers around a single command."""
    if tools & (FILE_TOOLS | AGENT_TOOLS):
        return False
    desc = str(fm.get("description", "")).lower()
    words = set(desc.split())
    thin_signals = {"simple", "greeting"}
    return line_count < 50 and bool(words & thin_signals)


_SCRIPT_EXTS = {".py", ".sh", ".bash", ".js", ".ts", ".rb"}


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


def score_skill(meta):
    """Compute eval-worthiness score from skill metadata."""
    if meta["is_thin_wrapper"]:
        return 0, [f"{meta['line_count']} lines", "thin wrapper"]

    score = 0
    reasons = []

    if meta["produces_files"]:
        score += 1
        reasons.append("files")
    if meta["uses_agents"]:
        score += 1
        reasons.append("agents")
    if meta["uses_orchestration"]:
        score += 1
        reasons.append("orchestration")
    if meta["uses_bash"]:
        score += 1
        reasons.append("bash")
    tool_count = len(meta["allowed_tools"])
    if tool_count >= 5:
        score += 1
    if tool_count >= 8:
        score += 1
    reasons.append(f"{tool_count} tools")

    script_count = meta["script_count"]
    if script_count >= 2:
        score += 1
    if script_count >= 5:
        score += 1
    if script_count > 0:
        reasons.append(f"{script_count} scripts")

    signals = meta["complexity_signals"]
    signal_score = min(len(signals), 3)
    score += signal_score
    if signals:
        reasons.append("/".join(signals[:3]))

    return score, reasons


def recommend(score, has_eval):
    """Map score to recommendation."""
    if has_eval:
        return "EXISTS"
    if score >= 4:
        return "RECOMMENDED"
    if score >= 2:
        return "OPTIONAL"
    return "SKIP"


def assess_all():
    """Assess all project skills and return structured results."""
    skills = list_skills()
    eval_names = _build_eval_names()
    results = []

    for skill in skills:
        path = skill["path"]
        try:
            content = Path(path).read_text()

            fm = _parse_frontmatter(content)
            tools = _parse_tools(fm)

            parts = content.split("---", 2)
            body = parts[2] if len(parts) >= 3 else content
            line_count = len(content.splitlines())

            meta = {
                "name": skill["name"],
                "dir_name": skill["dir_name"],
                "path": skill["path"],
                "description": skill.get("description", ""),
                "line_count": line_count,
                "allowed_tools": sorted(tools),
                "produces_files": bool(tools & FILE_TOOLS),
                "uses_bash": bool(tools & EXEC_TOOLS),
                "uses_agents": bool(tools & AGENT_TOOLS),
                "uses_orchestration": bool(tools & ORCHESTRATION_TOOLS),
                "script_count": _count_scripts(path),
                "has_existing_eval": skill["dir_name"] in eval_names,
                "complexity_signals": _find_complexity_signals(body),
                "is_thin_wrapper": _is_thin_wrapper(fm, tools, line_count),
            }

            score, reasons = score_skill(meta)
            meta["eval_score"] = score
            meta["score_reasons"] = reasons
            meta["recommendation"] = recommend(score, meta["has_existing_eval"])

            results.append(meta)
        except Exception as exc:
            print(f"  WARNING: failed to assess {path}: {exc}", file=sys.stderr)
            continue

    results.sort(key=lambda r: (-r["eval_score"], r["dir_name"]))
    return results


def print_report(results):
    """Print a human-readable assessment report."""
    groups = {"EXISTS": [], "RECOMMENDED": [], "OPTIONAL": [], "SKIP": []}
    for r in results:
        groups[r["recommendation"]].append(r)

    labels = {
        "EXISTS": "ALREADY HAS EVALS:",
        "RECOMMENDED": "RECOMMENDED (evals will add value):",
        "OPTIONAL": "OPTIONAL (consider if heavily used):",
        "SKIP": "SKIP (linters are sufficient):",
    }

    print("Skill Assessment Report")
    print("=" * 65)

    for category in ["EXISTS", "RECOMMENDED", "OPTIONAL", "SKIP"]:
        items = groups[category]
        if not items:
            continue
        print(f"\n{labels[category]}")
        for r in items:
            reasons_str = ", ".join(r["score_reasons"]) if r["score_reasons"] else "minimal"
            print(f"  {r['name']:<35} score: {r['eval_score']}  ({reasons_str})")

    total = len(results)
    counts = {k: len(v) for k, v in groups.items()}
    print(f"\nSummary: {counts.get('RECOMMENDED', 0)} recommended, "
          f"{counts.get('OPTIONAL', 0)} optional, "
          f"{counts.get('SKIP', 0)} skip, "
          f"{counts.get('EXISTS', 0)} already have evals "
          f"({total} total)")


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
        sys.exit(1)
    else:
        print_report(results)


if __name__ == "__main__":
    main()
