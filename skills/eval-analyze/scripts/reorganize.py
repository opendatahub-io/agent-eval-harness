#!/usr/bin/env python3
"""CLI wrapper for reorganize_root_config().

Moves root-level eval.yaml into eval/<eval_name>/ and updates
internal path references.

Usage:
    python3 scripts/reorganize.py --eval-name <name> [--project-root <path>]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from agent_eval.reorganize import reorganize_root_config


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--eval-name", required=True,
                        help="Eval name (from skill field)")
    parser.add_argument("--project-root", default=".",
                        help="Project root directory")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()

    try:
        result = reorganize_root_config(project_root, args.eval_name)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    output = {
        "moved": [{"from": m[0], "to": m[1]} for m in result.moved],
        "warnings": result.warnings,
        "target_config": str(result.target_config),
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
