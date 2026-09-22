"""Checks that execution cost stays within a configurable budget threshold.

Required fields: cost_usd
Optional fields: cost_source
Failure means: The execution cost exceeded the allowed budget.

Provenance-aware (spec 014): when the case carries no cost, or its
``cost_source`` is ``unavailable``, the judge abstains — it returns
``(None, rationale)`` so the case is skipped for this judge rather than failed
(an unknown cost is not an overspend). A cost the runner only *estimated*
(``cost_source`` ``runner-estimate`` / ``runner:*``) is still judged, with the
rationale saying so.
"""


def _is_estimate(source):
    return isinstance(source, str) and (
        "estimate" in source or source.startswith("runner"))


def judge(outputs, **kwargs):
    cost = outputs.get("cost_usd")
    source = outputs.get("cost_source")
    if cost is None or source == "unavailable":
        label = source or "missing"
        return (None, f"cost unavailable (cost_source: {label}) — abstained, not failed")

    max_cost = kwargs.get("max_cost_usd", 1.0)
    note = f" [{source}: an estimate, not billed spend]" if _is_estimate(source) else ""
    if cost <= max_cost:
        return (True, f"Cost ${cost:.2f} within budget ${max_cost:.2f}{note}")
    return (False, f"Cost ${cost:.2f} exceeds budget ${max_cost:.2f}{note}")
