#!/usr/bin/env python3
"""State persistence — survives context compression.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/state.py init <path> key=value ...
    python3 ${CLAUDE_SKILL_DIR}/scripts/state.py set <path> key=value ...
    python3 ${CLAUDE_SKILL_DIR}/scripts/state.py read <path>
    python3 ${CLAUDE_SKILL_DIR}/scripts/state.py write-ids <path> ID ...
    python3 ${CLAUDE_SKILL_DIR}/scripts/state.py read-ids <path>
    python3 ${CLAUDE_SKILL_DIR}/scripts/state.py clean
"""

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Invoked both as the ``agent_eval.state`` module and directly as
# ``python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py`` (eval-analyze,
# eval-review, eval-optimize). In the direct case sys.path[0] is *inside* the
# package, so ``import agent_eval`` cannot resolve until the plugin root is on
# the path. resolve() follows the per-skill scripts/agent_eval symlink.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_eval._bootstrap  # noqa: F401,E402 — auto-activate venv before 3p imports

import yaml  # noqa: E402


def _parse_value(v):
    if v.lower() == "true":
        return True
    elif v.lower() == "false":
        return False
    elif v.lower() in ("null", "none"):
        return None
    return v


def _parse_kwargs(args):
    kwargs = {}
    for arg in args:
        if "=" in arg:
            k, v = arg.split("=", 1)
            kwargs[k] = _parse_value(v)
    return kwargs


def main():
    if len(sys.argv) < 2:
        print("Usage: state.py <init|set|read|write-ids|read-ids|clean> [args]",
              file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd in ("init", "set", "read", "write-ids", "read-ids") and len(sys.argv) < 3:
        print(f"Usage: state.py {cmd} <path> [args]", file=sys.stderr)
        sys.exit(1)

    if cmd == "init":
        path = Path(sys.argv[2])
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _parse_kwargs(sys.argv[3:])
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)

    elif cmd == "set":
        path = Path(sys.argv[2])
        data = {}
        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        data.update(_parse_kwargs(sys.argv[3:]))
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)

    elif cmd == "read":
        path = sys.argv[2]
        if not Path(path).exists():
            print("{}")
            return
        with open(path) as f:
            if path.endswith(".json"):
                data = json.load(f)
                print(json.dumps(data, indent=2))
            else:
                print(f.read())

    elif cmd == "write-ids":
        path = Path(sys.argv[2])
        path.parent.mkdir(parents=True, exist_ok=True)
        ids = list(dict.fromkeys(sys.argv[3:]))  # dedup preserving order
        path.write_text("\n".join(ids) + "\n" if ids else "")

    elif cmd == "read-ids":
        path = Path(sys.argv[2])
        if path.exists():
            ids = [l.strip() for l in path.read_text().splitlines() if l.strip()]
            print(" ".join(ids))

    elif cmd == "clean":
        shutil.rmtree("tmp", ignore_errors=True)
        print("Cleaned tmp/")

    elif cmd == "timestamp":
        print(datetime.now(timezone.utc).isoformat())

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
