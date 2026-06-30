#!/usr/bin/env python3
"""List available test case generation templates by category.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/list_templates.py [category]

Examples:
    python3 ${CLAUDE_SKILL_DIR}/scripts/list_templates.py              # List all
    python3 ${CLAUDE_SKILL_DIR}/scripts/list_templates.py documentation # List documentation templates
"""

import sys
import agent_eval._bootstrap  # noqa: F401

from pathlib import Path


def main():
    # Templates are in ../templates/ relative to this script
    script_dir = Path(__file__).parent
    templates_dir = script_dir.parent / "templates"

    if not templates_dir.exists():
        print(f"ERROR: Templates directory not found: {templates_dir}")
        return

    # Optional: filter by category
    category_filter = sys.argv[1] if len(sys.argv) > 1 else None

    # Find all template categories (subdirectories)
    categories = sorted([
        d.name for d in templates_dir.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    ])

    if category_filter:
        if category_filter not in categories:
            print(f"ERROR: Category '{category_filter}' not found")
            print(f"Available categories: {', '.join(categories)}")
            return
        categories = [category_filter]

    if not categories:
        print("No template categories found")
        return

    print("Available templates:")
    for category in categories:
        category_dir = templates_dir / category

        # Find all .md files except README.md
        templates = sorted([
            f.stem for f in category_dir.glob("*.md")
            if f.name != "README.md"
        ])

        if not templates:
            continue

        for name in templates:
            template_path = category_dir / f"{name}.md"

            # Read first non-header line for description
            content = template_path.read_text()
            lines = content.split('\n')
            description = ""
            for line in lines:
                if line.strip() and not line.startswith('#') and not line.startswith('---'):
                    description = line.strip()[:60]
                    break

            template_ref = f"{category}/{name}"
            print(f"  {template_ref:<30} {description}")


if __name__ == "__main__":
    main()
