"""Pareto frontier identification for cost/quality trade-off analysis."""

from __future__ import annotations

from typing import Any


def pareto_frontier(
    conditions: list[dict[str, Any]],
    cost_key: str,
    quality_key: str,
) -> list[dict[str, Any]]:
    """Identify the Pareto-optimal conditions (minimize cost, maximize quality).

    A condition is dominated if another condition has both lower cost AND
    higher quality. Non-dominated conditions form the Pareto frontier.
    """
    if not conditions:
        return []

    frontier = []
    for candidate in conditions:
        dominated = False
        for other in conditions:
            if other is candidate:
                continue
            if (
                other[cost_key] <= candidate[cost_key]
                and other[quality_key] >= candidate[quality_key]
                and (
                    other[cost_key] < candidate[cost_key]
                    or other[quality_key] > candidate[quality_key]
                )
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(candidate)

    return frontier
