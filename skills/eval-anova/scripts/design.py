"""Interactive experiment design — matrix generation and cost estimation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_eval.matrix import MatrixBuilder, MatrixConfig


def design_experiment(
    config_path: Path,
    *,
    avg_cost_per_run: float = 0.0,
    n_cases: int = 1,
) -> dict[str, Any]:
    """Load matrix config, expand conditions, and estimate cost."""
    config = MatrixBuilder.from_yaml(config_path, strict=True)
    if config is None:
        raise ValueError(f"No matrix configuration found in {config_path}")

    conditions = MatrixBuilder.expand_full_factorial(config.factors)
    experiment_id = MatrixBuilder.generate_experiment_id(config.factors)
    cost = MatrixBuilder.estimate_cost(
        n_conditions=len(conditions),
        n_cases=n_cases,
        replications=config.replications,
        avg_cost_per_run=avg_cost_per_run,
    )

    return {
        "experiment_id": experiment_id,
        "config": config,
        "conditions": conditions,
        "cost_estimate": cost,
    }


def print_design_summary(design: dict[str, Any]) -> str:
    """Format a human-readable design summary."""
    config = design["config"]
    cost = design["cost_estimate"]

    lines = [
        f"Experiment: {design['experiment_id']}",
        f"Factors: {', '.join(config.factors.keys())}",
        f"Conditions: {len(design['conditions'])}",
        f"Replications: {config.replications}",
        f"Total runs: {cost['total_runs']}",
        f"Estimated cost: ${cost['estimated_cost']:.2f}",
        "",
        "Conditions:",
    ]

    for c in design["conditions"]:
        levels_str = ", ".join(f"{k}={v}" for k, v in sorted(c.levels.items()))
        lines.append(f"  [{c.condition_id[:8]}] {levels_str}")

    return "\n".join(lines)
