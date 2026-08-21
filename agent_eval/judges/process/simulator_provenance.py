"""Certifies simulated-user answer provenance for intercepted questions.

Required fields: hook_answers, interception_configured, events
Failure means: At least one intercepted AskUserQuestion was answered by the
arbitrary fallback tier, interception was silently disabled, an LLM-tier
answer attempt errored, or questions were asked with no provenance ledger
recorded at all.

A pass certifies **answer-provenance coverage only** — every intercepted
answer came from a recorded tier (``override`` or ``llm``) and interception
was never silently disabled. It is explicitly NOT simulator calibration:
tier=``llm`` answers are *reported*, not validated against human answers
(the paper's P1 reporting clause, not its calibration clause).

Blind spot (unavoidable): a PreToolUse hook killed from outside (crash,
OOM, external timeout) is treated as pass-through by the CLI and cannot
write a record from inside the dying process. The missing-ledger check
here is its detectable signature — interception configured, AskUserQuestion
calls in the trace, no ledger — but a hook killed *after* some questions
were recorded can only be caught by the per-case coverage count.

Batch mode: the single shared interceptor produces one run-level ledger,
so records cannot be attributed to individual cases. The judge still fails
on any fallback/disabled/error record, but skips per-case coverage counting
and labels the rationale as run-level (unattributed) provenance.
"""

_MAX_QUESTIONS_LISTED = 5
_QUESTION_TRUNC = 80

_RUN_SCOPE_NOTE = (" [run-level (unattributed) provenance — batch mode, "
                   "answers not attributed to cases]")


def _count_ask_user(events):
    """Count AskUserQuestion tool_use calls and the questions they carry."""
    calls = 0
    questions = 0
    for event in events or []:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        for tool in event.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            if tool.get("name") != "AskUserQuestion":
                continue
            calls += 1
            inp = tool.get("input")
            if (isinstance(inp, dict) and isinstance(inp.get("questions"), list)
                    and inp["questions"]):
                questions += len(inp["questions"])
            else:
                questions += 1
    return calls, questions


def _q(record):
    """A short display form of a record's question text."""
    text = str(record.get("question", "?"))
    if len(text) > _QUESTION_TRUNC:
        text = text[:_QUESTION_TRUNC] + "…"
    return repr(text)


def judge(outputs, **kwargs):
    configured = bool(outputs.get("interception_configured"))
    ledger = outputs.get("hook_answers")
    scope = outputs.get("hook_answers_scope")
    calls, questions = _count_ask_user(outputs.get("events"))

    if ledger is None:
        if not configured:
            return (True, "No tool interception configured — nothing to "
                          "certify (trivial pass)")
        if calls > 0:
            return (False,
                    f"no ledger collected but trace shows {calls} "
                    "AskUserQuestion call(s) — simulated-user provenance "
                    "unrecorded: hook crash, pre-ledger interceptor, or "
                    "collection gap")
        return (True, "Interception configured but the trace shows no "
                      "AskUserQuestion calls — nothing to certify")

    # Ledger present (possibly empty): count tiers, gather problems.
    n_override = n_llm = n_fallback = n_disabled = 0
    problems = []
    disabled_reasons = []
    fallback_questions = []
    error_records = []
    answered = 0
    for record in ledger:
        if not isinstance(record, dict):
            continue
        tier = record.get("tier")
        if tier == "override":
            n_override += 1
            answered += 1
        elif tier == "llm":
            n_llm += 1
            answered += 1
        elif tier == "fallback":
            n_fallback += 1
            answered += 1
            if len(fallback_questions) < _MAX_QUESTIONS_LISTED:
                cause = record.get("error") or record.get("llm_raw")
                note = f" (cause: {str(cause)[:_QUESTION_TRUNC]})" if cause else ""
                fallback_questions.append(_q(record) + note)
        elif tier == "disabled":
            n_disabled += 1
            reason = str(record.get("reason", "?"))
            if reason not in disabled_reasons:
                disabled_reasons.append(reason)
        if tier != "fallback" and record.get("error"):
            error_records.append(_q(record))

    if n_disabled:
        problems.append(
            f"{n_disabled} disabled record(s) — interception silently "
            f"disabled (reasons: {', '.join(disabled_reasons)})")
    if n_fallback:
        problems.append(
            f"{n_fallback} fallback-tier answer(s) — arbitrary answers, not "
            f"case-specific (questions: {', '.join(fallback_questions)})")
    if error_records:
        problems.append(
            f"{len(error_records)} record(s) carry an error "
            f"(questions: {', '.join(error_records[:_MAX_QUESTIONS_LISTED])})")
    # Coverage: recorded answers must account for every question the trace
    # shows. Catches swallowed best-effort writes and an empty-but-present
    # ledger. Skipped at run scope, where per-case attribution is impossible.
    if scope != "run" and questions > 0 and answered < questions:
        problems.append(
            f"partial provenance — {answered} recorded answer(s) for "
            f"{questions} question(s) observed in the trace")

    tier_summary = (f"tiers: {n_override} override / {n_llm} llm / "
                    f"{n_fallback} fallback")
    run_note = _RUN_SCOPE_NOTE if scope == "run" else ""

    if problems:
        return (False, "; ".join(problems) + f" ({tier_summary})" + run_note)
    return (True,
            f"All {answered} answer(s) from recorded tiers "
            f"({tier_summary})" + run_note)
