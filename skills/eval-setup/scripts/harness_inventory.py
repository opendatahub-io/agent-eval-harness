#!/usr/bin/env python3
"""Scan a Claude Code project for configuration artifacts and report a harness inventory."""

import argparse
import json
import sys
from pathlib import Path

MAX_FILE_SIZE = 1_000_000  # 1MB limit per file read


def _read_text_safe(path: Path) -> str:
    """Read file text with size limit to avoid memory issues on corrupted projects."""
    if path.stat().st_size > MAX_FILE_SIZE:
        return ""
    return path.read_text()


def count_tokens_approx(text: str) -> int:
    """Approximate token count using whitespace splitting (rough but fast)."""
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
            plugin = json.loads(plugin_json.read_text())
            for path in plugin.get("skills", []):
                search_dirs.append(root / path)
        except (json.JSONDecodeError, KeyError):
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
            tokens = count_tokens_approx(content)
            name = skill_dir.name
            description = ""
            for line in content.splitlines():
                if line.startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip("'\"")[:80]
                    break
            skills.append({
                "name": name,
                "path": str(skill_md.relative_to(root)),
                "tokens": tokens,
                "description": description,
            })
    return sorted(skills, key=lambda s: s["tokens"], reverse=True)


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
            tokens = count_tokens_approx(_read_text_safe(md_file))
            commands.append({"name": name, "path": str(md_file.relative_to(root)), "tokens": tokens})
    return commands


def find_claude_md(root: Path) -> dict | None:
    """Find the project CLAUDE.md."""
    for candidate in [root / "CLAUDE.md", root / ".claude" / "CLAUDE.md"]:
        if candidate.exists():
            content = _read_text_safe(candidate)
            return {
                "path": str(candidate.relative_to(root)),
                "tokens": count_tokens_approx(content),
                "lines": len(content.splitlines()),
            }
    return None


def find_hooks(root: Path) -> list[dict]:
    """Find hooks from settings.json."""
    hooks = []
    settings_path = root / ".claude" / "settings.json"
    if not settings_path.exists():
        return hooks
    try:
        settings = json.loads(_read_text_safe(settings_path))
        for hook_type, hook_list in settings.get("hooks", {}).items():
            if isinstance(hook_list, list):
                for hook in hook_list:
                    hooks.append({
                        "type": hook_type,
                        "matcher": hook.get("matcher", ""),
                        "command": hook.get("command", "")[:60],
                    })
    except (json.JSONDecodeError, KeyError):
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

    total_skill_tokens = sum(s["tokens"] for s in skills)
    total_command_tokens = sum(c["tokens"] for c in commands)
    claude_md_tokens = claude_md["tokens"] if claude_md else 0
    total_tokens = total_skill_tokens + total_command_tokens + claude_md_tokens

    if args.format == "yaml":
        try:
            import yaml
        except ImportError:
            print("Error: PyYAML is required for --format yaml. Install it with: pip install pyyaml", file=sys.stderr)
            return 1
        output = {
            "summary": {
                "skills": len(skills),
                "commands": len(commands),
                "hooks": len(hooks),
                "claude_md": bool(claude_md),
                "total_token_budget": total_tokens,
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
        print(f"Total tokens: {total_tokens}")
        if skills:
            print("\nTop skills by token count:")
            for s in skills[:5]:
                print(f"  {s['name']:30s} {s['tokens']:>5d} tokens")
        if warnings:
            print(f"\nWarnings ({len(warnings)}):")
            for w in warnings:
                print(f"  - {w}")
        if not skills:
            print("\nNo skills found. Single-skill or no-skill configuration.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
