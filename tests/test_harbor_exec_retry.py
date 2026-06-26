"""Tests for the scoped establishment-retry in KubernetesEnvironment._ws_exec.

Retry must happen ONLY when the WebSocket never established (the command
provably never ran — safe to retry, even the agent). A failure after the
command started must be surfaced as-is, never retried, so a possibly-executed
command (the agent run!) is never re-run.

kubernetes.py imports the `harbor` library, which lives in the Harbor CLI's
own interpreter rather than the eval venv — skip cleanly where it is absent.
"""

import logging
import sys
from pathlib import Path

import pytest

# Skip unless the real Harbor framework is importable. importorskip("harbor") is
# not enough: CI may have an unrelated top-level `harbor` module, which passes
# that check but then fails collection when kubernetes.py does
# `from harbor.environments.base import ...`. Probe the exact submodule instead.
pytest.importorskip("harbor.environments.base")
pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.harbor.kubernetes import KubernetesEnvironment, ExecResult


def _env():
    # Bypass __init__ (needs a live cluster); we only exercise the pure wrapper.
    env = object.__new__(KubernetesEnvironment)
    env.logger = logging.getLogger("test-exec-retry")
    return env


def test_retries_establishment_failure_then_succeeds(monkeypatch):
    env = _env()
    calls = []
    seq = [
        (ExecResult(stdout="", stderr="conn refused", return_code=1), False,
         RuntimeError("conn refused")),
        (ExecResult(stdout="", stderr="conn refused", return_code=1), False,
         RuntimeError("conn refused")),
        (ExecResult(stdout="ok", stderr="", return_code=0), True, None),
    ]

    def fake_once(cmd, timeout):
        calls.append(cmd)
        return seq[len(calls) - 1]

    monkeypatch.setattr(env, "_ws_exec_once", fake_once)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    res = env._ws_exec("test.sh", None)
    assert res.return_code == 0 and res.stdout == "ok"
    assert len(calls) == 3  # two establishment retries, then success


def test_post_establishment_failure_is_not_retried(monkeypatch):
    # If the connection established, the command may have run — re-running the
    # agent would be unsafe. The original exception must propagate, once.
    env = _env()
    calls = []

    def fake_once(cmd, timeout):
        calls.append(cmd)
        return (ExecResult(stdout="", stderr="boom", return_code=1), True,
                RuntimeError("mid-run drop"))

    monkeypatch.setattr(env, "_ws_exec_once", fake_once)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="mid-run drop"):
        env._ws_exec("run-agent", None)
    assert len(calls) == 1  # established -> no retry


def test_post_establishment_timeout_returned_not_retried(monkeypatch):
    # rc=124 timeout after establishment: return as-is, no retry.
    env = _env()
    calls = []

    def fake_once(cmd, timeout):
        calls.append(cmd)
        return ExecResult(stdout="", stderr="timed out", return_code=124), True, None

    monkeypatch.setattr(env, "_ws_exec_once", fake_once)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    res = env._ws_exec("run-agent", None)
    assert res.return_code == 124
    assert len(calls) == 1


def test_establishment_failure_exhausts_then_raises(monkeypatch):
    env = _env()
    calls = []

    def fake_once(cmd, timeout):
        calls.append(cmd)
        return (ExecResult(stdout="", stderr="never up", return_code=1), False,
                RuntimeError("never up"))

    monkeypatch.setattr(env, "_ws_exec_once", fake_once)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="never up"):
        env._ws_exec("test.sh", None)
    assert len(calls) == env._EXEC_ESTABLISH_RETRIES + 1


def test_establishment_failure_without_exception_returns_result(monkeypatch):
    # Hard-timeout-before-establish path: no exc, not established -> retried,
    # then the last result is returned (not raised).
    env = _env()
    calls = []

    def fake_once(cmd, timeout):
        calls.append(cmd)
        return ExecResult(stdout="", stderr="HAProxy connection dead",
                          return_code=124), False, None

    monkeypatch.setattr(env, "_ws_exec_once", fake_once)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    res = env._ws_exec("test.sh", None)
    assert res.return_code == 124
    assert len(calls) == env._EXEC_ESTABLISH_RETRIES + 1


def test_failed_exec_is_logged_with_diagnostics(monkeypatch, caplog):
    # A post-establishment failure must be logged (rc, established, cmd, detail)
    # so the reason is diagnosable rather than vanishing into "step failed".
    env = _env()

    def fake_once(cmd, timeout):
        return ExecResult(stdout="", stderr="oom-killed", return_code=137), True, None

    monkeypatch.setattr(env, "_ws_exec_once", fake_once)
    with caplog.at_level(logging.WARNING, logger="test-exec-retry"):
        res = env._ws_exec("cd /workspace && ./steps/auto-fix/tests/test.sh", None)
    assert res.return_code == 137
    msgs = [r.getMessage() for r in caplog.records]
    assert any("exec FAILED" in m and "rc=137" in m and "established=True" in m
               and "test.sh" in m for m in msgs), msgs


def test_successful_exec_does_not_warn(monkeypatch, caplog):
    env = _env()
    monkeypatch.setattr(env, "_ws_exec_once",
                        lambda c, t: (ExecResult(stdout="ok", stderr="", return_code=0), True, None))
    with caplog.at_level(logging.WARNING, logger="test-exec-retry"):
        env._ws_exec("ls", None)
    assert not any("exec FAILED" in r.getMessage() for r in caplog.records)


def test_exec_trace_logs_successful_execs(monkeypatch, caplog):
    # AGENT_EVAL_EXEC_TRACE=1 logs every (successful) exec so the full per-step
    # exec sequence can be reconstructed when diagnosing a missing reward.
    env = _env()
    monkeypatch.setenv("AGENT_EVAL_EXEC_TRACE", "1")
    monkeypatch.setattr(env, "_ws_exec_once",
                        lambda c, t: (ExecResult(stdout="ok", stderr="", return_code=0), True, None))
    with caplog.at_level(logging.WARNING, logger="test-exec-retry"):
        env._ws_exec("cd /workspace && ./tests/test.sh", None)
    assert any("exec trace" in r.getMessage() and "test.sh" in r.getMessage()
               for r in caplog.records)


def test_no_trace_for_success_by_default(monkeypatch, caplog):
    env = _env()
    monkeypatch.delenv("AGENT_EVAL_EXEC_TRACE", raising=False)
    monkeypatch.setattr(env, "_ws_exec_once",
                        lambda c, t: (ExecResult(stdout="ok", stderr="", return_code=0), True, None))
    with caplog.at_level(logging.WARNING, logger="test-exec-retry"):
        env._ws_exec("ls", None)
    assert not any("exec trace" in r.getMessage() for r in caplog.records)


def test_sensitive_exec_stderr_is_sanitized(monkeypatch, caplog):
    # stderr is untrusted container output: control chars / ANSI must be escaped
    # and the detail bounded before it reaches the log.
    env = _env()
    payload = "secret\x1b[31m\nLEAKED: token=abc\t" + "x" * 500
    monkeypatch.setattr(env, "_ws_exec_once",
                        lambda c, t: (ExecResult(stdout="", stderr=payload, return_code=1), True, None))
    with caplog.at_level(logging.WARNING, logger="test-exec-retry"):
        env._ws_exec("./test.sh", None)
    detail = next(r.getMessage() for r in caplog.records if "exec FAILED" in r.getMessage())
    assert "\x1b" not in detail and "\n" not in detail.split("detail=")[-1]
    assert "\\x1b" in detail  # escaped form retained
