"""Checks that execution cost stays within a configurable budget threshold.

Required fields: cost_usd
Failure means: The execution cost exceeded the allowed budget.
"""


def judge(outputs, **kwargs):
    cost = outputs.get("cost_usd")
    if cost is None:
        return (False, "No cost data available")

    max_cost = kwargs.get("max_cost_usd", 1.0)
    if cost <= max_cost:
        return (True, f"Cost ${cost:.2f} within budget ${max_cost:.2f}")
    return (False, f"Cost ${cost:.2f} exceeds budget ${max_cost:.2f}")
