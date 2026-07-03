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
import sys
from pathlib import Path

import yaml

# Import skill discovery from sibling module
sys.path.insert(0, str(Path(__file__).parent))
from find_skills import list_skills

COMPLEXITY_KEYWORDS = {
    "diagnose", "classify", "threat", "score",
    "assessment", "investigate", "severity", "root cause",
    "stride", "mitre",
}

FILE_TOOLS = {"Write", "Edit", "NotebookEdit"}
EXEC_TOOLS = {"Bash"}
AGENT_TOOLS = {"Agent"}


def _parse_frontmatter(content):
    """Extract frontmatter dict from SKILL.md content."""
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}


def _parse_tools(fm):
    """Extract allowed-tools list from frontmatter."""
    tools_raw = fm.get("allowed-tools", "")
    if isinstance(tools_raw, list):
        return set(tools_raw)
    return {t.strip() for t in str(tools_raw).split(",") if t.strip()}


def _find_complexity_signals(body):
    """Find complexity keywords in the skill body (case-insensitive)."""
    body_lower = body.lower()
    return sorted(kw for kw in COMPLEXITY_KEYWORDS if kw in body_lower)


def _is_thin_wrapper(fm, line_count):
    """Detect skills that are thin wrappers around a single command."""
    desc = str(fm.get("description", "")).lower()
    thin_signals = ["simple", "greeting", "label a ", "list "]
    return line_count < 50 and any(s in desc for s in thin_signals)


def _has_existing_eval(skill_path):
    """Check if an eval.yaml already exists for this skill."""
    skill_dir = Path(skill_path).parent
    plugin_dir = skill_dir.parent.parent
    skill_name = skill_dir.name

    candidates = [
        plugin_dir / "evals" / skill_name / "eval.yaml",
        plugin_dir / "evals" / f"{skill_name}.yaml",
        skill_dir / "eval.yaml",
    ]
    return any(c.exists() for c in candidates)


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
    if meta["line_count"] > 200:
        score += 1
    if meta["line_count"] > 400:
        score += 1
    reasons.append(f"{meta['line_count']} lines")

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
    results = []

    for skill in skills:
        path = skill["path"]
        try:
            content = Path(path).read_text()
        except OSError:
            continue

        fm = _parse_frontmatter(content)
        tools = _parse_tools(fm)

        parts = content.split("---", 2)
        body = parts[2] if len(parts) >= 3 else content
        line_count = len(content.splitlines())

        meta = {
            "name": skill["name"],
            "path": skill["path"],
            "description": skill.get("description", ""),
            "line_count": line_count,
            "allowed_tools": sorted(tools),
            "produces_files": bool(tools & FILE_TOOLS),
            "uses_bash": bool(tools & EXEC_TOOLS),
            "uses_agents": bool(tools & AGENT_TOOLS),
            "has_existing_eval": _has_existing_eval(path),
            "complexity_signals": _find_complexity_signals(body),
            "is_thin_wrapper": _is_thin_wrapper(fm, line_count),
        }

        score, reasons = score_skill(meta)
        meta["eval_score"] = score
        meta["score_reasons"] = reasons
        meta["recommendation"] = recommend(score, meta["has_existing_eval"])

        results.append(meta)

    results.sort(key=lambda r: (-r["eval_score"], r["name"]))
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

    if not results:
        print("No skills found in the project.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        json.dump(results, sys.stdout, indent=2)
        print()
    else:
        print_report(results)


if __name__ == "__main__":
    main()
