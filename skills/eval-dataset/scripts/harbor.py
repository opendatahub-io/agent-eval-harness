#!/usr/bin/env python3
"""CLI to generate Harbor task packages from an eval.yaml dataset.

Thin wrapper around :func:`agent_eval.harbor.tasks.generate_tasks` (the shared
library), exposed as the ``/eval-dataset`` entry point. ``/eval-run --runner
harbor`` consumes the packages this produces (it imports the same library).

Usage:
    python3 harbor.py --config eval.yaml --out harbor-tasks \\
        --image <registry>/<task-image>:latest \\
        --arguments '{prompt}' --skill <skill-name> \\
        [--cases case-001 case-002] [--workdir /workspace] [--judge-model <model>]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
from pathlib import Path

from agent_eval.config import EvalConfig
from agent_eval.harbor.tasks import generate_tasks


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="Path to eval.yaml")
    parser.add_argument("--out", required=True, help="Output dir for task packages")
    parser.add_argument("--image", required=True, help="Task container image ref")
    parser.add_argument("--arguments", default=None,
                        help="Per-case arguments template (overrides execution.arguments); "
                             "supports {field}/{field?} from input.yaml")
    parser.add_argument("--skill", default=None, help="Skill name override")
    parser.add_argument("--workdir", default="/workspace",
                        help="Container workdir where the agent writes artifacts")
    parser.add_argument("--cases", nargs="*", default=None, help="Subset of case ids")
    parser.add_argument("--verifier-timeout", type=float, default=300.0)
    parser.add_argument("--agent-timeout", type=float, default=1800.0)
    parser.add_argument("--judge-model", default=None,
                        help="Override models.judge in the bundled config (e.g. a "
                             "model published on the target Vertex/Bedrock deployment)")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = EvalConfig.from_yaml(config_path)
    print(f"Generating Harbor tasks from {config_path} (skill={args.skill or config.skill})")

    tasks = generate_tasks(
        config, config_path, Path(args.out).resolve(), args.image,
        arguments=args.arguments, skill=args.skill, workdir=args.workdir,
        cases=args.cases, verifier_timeout=args.verifier_timeout,
        agent_timeout=args.agent_timeout, judge_model=args.judge_model,
    )
    print(f"Generated {len(tasks)} task package(s) in {args.out}")


if __name__ == "__main__":
    main()
