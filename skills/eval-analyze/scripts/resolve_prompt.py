#!/usr/bin/env python3
"""Resolve analysis prompt references to file paths.

Supports:
- builtin:name → ${CLAUDE_SKILL_DIR}/prompts/analyze-{name}.md
- path/to/prompt.md → absolute or relative file path
"""

import os
import sys
from pathlib import Path


def resolve_analysis_prompt(prompt_ref: str) -> Path:
    """Resolve prompt reference to file path.

    Args:
        prompt_ref: Either "builtin:name" or a file path

    Returns:
        Absolute path to prompt file

    Raises:
        FileNotFoundError: If prompt file doesn't exist
        ValueError: If prompt_ref is invalid
    """
    if not prompt_ref:
        raise ValueError("prompt_ref cannot be empty")

    if prompt_ref.startswith("builtin:"):
        # Builtin prompt: builtin:docs → analyze-docs.md
        name = prompt_ref[8:]  # Strip "builtin:"
        if not name:
            raise ValueError("builtin: prefix requires a name (e.g., builtin:docs)")

        # Resolve to eval-analyze prompts directory
        # Try CLAUDE_SKILL_DIR env var first, fall back to script location
        skill_dir_str = os.environ.get("CLAUDE_SKILL_DIR", "")
        if skill_dir_str:
            skill_dir = Path(skill_dir_str)
        else:
            # Determine skill dir from this script's location
            # Script is at: skills/eval-analyze/scripts/resolve_prompt.py
            # Skill dir is: skills/eval-analyze/
            script_path = Path(__file__).resolve()
            skill_dir = script_path.parent.parent

        prompt_path = skill_dir / "prompts" / f"analyze-{name}.md"
        if not prompt_path.exists():
            available = list((skill_dir / "prompts").glob("analyze-*.md"))
            available_names = [p.stem.replace("analyze-", "") for p in available]
            raise FileNotFoundError(
                f"Builtin prompt '{name}' not found at {prompt_path}.\n"
                f"Available builtin prompts: {', '.join(available_names)}")

        return prompt_path.resolve()

    # Custom prompt path (absolute or relative to project root)
    path = Path(prompt_ref)
    if not path.is_absolute():
        path = Path.cwd() / path

    if not path.exists():
        raise FileNotFoundError(
            f"Prompt file not found: {path}\n"
            f"Looked in: {path.absolute()}")

    return path.resolve()


def main():
    """CLI for testing prompt resolution."""
    if len(sys.argv) != 2:
        print("Usage: resolve_prompt.py <prompt-ref>", file=sys.stderr)
        print("", file=sys.stderr)
        print("Examples:", file=sys.stderr)
        print("  resolve_prompt.py builtin:docs", file=sys.stderr)
        print("  resolve_prompt.py eval/prompts/my-prompt.md", file=sys.stderr)
        sys.exit(1)

    try:
        prompt_ref = sys.argv[1]
        resolved = resolve_analysis_prompt(prompt_ref)
        print(resolved)
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
