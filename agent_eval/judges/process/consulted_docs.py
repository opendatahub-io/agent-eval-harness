"""Checks the agent read the documentation files listed in expected_files.

Required fields: events, annotations.expected_files
Failure means: The agent did not read enough of the expected documentation.

Arguments:
    min_coverage (float): fraction of expected_files that must be read (default 0.8)
    match (str): path match strategy — "suffix" (default), "exact", or "basename"
    include_subagents (bool): include reads from subagent/Explore events (default true).
        Agents in prompt mode typically delegate file reads to Explore subagents,
        so disabling this will miss most reads.
    include_grep (bool): count Grep tool calls as file reads (default true).
        Agents using Grep to search file contents have effectively consulted
        those files.
    preloaded_files (list[str]): files that are auto-loaded into agent context
        (e.g. CLAUDE.md). These count as "already consulted" without needing
        a Read tool call. Use this for workspace_mode: repo evaluations where
        Claude Code auto-loads CLAUDE.md on startup.
"""

from agent_eval.events import extract_read_calls


def _normalize(path, match):
    path = (path or "").replace("\\", "/").strip()
    if match == "basename":
        return path.rsplit("/", 1)[-1]
    return path


def _suffix_match(read_path, expected):
    """True when one path is a suffix of the other at a path-component boundary.

    Component-boundary matching prevents false positives like ``report.md``
    matching ``final-report.md`` (plain ``endswith`` substring match), while
    still letting a repo-relative ``docs/setup.md`` match an absolute read of
    ``/home/user/project/docs/setup.md`` (and vice versa).
    """
    if not read_path or not expected:
        return False
    return (
        read_path == expected
        or read_path.endswith("/" + expected)
        or expected.endswith("/" + read_path)
    )


def judge(outputs, **kwargs):
    min_coverage = kwargs.get("min_coverage", 0.8)
    match = kwargs.get("match", "suffix")
    include_subagents = kwargs.get("include_subagents", True)
    include_grep = kwargs.get("include_grep", True)
    preloaded_files = kwargs.get("preloaded_files", [])

    expected = [e for e in (
        _normalize(p, match)
        for p in outputs.get("annotations", {}).get("expected_files", [])
    ) if e]
    if not expected:
        return (True, "No expected_files specified — nothing to verify")

    preloaded = [_normalize(p, match) for p in preloaded_files if p]

    read_calls = extract_read_calls(outputs.get("events", []),
                                    include_subagents=include_subagents,
                                    include_grep=include_grep)
    read = [r for r in (_normalize(c.get("file_path"), match)
                        for c in read_calls) if r]

    def _hit(exp):
        if match == "suffix":
            return (any(_suffix_match(r, exp) for r in read)
                    or any(_suffix_match(p, exp) for p in preloaded))
        return exp in read or exp in preloaded

    hits = [e for e in expected if _hit(e)]
    preloaded_hits = []
    if preloaded:
        def _read_hit(exp):
            if match == "suffix":
                return any(_suffix_match(r, exp) for r in read)
            return exp in read
        preloaded_hits = [e for e in hits if not _read_hit(e)]

    coverage = len(hits) / len(expected)
    passed = coverage >= min_coverage

    detail = f"Read {len(hits)}/{len(expected)} expected docs "
    detail += f"(coverage {coverage:.0%}, threshold {min_coverage:.0%})"
    if preloaded_hits:
        detail += f" [{len(preloaded_hits)} via auto-load]"

    return (passed, detail)
