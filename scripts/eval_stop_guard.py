#!/usr/bin/env python3
"""Stop hook: keep the eval-run orchestrator from ending its turn while an eval
executor (``execute.py``) is still alive, with a bounded escape hatch so a
genuinely stuck run can never hang the session forever.

Root cause it fixes
-------------------
In headless / CI runs the orchestrator sometimes launches ``execute.py`` with
``run_in_background: true`` and then ends its turn right away ("I'll report
when it finishes"). The session tears down, the background process is killed
before it writes ``run_result.json``, and the eval records an *empty* result as
a false pass. This has been observed repeatedly on the OpenShift CI
``eval-payload-analysis`` job — including under harness 1.40.x, whose bg-kill
handling only *relabels* per-case kills and cannot help when the whole
orchestrator dies before any ``run_result.json`` exists.

The eval-run skill already tells the agent to poll until completion, but in a
non-interactive session nothing *enforces* it. This hook does: while an
executor is alive, it blocks the Stop, forcing the agent to keep polling until
the run finishes.

Why a pidfile, not the transcript
---------------------------------
An earlier version reconstructed the live-task set from ``system`` events in
the session transcript (``background_tasks_changed`` / ``task_started`` /
``task_updated`` / ``task_notification``). Those event subtypes do not exist as
structured records in current Claude Code transcripts — background-task
lifecycle is encoded as Bash ``tool_result`` payloads and ``<task-notification>``
XML embedded in *user*-role message content, and the exact shape drifts across
CLI versions. Parsing it made the guard silently inert (it always saw zero
tasks and allowed every stop), reproducing the very empty-result failure it was
meant to prevent.

Instead the harness owns ``execute.py``, so we use a first-party signal that is
CLI-version-proof and self-scoping:

  * ``execute.py`` writes ``<output_dir>/execute.pid`` (pid + an OS process
    creation marker) before it does anything else — ahead of its own imports,
    argument parsing and config load, since the guard is blind until that file
    exists and the racing orchestrator ends its turn immediately after launch —
    and removes it at exit.
  * this hook scans ``$AGENT_EVAL_RUNS_DIR`` (default ``eval/runs``) under the
    session cwd for ``execute.pid`` files, and treats a run as *live* when the
    recorded pid is still alive (``os.kill(pid, 0)``), the creation marker still
    matches (defeats pid reuse), and no ``run_result.json`` sits beside it yet.

Because only ``execute.py`` writes ``execute.pid``, the guard is inherently
scoped to evals: a backgrounded dev server or a deliberately-backgrounded agent
is never mistaken for one, so it is not blocked.

Escape hatch (stuck runs)
-------------------------
If an executor hangs, blocking forever would just defer the failure to the CI
pod's outer timeout. So the hook records when it *first* blocked this session
and, once more than ``AGENT_EVAL_STOP_GUARD_MAX_MIN`` (default 180) minutes have
elapsed, it allows the Stop and emits a loud warning to stderr. Progress
sniffing (e.g. console.log mtime) is deliberately NOT used: at high reasoning
effort a single case can run ~30 min with no console output, so "quiet" is not
"stuck". A bounded wall-clock ceiling is the only false-positive-free signal.

Contract (Claude Code ``Stop`` hook)
------------------------------------
* stdin: JSON with ``session_id``, ``cwd``, ``transcript_path``,
  ``stop_hook_active``, ``hook_event_name`` (best-effort; absence tolerated).
* allow the stop: exit 0 with no stdout.
* block the stop: print ``{"decision": "block", "reason": "..."}`` and exit 0.
  The reason is surfaced to the model, which then continues instead of stopping.

Scope (per project, NOT per session)
------------------------------------
Enabled by default, everywhere. Note the deliberate trade-off against the
transcript version this replaced: a pidfile scan is scoped to the *project*,
not to the session that launched the run. So a second session working in the
same repo while an eval runs — a user chatting about the code in another
terminal, say — is also blocked from ending its turn, until the executor exits
or the escape-hatch ceiling is reached.

That is accepted rather than fixed: the blast radius is bounded (the ceiling
applies, the block reason names the offending run directory, and the run really
is live), whereas tying the block to the launching session means walking the
process ancestry to a top-level ``claude`` pid and comparing it against the
hook's own — more machinery, and more ways to be wrong, than the nuisance
warrants. ``AGENT_EVAL_STOP_GUARD=0`` disables the guard for such a session.
Fails open on any error.

Environment
-----------
* ``AGENT_EVAL_STOP_GUARD``        — force on/off (default on).
* ``AGENT_EVAL_STOP_GUARD_MAX_MIN``— escape-hatch ceiling in minutes (default
  180; ``0`` disables the ceiling and blocks until the executor exits).
* ``AGENT_EVAL_RUNS_DIR``          — runs directory to scan (default
  ``eval/runs`` under the session cwd).
"""

import glob
import hashlib
import json
import os
import stat
import subprocess
import sys
import time

DEFAULT_MAX_MIN = 180
STATE_DIRNAME = "agent-eval-stop-guard"
# A pidfile older than this is treated as abandoned regardless of pid liveness.
# Comfortably longer than any eval (the CI step caps at 3h).
MAX_PIDFILE_AGE_S = 24 * 3600
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# Sentinel: no durable first-block timestamp exists and one cannot be written.
# Distinct from a real timestamp so main() can fail open instead of treating a
# state-write failure as "elapsed ~= 0" and blocking the session forever.
_PERSIST_FAILED = object()


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _guard_enabled():
    """Enabled by default; an explicit ``AGENT_EVAL_STOP_GUARD`` always wins."""
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


# ── Process-liveness signal ──────────────────────────────────────────────

def _pid_alive(pid):
    """True if a process with ``pid`` currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _process_create_time(pid):
    """Best-effort, stable per-process creation marker used to detect pid reuse.

    Returns a string identity for the process currently at ``pid`` (Linux boot
    tick count, or the macOS start timestamp), or ``None`` if it cannot be
    determined. Must match ``execute.py``'s derivation for the reuse check to
    work, so the two implementations are kept identical.
    """
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            data = fh.read()
        # comm (field 2) is parenthesised and may itself contain spaces/parens;
        # starttime is field 22 -> index 19 after the final ')'.
        after = data[data.rfind(")") + 2:].split()
        return after[19]
    except (OSError, IndexError, ValueError):
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
    return None


# ── Filesystem helpers (symlink-hardened) ────────────────────────────────

def _within(root, path):
    """True if ``path`` resolves to a location inside ``root`` (no symlink escape)."""
    if not root:
        return False
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    root = os.path.realpath(root)
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


# ── Live-executor detection ──────────────────────────────────────────────

def _run_output_roots(cwd):
    """Runs directories to scan for ``execute.pid`` (existing dirs, de-duped)."""
    base = cwd or os.getcwd()
    candidates = []
    env = os.environ.get("AGENT_EVAL_RUNS_DIR")
    if env:
        candidates.append(env if os.path.isabs(env) else os.path.join(base, env))
    candidates.append(os.path.join(base, "eval", "runs"))

    seen = set()
    roots = []
    for c in candidates:
        try:
            real = os.path.realpath(c)
        except OSError:
            continue
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            roots.append(real)
    return roots


def _read_pidfile(path):
    fh = _open_read_nofollow(path)
    if fh is None:
        return None
    try:
        with fh:
            data = json.load(fh)
    except (ValueError, TypeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _pidfile_candidates(root):
    """Pidfile paths under ``root``, found with bounded-depth globs.

    Pidfiles only ever live at ``<runs>/<eval-name>/<run-id>/execute.pid``, so a
    handful of fixed-depth patterns covers every layout. A ``**`` recursive glob
    would instead walk the whole runs tree on *every* Stop — including the
    subagent transcripts and collected artifacts under each run, which reach
    six figures of files on a busy tree (measured: 1,859 files → 64 ms recursive
    vs 0.6 ms bounded).
    """
    matches = []
    for pattern in ("execute.pid", "*/execute.pid", "*/*/execute.pid",
                    "*/*/*/execute.pid"):
        try:
            matches.extend(glob.glob(os.path.join(root, pattern)))
        except OSError:
            continue
    return matches


def _live_eval_runs(cwd):
    """Return the output dirs of eval executors that are still alive.

    An executor counts as live when its ``execute.pid`` names a process that is
    still running, whose creation marker still matches (not a reused pid), and
    which has not yet written ``run_result.json`` beside the pidfile. Any error
    on a given pidfile just skips it — the guard fails open, never closed.
    """
    live = []
    seen = set()
    now = time.time()
    for root in _run_output_roots(cwd):
        for pidfile in _pidfile_candidates(root):
            real = os.path.realpath(pidfile)
            if real in seen:
                continue
            seen.add(real)
            if not _within(root, pidfile):
                continue  # symlink escaping the runs tree
            info = _read_pidfile(pidfile)
            if not info:
                continue
            pid = info.get("pid")
            if not isinstance(pid, int):
                continue
            run_dir = os.path.dirname(pidfile)
            if os.path.exists(os.path.join(run_dir, "run_result.json")):
                continue  # run already finished
            # Staleness backstop. The create_time check below is the primary
            # pid-reuse defense, but it is inert where no marker is obtainable
            # (no /proc and not darwin), leaving a reused pid able to block
            # until the ceiling. No eval outlives MAX_PIDFILE_AGE_S, so an
            # older pidfile is abandoned by definition — on every platform.
            try:
                if now - os.path.getmtime(pidfile) > MAX_PIDFILE_AGE_S:
                    continue
            except OSError:
                continue
            if not _pid_alive(pid):
                continue  # executor already dead — nothing to guard
            recorded = info.get("create_time")
            current = _process_create_time(pid)
            if (recorded is not None and current is not None
                    and str(recorded) != str(current)):
                continue  # pid was reused by an unrelated process
            live.append(run_dir)
    return live


# ── Escape-hatch state (per session, symlink/ownership hardened) ─────────

def _state_key(session_id, fallback):
    """Stable, collision-resistant per-session key for the escape-hatch timer.

    Prefer ``session_id``; fall back to the session cwd. Returns ``None`` when
    neither identity exists — the caller then fails open rather than sharing one
    ``default`` file across unrelated sessions, where one session could clear or
    inherit another's timer.
    """
    ident = session_id or fallback
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


def _state_path(session_id, fallback):
    key = _state_key(session_id, fallback)
    if not key:
        return None
    directory = _state_dir()
    if not directory:
        return None
    return os.path.join(directory, f"{key}.json")


def _first_block_ts(path, now, runs_key):
    """When this session first blocked *on this set of runs*, recording ``now``
    if new.

    ``runs_key`` scopes the timer to the runs actually being waited on. Without
    it a session that chains evals back to back — with no intervening idle Stop
    to clear the state — would inherit the previous run's ``first_block`` and
    could trip the escape hatch almost immediately on a fresh run. A changed run
    set means new work, so the clock restarts.

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
                data = json.load(fh)
            ts = data.get("first_block")
            if isinstance(ts, (int, float)) and data.get("runs") == runs_key:
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
            json.dump({"first_block": now, "runs": runs_key}, fh)
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
    cwd = payload.get("cwd") or os.getcwd()
    state_path = _state_path(session_id, cwd)

    if not _guard_enabled():
        _clear_state(state_path)
        return 0  # allow stop

    live = _live_eval_runs(cwd)
    # Fail open if no executor is alive — better to let the agent stop than to
    # hang a session with nothing left to wait for.
    if not live:
        _clear_state(state_path)
        return 0

    now = time.time()
    max_s = _max_seconds()
    if max_s > 0:
        first = _first_block_ts(state_path, now, sorted(live))
        if first is _PERSIST_FAILED:
            # Can't durably track elapsed time (no session identity or an
            # unwritable state dir). Fail open rather than risk blocking forever.
            print(
                "eval_stop_guard: cannot persist the escape-hatch timer; "
                "allowing stop rather than risk hanging the session. An eval "
                "executor may still be running and the result incomplete.",
                file=sys.stderr,
            )
            return 0  # allow stop
        elapsed = now - first
        if elapsed > max_s:
            print(
                "eval_stop_guard: escape hatch — blocked for "
                f"{elapsed / 60:.0f} min (ceiling {max_s / 60:.0f} min) with "
                f"{len(live)} eval executor(s) still alive (e.g. \"{live[0]}\"). "
                "Allowing stop; the run is likely stuck and may be incomplete. "
                "Tune with AGENT_EVAL_STOP_GUARD_MAX_MIN.",
                file=sys.stderr,
            )
            _clear_state(state_path)
            return 0  # allow stop

    # Small courtesy pause so repeated block -> stop cycles don't churn the
    # model faster than roughly once per turn. Stays well within the hook
    # timeout; it does not itself wait for completion.
    time.sleep(5)

    run_dir = live[0]
    reason = (
        f"Eval executor still running ({run_dir} — possibly another session). "
        "Don't end your turn: stopping kills it before run_result.json is "
        f"written. Poll `tail -20 {run_dir}/console.log`; stop once "
        "run_result.json exists."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
