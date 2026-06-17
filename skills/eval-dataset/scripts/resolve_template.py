#!/usr/bin/env python3
"""Resolve test case template references to file paths.

Supports:
- category/name → ${CLAUDE_SKILL_DIR}/templates/category/name.md (e.g., documentation/navigation)
- path/to/template.md → absolute or relative file path
"""

import os
import sys
from pathlib import Path


def resolve_template(template_ref: str) -> Path:
    """Resolve template reference to file path.

    Args:
        template_ref: Either "category/name" or a file path

    Returns:
        Absolute path to template file

    Raises:
        FileNotFoundError: If template file doesn't exist
        ValueError: If template_ref is invalid
    """
    if not template_ref:
        raise ValueError("template_ref cannot be empty")

    # Get skill directory (templates base)
    skill_dir_str = os.environ.get("CLAUDE_SKILL_DIR", "")
    if skill_dir_str:
        skill_dir = Path(skill_dir_str)
    else:
        # Determine skill dir from this script's location
        # Script is at: skills/eval-dataset/scripts/resolve_template.py
        # Skill dir is: skills/eval-dataset/
        script_path = Path(__file__).resolve()
        skill_dir = script_path.parent.parent

    templates_dir = skill_dir / "templates"

    # Handle category/name format (e.g., documentation/navigation)
    if "/" in template_ref and not template_ref.startswith("/") and ":" not in template_ref:
        # Assume it's a category/name reference
        template_path = templates_dir / f"{template_ref}.md"

        if not template_path.exists():
            # List available templates in this category
            category = template_ref.split("/")[0]
            category_dir = templates_dir / category
            available_names = []
            if category_dir.exists():
                available = list(category_dir.glob("*.md"))
                available_names = [p.stem for p in available if p.name != "README.md"]

            error_msg = f"Template '{template_ref}' not found at {template_path}."
            if available_names:
                error_msg += f"\nAvailable templates in {category}/: {', '.join(available_names)}"
            else:
                error_msg += f"\nCategory directory {category}/ does not exist or is empty."
            raise FileNotFoundError(error_msg)

        return template_path.resolve()

    # Custom template path (absolute or relative to project root)
    path = Path(template_ref)
    if not path.is_absolute():
        path = Path.cwd() / path

    if not path.exists():
        raise FileNotFoundError(
            f"Template file not found: {path}\n"
            f"Looked in: {path.absolute()}")

    return path.resolve()


def list_templates(category: str = None) -> dict[str, list[str]]:
    """List all available templates by category.

    Args:
        category: Optional category name to filter (e.g., "documentation")

    Returns:
        Dict mapping category names to lists of template names (without .md extension)
    """
    # Try CLAUDE_SKILL_DIR env var first, fall back to script location
    skill_dir_str = os.environ.get("CLAUDE_SKILL_DIR", "")
    if skill_dir_str:
        skill_dir = Path(skill_dir_str)
    else:
        # Determine skill dir from this script's location
        script_path = Path(__file__).resolve()
        skill_dir = script_path.parent.parent

    templates_dir = skill_dir / "templates"
    if not templates_dir.exists():
        return {}

    result = {}

    # If specific category requested, only process that one
    categories_to_process = [category] if category else [
        d.name for d in templates_dir.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    ]

    for cat in categories_to_process:
        cat_dir = templates_dir / cat
        if not cat_dir.exists():
            continue

        templates = []
        for template_file in cat_dir.glob("*.md"):
            if template_file.name != "README.md":
                templates.append(template_file.stem)

        if templates:
            result[cat] = sorted(templates)

    return result


def main():
    """CLI for testing template resolution."""
    if len(sys.argv) < 2:
        print("Usage:", file=sys.stderr)
        print("  resolve_template.py <template-ref>", file=sys.stderr)
        print("  resolve_template.py --list [category]", file=sys.stderr)
        print("", file=sys.stderr)
        print("Examples:", file=sys.stderr)
        print("  resolve_template.py documentation/navigation", file=sys.stderr)
        print("  resolve_template.py eval/templates/my-template.md", file=sys.stderr)
        print("  resolve_template.py --list", file=sys.stderr)
        print("  resolve_template.py --list documentation", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--list":
        try:
            category = sys.argv[2] if len(sys.argv) > 2 else None
            templates_by_cat = list_templates(category)

            if templates_by_cat:
                print("Available templates:")
                for cat, templates in sorted(templates_by_cat.items()):
                    for name in templates:
                        print(f"  {cat}/{name}")
            else:
                if category:
                    print(f"No templates found in category '{category}'")
                else:
                    print("No templates found")
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            template_ref = sys.argv[1]
            resolved = resolve_template(template_ref)
            print(resolved)
        except (ValueError, FileNotFoundError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
