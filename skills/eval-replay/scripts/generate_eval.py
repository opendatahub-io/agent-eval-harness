#!/usr/bin/env python3
"""Generate a complete eval.yaml for PR replay evaluation.

Produces a harness-compliant config with an LLM alignment judge as the
primary evaluator (compares skill output against accepted diff + reviewer
comments) and lightweight deterministic checks as sanity guards.

Usage:
    python3 generate_eval.py --skill code-review --strategy review \
        --dataset-path /path/to/cases --output /path/to/eval.yaml
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Prompt file paths (resolved absolute via SKILL_DIR)
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent

STRATEGY_PROMPT_FILES: dict[str, str] = {
    "review": str(SKILL_DIR / "prompts" / "review-alignment.md"),
    "fix": str(SKILL_DIR / "prompts" / "fix-alignment.md"),
    "scan": str(SKILL_DIR / "prompts" / "scan-alignment.md"),
}

STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "review": (
        "Evaluates whether the skill's review aligns with the accepted "
        "PR outcome: did its feedback point toward what was actually merged?"
    ),
    "fix": (
        "Evaluates whether the skill's fix aligns with the accepted patch: "
        "would it have resolved the same issue?"
    ),
    "scan": (
        "Evaluates whether the skill's scan identified the vulnerability "
        "that the accepted PR fixes."
    ),
}

# ---------------------------------------------------------------------------
# Deterministic sanity-check judges (strategy-independent)
# ---------------------------------------------------------------------------

SANITY_JUDGES: list[dict[str, str]] = [
    {
        "name": "has_output",
        "description": "Skill produced non-empty output.",
        "check": textwrap.dedent("""\
            conversation = outputs.get("conversation", "")
            if not conversation.strip():
                return False, "Output is empty"
            return True, f"Output has {len(conversation.strip())} chars"
        """),
    },
    {
        "name": "references_diff_files",
        "description": "Skill referenced at least one file from the PR diff.",
        "check": textwrap.dedent("""\
            ann = outputs.get("annotations", {})
            expected = ann.get("expected_files", []) or []
            changed = outputs.get("annotations", {}).get("changed_files", expected)
            if not changed:
                return True, "No files to check"
            conversation = outputs.get("conversation", "")
            found = [f for f in changed if f in conversation]
            if found:
                return True, f"Referenced {len(found)}/{len(changed)} diff files"
            return False, "Skill output does not reference any files from the diff"
        """),
    },
]

_DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "outcome_alignment": {"min_mean": 3.0},
    "has_output": {"min_pass_rate": 1.0},
    "references_diff_files": {"min_pass_rate": 0.5},
}

# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------


def generate_config(
    skill: str,
    strategy: str,
    dataset_path: str,
) -> dict[str, Any]:
    """Build a complete eval.yaml dict."""
    prompt_file = STRATEGY_PROMPT_FILES.get(strategy)
    if prompt_file is None:
        raise ValueError(f"Unknown strategy: {strategy}")

    alignment_judge = {
        "name": "outcome_alignment",
        "description": STRATEGY_DESCRIPTIONS[strategy],
        "prompt_file": prompt_file,
        "feedback_type": "int",
    }

    return {
        "name": f"{skill}-pr-replay",
        "description": f"Replay historical PRs against {skill} (strategy: {strategy})",
        "skill": skill,
        "execution": {
            "mode": "case",
            "arguments": "{prompt}",
        },
        "dataset": {
            "path": dataset_path,
            "schema": (
                "Each case (pr-<N>/) contains input.yaml with PR context "
                "and annotations.yaml with reviewer ground truth."
            ),
        },
        "outputs": [{"path": "review_output"}],
        "traces": {
            "events": True,
            "stdout": True,
            "stderr": True,
            "metrics": True,
        },
        "judges": [alignment_judge, *SANITY_JUDGES],
        "thresholds": _DEFAULT_THRESHOLDS,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--skill", required=True, help="Name of the skill under test")
    parser.add_argument(
        "--strategy", choices=["review", "fix", "scan"], default="review"
    )
    parser.add_argument(
        "--dataset-path",
        required=True,
        help="Path to cases directory (resolved to absolute)",
    )
    parser.add_argument(
        "--output", required=True, help="Output path for generated eval.yaml"
    )

    args = parser.parse_args()
    output_path = Path(args.output).resolve()
    dataset_path = str(Path(args.dataset_path).resolve())

    config = generate_config(args.skill, args.strategy, dataset_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"Generated {output_path} (skill={args.skill}, strategy={args.strategy})")


if __name__ == "__main__":
    main()
