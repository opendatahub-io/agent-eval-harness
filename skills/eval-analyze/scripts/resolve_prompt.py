#!/usr/bin/env python3
"""Resolve analysis prompt references to file paths.

Supports:
- path/to/prompt.md → absolute or relative file path
"""

import os
import sys
from pathlib import Path


def resolve_analysis_prompt(prompt_ref: str) -> Path:
    """Resolve prompt reference to file path.

    Args:
        prompt_ref: File path (absolute or relative)

    Returns:
        Absolute path to prompt file

    Raises:
        FileNotFoundError: If prompt file doesn't exist
        ValueError: If prompt_ref is invalid
    """
    if not prompt_ref:
        raise ValueError("prompt_ref cannot be empty")

    # Custom prompt path (absolute or relative to project root or plugin dir)
    path = Path(prompt_ref)

    # If absolute, use directly
    if path.is_absolute():
        if not path.exists():
            raise FileNotFoundError(
                f"Prompt file not found: {path}")
        return path.resolve()

    # If relative, search in order:
    # 1. Current working directory (project repo)
    # 2. Plugin installation directory

    search_paths = []

    # Search in project repo
    project_path = Path.cwd() / path
    search_paths.append(project_path)
    if project_path.exists():
        return project_path.resolve()

    # Search in plugin directory
    # Try CLAUDE_PLUGIN_DIR env var first, fall back to script location
    plugin_dir_str = os.environ.get("CLAUDE_PLUGIN_DIR", "")
    if plugin_dir_str:
        plugin_dir = Path(plugin_dir_str)
    else:
        # Determine plugin dir from this script's location
        # Script is at: skills/eval-analyze/scripts/resolve_prompt.py
        # Plugin root is: ../../.. (up 3 levels)
        script_path = Path(__file__).resolve()
        plugin_dir = script_path.parent.parent.parent.parent

    plugin_path = plugin_dir / path
    search_paths.append(plugin_path)
    if plugin_path.exists():
        return plugin_path.resolve()

    # Not found in any location
    raise FileNotFoundError(
        f"Prompt file not found: {prompt_ref}\n"
        f"Looked in:\n" +
        "\n".join(f"  - {p.absolute()}" for p in search_paths))


def main():
    """CLI for testing prompt resolution."""
    if len(sys.argv) != 2:
        print("Usage: resolve_prompt.py <prompt-ref>", file=sys.stderr)
        print("", file=sys.stderr)
        print("Examples:", file=sys.stderr)
        print("  resolve_prompt.py examples/openshift-agentic-docs.md", file=sys.stderr)
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
