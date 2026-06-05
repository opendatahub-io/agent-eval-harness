#!/usr/bin/env python3
"""List available builtin test case generation templates.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/list_templates.py
"""

import agent_eval._bootstrap  # noqa: F401

from pathlib import Path


def main():
    # Templates are in ../templates/builtin/ relative to this script
    script_dir = Path(__file__).parent
    builtin_dir = script_dir.parent / "templates" / "builtin"

    if not builtin_dir.exists():
        print(f"ERROR: Builtin templates directory not found: {builtin_dir}")
        return

    # Find all .md files except README.md
    templates = sorted([
        f.stem for f in builtin_dir.glob("*.md")
        if f.name != "README.md"
    ])

    if not templates:
        print("No builtin templates found")
        return

    print("Available builtin templates:")
    for name in templates:
        template_path = builtin_dir / f"{name}.md"
        # Read first line after any frontmatter for description
        content = template_path.read_text()
        lines = content.split('\n')
        description = ""
        for line in lines:
            if line.strip() and not line.startswith('#') and not line.startswith('---'):
                description = line.strip()[:60]
                break
        print(f"  builtin:{name:<20} {description}")


if __name__ == "__main__":
    main()
