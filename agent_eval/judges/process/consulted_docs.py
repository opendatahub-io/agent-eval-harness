"""Checks the agent read the documentation files listed in expected_files.

Required fields: events, annotations.expected_files
Failure means: The agent did not read enough of the expected documentation.

Arguments:
    min_coverage (float): fraction of expected_files that must be read (default 0.8)
    match (str): path match strategy — "suffix" (default), "exact", or "basename"
"""

from agent_eval.events import extract_read_calls


def _normalize(path, match):
    path = (path or "").replace("\\", "/").strip()
    if match == "basename":
        return path.rsplit("/", 1)[-1]
    return path


def judge(outputs, **kwargs):
    min_coverage = kwargs.get("min_coverage", 0.8)
    match = kwargs.get("match", "suffix")

    expected = [_normalize(p, match)
                for p in outputs.get("annotations", {}).get("expected_files", [])]
    if not expected:
        return (True, "No expected_files specified — nothing to verify")

    # Derive read_calls from events on demand (same pattern as conversation/tool_calls)
    read_calls = extract_read_calls(outputs.get("events", []))
    read = [_normalize(c.get("file_path"), match) for c in read_calls]

    def _hit(exp):
        if match == "suffix":
            return any(r.endswith(exp) or exp.endswith(r) for r in read if r)
        return exp in read

    hits = [e for e in expected if _hit(e)]
    coverage = len(hits) / len(expected)
    passed = coverage >= min_coverage
    return (passed,
            f"Read {len(hits)}/{len(expected)} expected docs "
            f"(coverage {coverage:.0%}, threshold {min_coverage:.0%})")
