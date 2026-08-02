"""Hierarchical composite scoring with bool/int separation.

Critical invariant: Python bool is a subclass of int. Bools must NEVER enter
the numeric weighted-average path. All isinstance checks for numeric types
must explicitly exclude bool.
"""

from __future__ import annotations

import statistics
import logging
from typing import Any

logger = logging.getLogger(__name__)


def composite_score(
    judge_results: dict[str, Any],
    judge_configs: dict[str, dict[str, Any]],
) -> float:
    """Compute a composite score from judge results.

    Scoring pipeline:
    1. Classify each result as boolean or numeric (bool excluded from numeric)
    2. Check gate judges — if any gate bool is False, return 0.0
    3. Compute weighted average of numeric scores
    4. Apply non-gate boolean modifier (fraction of passing non-gate bools)
    """
    booleans: dict[str, bool] = {}
    numerics: dict[str, float] = {}

    for key, value in judge_results.items():
        config = judge_configs.get(key, {})
        declared_type = config.get("type")

        if value is None:
            logger.warning(
                "Dropped judge result %s=%r declared_type=%r",
                key,
                value,
                declared_type,
            )
            continue

        if declared_type == "boolean" and isinstance(value, bool):
            booleans[key] = bool(value)
        elif declared_type == "numeric" and isinstance(value, (int, float)) and not isinstance(value, bool):
            numerics[key] = float(value)
        elif declared_type is None and isinstance(value, bool):
            booleans[key] = bool(value)
        elif declared_type is None and isinstance(value, (int, float)) and not isinstance(value, bool):
            numerics[key] = float(value)
        else:
            logger.warning(
                "Dropped judge result %s=%r declared_type=%r",
                key,
                value,
                declared_type,
            )

    gates = {
        k: v
        for k, v in booleans.items()
        if judge_configs.get(k, {}).get("gate", False)
    }
    if gates and not all(gates.values()):
        return 0.0

    non_gate_bools = {k: v for k, v in booleans.items() if k not in gates}

    if numerics:
        weighted_sum = 0.0
        total_weight = 0.0
        for key, value in numerics.items():
            weight = judge_configs.get(key, {}).get("weight", 1.0)
            clamped = max(0.0, min(1.0, value))
            weighted_sum += clamped * weight
            total_weight += weight

        numeric_score = weighted_sum / total_weight if total_weight > 0 else 0.0
    else:
        numeric_score = 1.0

    if non_gate_bools:
        bool_rate = sum(1 for v in non_gate_bools.values() if v) / len(non_gate_bools)
        return numeric_score * bool_rate

    return numeric_score


def aggregate_replications(scores: list[float]) -> dict[str, float]:
    """Aggregate composite scores across replications."""
    n = len(scores)
    if n == 0:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0, "n": 0}
    mean = statistics.mean(scores)
    std = statistics.stdev(scores) if n > 1 else 0.0
    return {
        "mean": mean,
        "std": std,
        "min": min(scores),
        "max": max(scores),
        "n": n,
    }
