#!/usr/bin/env python3
"""Scan a Claude Code project for configuration artifacts and report a harness inventory."""

import argparse
import json
import sys
from pathlib import Path

MAX_FILE_SIZE = 1_000_000  # 1MB limit per file read


def _read_text_safe(path: Path) -> str:
    """Read file text with size limit to avoid memory issues."""
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return ""
        return path.read_text()
    except (OSError, PermissionError, UnicodeDecodeError):
        return ""


def _parse_frontmatter_description(content: str) -> str:
    """Extract description from YAML frontmatter using yaml.safe_load."""
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        return ""
    end = None
    for i, line in enumerate(lines[1:], 1):
        if line == "---":
            end = i
            break
    if end is None:
        return ""
    frontmatter_text = "\n".join(lines[1:end])
    try:
        import yaml
        parsed = yaml.safe_load(frontmatter_text)
        if isinstance(parsed, dict) and "description" in parsed:
            return str(parsed["description"])
    except Exception:
        pass
    return ""


def count_words(text: str) -> int:
    """Count words using whitespace splitting."""
    return len(text.split())


def find_skills(root: Path) -> list[dict]:
    """Find all skills by scanning for SKILL.md files."""
    skills = []
    search_dirs = [
        root / ".claude" / "skills",
        root / "skills",
    ]
    plugin_json = root / ".claude-plugin" / "plugin.json"
    if plugin_json.exists():
        try:
            plugin = json.loads(_read_text_safe(plugin_json))
            for path in plugin.get("skills", []):
                resolved = (root / path).resolve()
                if resolved.is_relative_to(root):
                    search_dirs.append(resolved)
        except (json.JSONDecodeError, KeyError, AttributeError):
            pass

    seen = set()
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for skill_md in search_dir.rglob("SKILL.md"):
            skill_dir = skill_md.parent
            if skill_dir in seen:
                continue
            seen.add(skill_dir)
            content = _read_text_safe(skill_md)
            if not content:
                continue
            words = count_words(content)
            name = skill_dir.name
            description = _parse_frontmatter_description(content)
            skills.append({
                "name": name,
                "path": str(skill_md.relative_to(root)),
                "words": words,
                "description": description,
            })
    return sorted(skills, key=lambda s: s["words"], reverse=True)


def find_commands(root: Path) -> list[dict]:
    """Find command definitions."""
    commands = []
    search_dirs = [
        root / ".claude" / "commands",
        root / "commands",
    ]
    seen = set()
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for md_file in search_dir.rglob("*.md"):
            name = md_file.stem
            if name in seen:
                continue
            seen.add(name)
            content = _read_text_safe(md_file)
            words = count_words(content) if content else 0
            commands.append({"name": name, "path": str(md_file.relative_to(root)), "words": words})
    return commands


def find_claude_md(root: Path) -> dict | None:
    """Find the project CLAUDE.md."""
    for candidate in [root / "CLAUDE.md", root / ".claude" / "CLAUDE.md"]:
        if candidate.exists():
            content = _read_text_safe(candidate)
            if not content:
                continue
            return {
                "path": str(candidate.relative_to(root)),
                "words": count_words(content),
                "lines": len(content.splitlines()),
            }
    return None


def find_hooks(root: Path) -> list[dict]:
    """Find hooks from settings.json and plugin.json."""
    hooks = []
    sources = [
        root / ".claude" / "settings.json",
        root / ".claude-plugin" / "plugin.json",
    ]
    for source_path in sources:
        if not source_path.exists():
            continue
        content = _read_text_safe(source_path)
        if not content:
            continue
        try:
            data = json.loads(content)
            hooks_map = data.get("hooks", {}) if isinstance(data, dict) else {}
            for hook_type, matchers in hooks_map.items():
                if not isinstance(matchers, list):
                    continue
                for matcher in matchers:
                    if not isinstance(matcher, dict):
                        continue
                    inner_hooks = matcher.get("hooks", [])
                    if isinstance(inner_hooks, list):
                        for hook in inner_hooks:
                            if not isinstance(hook, dict):
                                continue
                            hooks.append({
                                "type": hook_type,
                                "matcher": matcher.get("matcher", ""),
                                "command": hook.get("command", "")[:60],
                            })
        except (json.JSONDecodeError, KeyError, AttributeError):
            pass
    return hooks


def check_structural_issues(skills: list[dict], claude_md: dict | None) -> list[str]:
    """Flag obvious structural issues."""
    warnings = []
    if not claude_md:
        warnings.append("No CLAUDE.md found. Consider adding one for project-level instructions.")
    for skill in skills:
        if not skill["description"]:
            warnings.append(f"Skill '{skill['name']}' has no description in frontmatter. This hurts trigger precision.")
    return warnings


def main():
    parser = argparse.ArgumentParser(description="Harness inventory scanner")
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--format", choices=["text", "yaml"], default="text")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    skills = find_skills(root)
    commands = find_commands(root)
    claude_md = find_claude_md(root)
    hooks = find_hooks(root)
    warnings = check_structural_issues(skills, claude_md)

    total_skill_words = sum(s["words"] for s in skills)
    total_command_words = sum(c["words"] for c in commands)
    claude_md_words = claude_md["words"] if claude_md else 0
    total_words = total_skill_words + total_command_words + claude_md_words

    if args.format == "yaml":
        try:
            import yaml
        except ImportError:
            print("Error: PyYAML is required for --format yaml. Install with: pip install pyyaml", file=sys.stderr)
            return 1
        output = {
            "summary": {
                "skills": len(skills),
                "commands": len(commands),
                "hooks": len(hooks),
                "claude_md": bool(claude_md),
                "total_word_count": total_words,
            },
            "skills": skills,
            "commands": commands,
            "claude_md": claude_md,
            "hooks": hooks,
            "warnings": warnings,
        }
        print(yaml.dump(output, default_flow_style=False, sort_keys=False))
    else:
        print("=== Harness Inventory ===\n")
        print(f"Skills:     {len(skills)}")
        print(f"Commands:   {len(commands)}")
        print(f"Hooks:      {len(hooks)}")
        print(f"CLAUDE.md:  {'Yes' if claude_md else 'No'}")
        print(f"Total words (approx): {total_words}")
        if skills:
            print("\nSkills by word count:")
            for s in skills[:10]:
                print(f"  {s['name']:30s} {s['words']:>5d} words")
        if warnings:
            print(f"\nWarnings ({len(warnings)}):")
            for w in warnings:
                print(f"  - {w}")
        if not skills:
            print("\nNo skills found.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
