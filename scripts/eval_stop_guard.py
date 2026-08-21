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

import hashlib
import json
import os
import stat
import sys
import time

TERMINAL_STATUSES = {"completed", "failed", "killed", "stopped", "cancelled"}
DEFAULT_MAX_MIN = 180
STATE_DIRNAME = "agent-eval-stop-guard"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# Sentinel: no durable first-block timestamp exists and one cannot be written.
# Distinct from a real timestamp so main() can fail open instead of treating a
# state-write failure as "elapsed ~= 0" and blocking the session forever.
_PERSIST_FAILED = object()


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


def _transcript_root():
    """Directory tree Claude Code keeps session transcripts under.

    Transcripts live beneath ``${CLAUDE_CONFIG_DIR:-~/.claude}``. Confining
    reads to this root stops a crafted ``transcript_path`` on hook stdin from
    pointing the guard at an arbitrary file (CWE-22). Returns ``None`` if the
    root can't be resolved, in which case the caller fails open.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")
    try:
        return os.path.realpath(base)
    except OSError:
        return None


def _within(root, path):
    """True if ``path`` resolves to a location inside ``root`` (no symlink escape)."""
    if not root:
        return False
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return real == root or real.startswith(root + os.sep)


def _open_read_nofollow(path):
    """Open ``path`` for reading, refusing a final-component symlink.

    Returns a text file object, or ``None`` on any failure (missing file, a
    symlink swapped in via TOCTOU, permission error, …).
    """
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError:
        return None
    try:
        return os.fdopen(fd, encoding="utf-8", errors="replace")
    except OSError:
        os.close(fd)
        return None


def _live_background_tasks(transcript_path):
    """Reconstruct the set of still-running background tasks from the transcript.

    Returns a dict of ``task_id -> description`` for tasks that started and have
    not reached a terminal status, or ``None`` if the transcript cannot be read
    (caller fails open in that case).
    """
    if not transcript_path or not _within(_transcript_root(), transcript_path):
        return None

    fh = _open_read_nofollow(transcript_path)
    if fh is None:
        return None

    live = {}
    try:
        with fh:
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


def _state_key(session_id, transcript_path):
    """Stable, collision-resistant per-session key for the escape-hatch timer.

    Prefer ``session_id``; fall back to the transcript path (also per-session)
    when it is absent. Returns ``None`` when neither identity exists — the
    caller then fails open rather than sharing one ``default`` file across
    unrelated sessions, where one session could clear or inherit another's timer.
    """
    ident = session_id or transcript_path
    if not ident:
        return None
    return hashlib.sha256(ident.encode("utf-8", "replace")).hexdigest()


def _state_dir():
    """Plugin-owned ``0700`` directory for guard state, or ``None`` if it can't
    be secured.

    Kept private and non-symlinked so a shared ``TMPDIR`` can't be used to make
    the hook follow an attacker-planted symlink onto another file (CWE-59/377).
    """
    tmp = os.environ.get("TMPDIR") or "/tmp"
    path = os.path.join(tmp, STATE_DIRNAME)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
    except OSError:
        return None
    # Refuse a symlink, a non-directory, or a directory owned by someone else.
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return None
    try:
        os.chmod(path, 0o700)  # tighten in case it pre-existed with looser bits
    except OSError:
        return None
    return path


def _state_path(session_id, transcript_path):
    key = _state_key(session_id, transcript_path)
    if not key:
        return None
    directory = _state_dir()
    if not directory:
        return None
    return os.path.join(directory, f"{key}.json")


def _first_block_ts(path, now):
    """When this session first blocked, recording ``now`` if new.

    Returns ``_PERSIST_FAILED`` when no durable timestamp exists and one cannot
    be written, so the caller can fail open instead of letting a state-write
    failure block the session forever (each later Stop would otherwise see
    ``elapsed ~= 0``).
    """
    if not path:
        return _PERSIST_FAILED

    fh = _open_read_nofollow(path)
    if fh is not None:
        try:
            with fh:
                ts = json.load(fh).get("first_block")
            if isinstance(ts, (int, float)):
                return ts
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    # No usable timestamp yet — create one with no-follow, 0600 semantics.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600)
    except OSError:
        return _PERSIST_FAILED
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"first_block": now}, fh)
    except OSError:
        return _PERSIST_FAILED
    return now


def _clear_state(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    state_path = _state_path(session_id, transcript_path)

    if not _guard_enabled():
        _clear_state(state_path)
        return 0  # allow stop

    live = _live_background_tasks(transcript_path)
    # Fail open if we could not read the transcript, or nothing is running —
    # better to let the agent stop than to hang a session we can't reason about.
    if not live:
        _clear_state(state_path)
        return 0

    now = time.time()
    max_s = _max_seconds()
    if max_s > 0:
        first = _first_block_ts(state_path, now)
        if first is _PERSIST_FAILED:
            # Can't durably track elapsed time (no session identity or an
            # unwritable state dir). Fail open rather than risk blocking forever.
            print(
                "eval_stop_guard: cannot persist the escape-hatch timer; "
                "allowing stop rather than risk hanging the session. A "
                "background task may still be running and the eval incomplete.",
                file=sys.stderr,
            )
            return 0  # allow stop
        elapsed = now - first
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
            _clear_state(state_path)
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
