#!/usr/bin/env python3
"""List available builtin judges from the harness registry."""

import agent_eval._bootstrap  # noqa: F401

from agent_eval.judges import BuiltinJudgeRegistry


def main():
    registry = BuiltinJudgeRegistry()
    registry.discover()
    for name in registry.list_names():
        entry = registry.get(name)
        print(f"  {name} ({entry.category}/{entry.kind})")


if __name__ == "__main__":
    main()
