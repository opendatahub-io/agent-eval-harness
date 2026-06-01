#!/usr/bin/env python3
"""Discover eval configs and print results as JSON.

Shared helper used by all eval skill SKILL.md files when --config
is not provided. Scans the project for eval.yaml files and prints
discovery results for the LLM to parse and act on.

Usage:
    python3 scripts/discover.py [--project-root <path>]

Output (JSON):
    {
        "configs": [{"path": "...", "eval_name": "...", "is_root": true}],
        "layout": "root|nested|flat|mixed|none",
        "count": 1
    }
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import discover_configs, infer_layout


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-root", default=".",
                        help="Project root directory (default: cwd)")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    configs = discover_configs(project_root)
    layout = infer_layout(configs)

    result = {
        "configs": [
            {
                "path": str(c.path),
                "eval_name": c.eval_name,
                "is_root": c.is_root,
            }
            for c in configs
        ],
        "layout": layout,
        "count": len(configs),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
