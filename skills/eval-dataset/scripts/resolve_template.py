#!/usr/bin/env python3
"""Resolve test case template references to file paths.

Supports:
- builtin:name → ${CLAUDE_SKILL_DIR}/templates/builtin/{name}.md
- path/to/template.md → absolute or relative file path
"""

import os
import sys
from pathlib import Path


def resolve_template(template_ref: str) -> Path:
    """Resolve template reference to file path.

    Args:
        template_ref: Either "builtin:name" or a file path

    Returns:
        Absolute path to template file

    Raises:
        FileNotFoundError: If template file doesn't exist
        ValueError: If template_ref is invalid
    """
    if not template_ref:
        raise ValueError("template_ref cannot be empty")

    if template_ref.startswith("builtin:"):
        # Builtin template: builtin:navigation → navigation.md
        name = template_ref[8:]  # Strip "builtin:"
        if not name:
            raise ValueError("builtin: prefix requires a name (e.g., builtin:navigation)")

        # Resolve to eval-dataset templates directory
        # Try CLAUDE_SKILL_DIR env var first, fall back to script location
        skill_dir_str = os.environ.get("CLAUDE_SKILL_DIR", "")
        if skill_dir_str:
            skill_dir = Path(skill_dir_str)
        else:
            # Determine skill dir from this script's location
            # Script is at: skills/eval-dataset/scripts/resolve_template.py
            # Skill dir is: skills/eval-dataset/
            script_path = Path(__file__).resolve()
            skill_dir = script_path.parent.parent

        builtin_dir = skill_dir / "templates/builtin"
        template_path = builtin_dir / f"{name}.md"

        if not template_path.exists():
            # List available templates if directory exists
            available_names = []
            if builtin_dir.exists():
                available = list(builtin_dir.glob("*.md"))
                available_names = [p.stem for p in available if p.name != "README.md"]

            error_msg = f"Builtin template '{name}' not found at {template_path}."
            if available_names:
                error_msg += f"\nAvailable builtin templates: {', '.join(available_names)}"
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


def list_builtin_templates() -> list[str]:
    """List all available builtin templates.

    Returns:
        List of template names (without .md extension)
    """
    # Try CLAUDE_SKILL_DIR env var first, fall back to script location
    skill_dir_str = os.environ.get("CLAUDE_SKILL_DIR", "")
    if skill_dir_str:
        skill_dir = Path(skill_dir_str)
    else:
        # Determine skill dir from this script's location
        script_path = Path(__file__).resolve()
        skill_dir = script_path.parent.parent

    builtin_dir = skill_dir / "templates/builtin"
    if not builtin_dir.exists():
        return []

    templates = []
    for template_file in builtin_dir.glob("*.md"):
        if template_file.name != "README.md":
            templates.append(template_file.stem)

    return sorted(templates)


def main():
    """CLI for testing template resolution."""
    if len(sys.argv) < 2:
        print("Usage:", file=sys.stderr)
        print("  resolve_template.py <template-ref>", file=sys.stderr)
        print("  resolve_template.py --list", file=sys.stderr)
        print("", file=sys.stderr)
        print("Examples:", file=sys.stderr)
        print("  resolve_template.py builtin:navigation", file=sys.stderr)
        print("  resolve_template.py eval/templates/my-template.md", file=sys.stderr)
        print("  resolve_template.py --list", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--list":
        try:
            templates = list_builtin_templates()
            if templates:
                print("Available builtin templates:")
                for name in templates:
                    print(f"  builtin:{name}")
            else:
                print("No builtin templates found")
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
