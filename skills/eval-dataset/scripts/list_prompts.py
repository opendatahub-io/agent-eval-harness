#!/usr/bin/env python3
"""List available builtin generation prompts from the harness registry."""

import agent_eval._bootstrap  # noqa: F401

from agent_eval.prompts import BuiltinPromptRegistry


def main():
    registry = BuiltinPromptRegistry()
    registry.discover()
    for name in registry.list_names():
        entry = registry.get(name)
        # Prefer the **Purpose** line as a short description
        description = ""
        for line in entry.path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("**Purpose**"):
                description = stripped.split(":", 1)[-1].strip().rstrip("*").strip()[:70]
                break
        print(f"  {name:<28} {description}")


if __name__ == "__main__":
    main()
