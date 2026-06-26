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
