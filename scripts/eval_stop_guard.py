#!/usr/bin/env python3
"""Stop hook: keep the eval-run orchestrator from ending its turn while it still
has a live background task (e.g. ``execute.py``), with a bounded escape hatch so
a genuinely stuck run can never hang the session forever.

Root cause it fixes
-------------------
In headless / CI runs the orchestrator sometimes launches ``execute.py`` with
``run_in_background: true`` and then ends its turn right away ("I'll report
when it finishes"). The session tears down, the background task is killed
before it writes ``run_result.json``, and the eval reports an *empty* result as
green. This has been observed repeatedly on the OpenShift CI
``eval-payload-analysis`` job — including under harness 1.40.x, whose bg-kill
handling only *relabels* per-case kills and cannot help when the whole
orchestrator dies before any ``run_result.json`` exists.

The eval-run skill already tells the agent to poll until completion, but in a
non-interactive session nothing *enforces* it. This hook does: while the
session has a live background task, it blocks the Stop, forcing the agent to
keep polling until the task finishes.

Why the transcript, not ``pgrep``
---------------------------------
Claude Code records background-task lifecycle in the session transcript
(handed to the hook via ``transcript_path``):

  * ``background_tasks_changed`` — carries the *full* current live-task list
    (``tasks: []`` once they all end);
  * ``task_started`` — a task began;
  * ``task_updated`` / ``task_notification`` — status transitions, terminal
    when ``completed`` / ``failed`` / ``killed`` / ``stopped`` / ``cancelled``.

Because the transcript is per-session, this is inherently scoped to *this*
agent. A host-wide ``pgrep execute.py`` would also match executions from other
concurrent sessions and block spuriously; reading the transcript does not.

Escape hatch (stuck runs)
-------------------------
If a background task hangs, blocking forever would just defer the failure to
the CI pod's outer timeout. So the hook records when it *first* blocked this
session and, once more than ``AGENT_EVAL_STOP_GUARD_MAX_MIN`` (default 180)
have elapsed, it allows the Stop and emits a loud warning to stderr. Progress
sniffing (e.g. console.log mtime) is deliberately NOT used: at high reasoning
effort a single case can run ~30 min with no console output, so "quiet" is not
"stuck". A bounded wall-clock ceiling is the only false-positive-free signal.

Contract (Claude Code ``Stop`` hook)
------------------------------------
* stdin: JSON with ``transcript_path``, ``session_id``, ``cwd``,
  ``stop_hook_active``, ``hook_event_name`` (best-effort; absence tolerated).
* allow the stop: exit 0 with no stdout.
* block the stop: print ``{"decision": "block", "reason": "..."}`` and exit 0.
  The reason is surfaced to the model, which then continues instead of stopping.

Scope
-----
Enabled by default, everywhere. The background-kill failure is specific to
headless / print mode (``claude -p`` / the Agent SDK), where ending the turn
tears down the session and kills background tasks ~5s later — but Claude Code
exposes no reliable signal (no env var, no Stop-payload field) to distinguish
headless from interactive, so the guard cannot gate on mode and instead runs
everywhere. In interactive sessions background tasks *persist* across turns, so
a spurious block there is at most a nuisance: interrupt with Esc, or set
``AGENT_EVAL_STOP_GUARD=0``. Disabled explicitly when that variable is falsy.

Environment
-----------
* ``AGENT_EVAL_STOP_GUARD``        — force on/off (default on).
* ``AGENT_EVAL_STOP_GUARD_MAX_MIN``— escape-hatch ceiling in minutes (default
  180; ``0`` disables the ceiling and blocks until the task ends).
"""

import json
import os
import re
import sys
import time

TERMINAL_STATUSES = {"completed", "failed", "killed", "stopped", "cancelled"}
DEFAULT_MAX_MIN = 180


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _guard_enabled():
    """Enabled by default; an explicit ``AGENT_EVAL_STOP_GUARD`` always wins.

    The failure this guards against only happens in headless / print mode, but
    that mode is not detectable from a hook, so the guard defaults on. A
    spurious block in an interactive session is only a nuisance (background
    tasks persist there anyway), so defaulting on is safe.
    """
    override = os.environ.get("AGENT_EVAL_STOP_GUARD")
    if override is not None:
        return _truthy(override)
    return True


def _max_seconds():
    raw = os.environ.get("AGENT_EVAL_STOP_GUARD_MAX_MIN")
    if raw is None:
        return DEFAULT_MAX_MIN * 60
    try:
        return max(0, float(raw)) * 60
    except (ValueError, TypeError):
        return DEFAULT_MAX_MIN * 60


def _live_background_tasks(transcript_path):
    """Reconstruct the set of still-running background tasks from the transcript.

    Returns a dict of ``task_id -> description`` for tasks that started and have
    not reached a terminal status, or ``None`` if the transcript cannot be read
    (caller fails open in that case).
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return None

    live = {}
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if event.get("type") != "system":
                    continue
                subtype = event.get("subtype")

                if subtype == "background_tasks_changed":
                    # Authoritative full snapshot of the current live set.
                    live = {
                        t.get("task_id"): t.get("description", "")
                        for t in (event.get("tasks") or [])
                        if t.get("task_id")
                    }
                elif subtype == "task_started":
                    tid = event.get("task_id")
                    if tid:
                        live[tid] = event.get("description", "")
                elif subtype in ("task_updated", "task_notification"):
                    tid = event.get("task_id")
                    if not tid:
                        continue
                    status = (event.get("status")
                              or (event.get("patch") or {}).get("status"))
                    if status in TERMINAL_STATUSES:
                        live.pop(tid, None)
    except OSError:
        return None

    return live


def _state_path(session_id):
    tmp = os.environ.get("TMPDIR") or "/tmp"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "default")
    return os.path.join(tmp, f"agent-eval-stop-guard-{safe}.json")


def _first_block_ts(session_id, now):
    """Return when this session first blocked, recording ``now`` if new."""
    path = _state_path(session_id)
    try:
        with open(path, encoding="utf-8") as fh:
            ts = json.load(fh).get("first_block")
            if isinstance(ts, (int, float)):
                return ts
    except (OSError, ValueError, TypeError):
        pass
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"first_block": now}, fh)
    except OSError:
        pass
    return now


def _clear_state(session_id):
    try:
        os.remove(_state_path(session_id))
    except OSError:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    session_id = payload.get("session_id")

    if not _guard_enabled():
        _clear_state(session_id)
        return 0  # allow stop

    live = _live_background_tasks(payload.get("transcript_path"))
    # Fail open if we could not read the transcript, or nothing is running —
    # better to let the agent stop than to hang a session we can't reason about.
    if not live:
        _clear_state(session_id)
        return 0

    now = time.time()
    max_s = _max_seconds()
    if max_s > 0:
        elapsed = now - _first_block_ts(session_id, now)
        if elapsed > max_s:
            desc = next(iter(live.values()), "") or "a background task"
            print(
                "eval_stop_guard: escape hatch — blocked for "
                f"{elapsed / 60:.0f} min (ceiling "
                f"{max_s / 60:.0f} min) with {len(live)} task(s) still live "
                f"(e.g. \"{desc}\"). Allowing stop; the run is likely stuck and "
                "may be incomplete. Tune with AGENT_EVAL_STOP_GUARD_MAX_MIN.",
                file=sys.stderr,
            )
            _clear_state(session_id)
            return 0  # allow stop

    # Small courtesy pause so repeated block -> stop cycles don't churn the
    # model faster than roughly once per turn. Stays well within the hook
    # timeout; it does not itself wait for completion.
    time.sleep(5)

    desc = next(iter(live.values()), "") or "a background task"
    reason = (
        f"You still have {len(live)} background task(s) running "
        f"(e.g. \"{desc}\"). Do NOT end your turn — ending it now tears down "
        "the session and kills the task before it writes run_result.json, "
        "which reports an empty eval result as green. Poll progress with "
        "`tail -20 <output_dir>/console.log` (or BashOutput on the task) every "
        "2-3 minutes, and only stop once the task has finished and "
        "run_result.json exists."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
