"""Tests for agent_eval._bootstrap venv activation.

Regression coverage for the duplicate-run bug: _bootstrap must NEVER os.execv
when it is reached via `python -m`, `python -c`, `python -`, the REPL, or
importlib exec_module — only for a genuine top-level script entry under an
ABI-mismatched interpreter. The default action is sys.path patching, which is
re-entrant and side-effect-free.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval import _bootstrap

_RUNNING = f"{sys.version_info.major}.{sys.version_info.minor}"


@pytest.fixture
def clean_sentinel(monkeypatch):
    monkeypatch.delenv(_bootstrap._SENTINEL, raising=False)
    yield


def _set_main_spec(monkeypatch, spec):
    main = sys.modules["__main__"]
    monkeypatch.setattr(main, "__spec__", spec, raising=False)


# ---------------------------------------------------------------------------
# _venv_pyver — guards the '.../lib/pythonX.Y/site-packages' parse
# ---------------------------------------------------------------------------

def test_venv_pyver_extracts_version():
    site = "/x/.eval-venv/lib/python3.11/site-packages"
    assert _bootstrap._venv_pyver([site]) == "3.11"


def test_venv_pyver_none_on_nonstandard_layout():
    assert _bootstrap._venv_pyver(["/x/.eval-venv/weird/site-packages"]) is None


# ---------------------------------------------------------------------------
# _is_true_script_entry — the execv discriminator
# ---------------------------------------------------------------------------

def test_script_entry_true_for_plain_script(monkeypatch):
    _set_main_spec(monkeypatch, None)
    monkeypatch.setattr(sys, "argv", ["/some/script.py", "--flag"])
    assert _bootstrap._is_true_script_entry() is True


def test_script_entry_false_for_dash_c(monkeypatch):
    _set_main_spec(monkeypatch, None)
    monkeypatch.setattr(sys, "argv", ["-c"])
    assert _bootstrap._is_true_script_entry() is False


def test_script_entry_false_for_stdin_repl_empty(monkeypatch):
    # `python -` (stdin), REPL/embedded (argv[0] == ''), and an empty argv have
    # no real script file to re-exec, so they must not take the execv path.
    _set_main_spec(monkeypatch, None)
    for argv in (["-"], [""], []):
        monkeypatch.setattr(sys, "argv", argv)
        assert _bootstrap._is_true_script_entry() is False, argv


def test_script_entry_false_for_dash_m(monkeypatch):
    # `python -m pkg.mod` leaves a non-None ModuleSpec on __main__.
    import importlib.machinery
    _set_main_spec(monkeypatch, importlib.machinery.ModuleSpec("pkg.mod", None))
    monkeypatch.setattr(sys, "argv", ["/abs/path/mod.py"])
    assert _bootstrap._is_true_script_entry() is False


# ---------------------------------------------------------------------------
# _activate — never execv on -m / -c; execv only on script + ABI mismatch
# ---------------------------------------------------------------------------

def _arm(monkeypatch, venv_ver):
    """Make _activate see a venv whose site-packages report python<venv_ver>."""
    site = f"/fake/.eval-venv/lib/python{venv_ver}/site-packages"
    monkeypatch.setattr(_bootstrap, "_venv_python_and_site",
                        lambda root: ("/fake/.eval-venv/bin/python3", [site]))
    execv_calls, patch_calls = [], []
    monkeypatch.setattr(os, "execv", lambda *a: execv_calls.append(a))
    monkeypatch.setattr(_bootstrap, "_patch_syspath", lambda s: patch_calls.append(s))
    return execv_calls, patch_calls


def test_activate_abi_match_patches_no_execv(clean_sentinel, monkeypatch):
    execv_calls, patch_calls = _arm(monkeypatch, _RUNNING)
    _set_main_spec(monkeypatch, None)
    monkeypatch.setattr(sys, "argv", ["/some/script.py"])
    _bootstrap._activate()
    assert execv_calls == []
    assert patch_calls and os.environ.get(_bootstrap._SENTINEL) == "1"


def test_activate_abi_mismatch_script_execs(clean_sentinel, monkeypatch):
    execv_calls, patch_calls = _arm(monkeypatch, "2.0")  # != running
    _set_main_spec(monkeypatch, None)
    monkeypatch.setattr(sys, "argv", ["/some/script.py"])
    _bootstrap._activate()
    assert len(execv_calls) == 1
    assert execv_calls[0][0] == "/fake/.eval-venv/bin/python3"


def test_activate_abi_mismatch_dash_m_does_not_exec(clean_sentinel, monkeypatch):
    import importlib.machinery
    execv_calls, patch_calls = _arm(monkeypatch, "2.0")  # mismatch, but -m
    _set_main_spec(monkeypatch, importlib.machinery.ModuleSpec("agent_eval.harbor.run", None))
    monkeypatch.setattr(sys, "argv", ["/abs/run.py", "--config", "x"])
    _bootstrap._activate()
    assert execv_calls == []           # the duplicate-run bug must not reappear
    assert patch_calls                  # falls back to sys.path patch


def test_activate_sentinel_short_circuits(clean_sentinel, monkeypatch):
    monkeypatch.setenv(_bootstrap._SENTINEL, "1")
    called = []
    monkeypatch.setattr(_bootstrap, "_venv_python_and_site",
                        lambda root: called.append(1) or (None, []))
    _bootstrap._activate()
    assert called == []  # returned before touching anything


def test_activate_no_venv_is_noop(clean_sentinel, monkeypatch):
    monkeypatch.setattr(_bootstrap, "_venv_python_and_site", lambda root: (None, []))
    execv_calls = []
    monkeypatch.setattr(os, "execv", lambda *a: execv_calls.append(a))
    _bootstrap._activate()
    assert execv_calls == []
    assert os.environ.get(_bootstrap._SENTINEL) == "1"


# ---------------------------------------------------------------------------
# Integration: a fresh process with a *fake ABI-mismatched* .eval-venv, so the
# mismatch branch is exercised regardless of the test runner's interpreter
# (no skip, never vacuous). The `-m` case faithfully reproduces how the harbor
# runner is launched; the true-script case is a positive control proving the
# mismatch branch is actually reachable in this harness.
# ---------------------------------------------------------------------------

# Spy installed before any agent_eval import: records an execv attempt to a
# marker file instead of replacing the process.
_EXECV_SPY = textwrap.dedent("""
    import os
    def _spy(*a, **k):
        with open(os.environ["EXECV_MARKER"], "w") as f:
            f.write("EXECV")
        raise SystemExit("execv-attempted")
    os.execv = _spy
""")


def _make_fake_plugin(tmp_path, venv_pyver="0.0"):
    """Throwaway plugin root: a copy of _bootstrap plus a fake .eval-venv whose
    python<venv_pyver> never matches the runner -> always the ABI-mismatch path."""
    root = tmp_path / "plugin"
    pkg = root / "agent_eval"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")  # no _bootstrap import (mirrors real __init__)
    shutil.copy(_bootstrap.__file__, pkg / "_bootstrap.py")
    venv = root / ".eval-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("")  # only needs to be a regular file
    (venv / "lib" / f"python{venv_pyver}" / "site-packages").mkdir(parents=True)
    return root


def _run_child(root, code, module=None):
    marker = root / "execv.marker"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)          # isolate: only the fake agent_eval
    env["EXECV_MARKER"] = str(marker)
    env.pop("_AGENT_EVAL_BOOTSTRAP_DONE", None)
    if module:
        (root / f"{module}.py").write_text(code)
        cmd = [sys.executable, "-m", module]
    else:
        script = root / "child_script.py"
        script.write_text(code)
        cmd = [sys.executable, str(script)]
    proc = subprocess.run(cmd, cwd=str(root), env=env,
                          capture_output=True, text=True, timeout=120)
    return proc, marker.exists()


def test_dash_m_under_abi_mismatch_does_not_exec(tmp_path):
    # `python -m childmod` -> __main__.__spec__ non-None (like the harbor runner).
    # The deferred exec_module import of _bootstrap must NOT execv even though the
    # fake venv is ABI-mismatched.
    root = _make_fake_plugin(tmp_path)
    (root / "loaded.py").write_text("import agent_eval._bootstrap\nVALUE = 42\n")
    code = _EXECV_SPY + textwrap.dedent("""
        import sys, os, importlib.util
        import agent_eval
        assert "agent_eval._bootstrap" not in sys.modules, "bootstrap leaked early"
        p = os.path.join(os.path.dirname(__file__), "loaded.py")
        spec = importlib.util.spec_from_file_location("loaded_mod", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        print("OK", m.VALUE)
    """)
    proc, execd = _run_child(root, code, module="childmod")
    assert not execd, f"execv fired on the -m path:\n{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0 and "OK 42" in proc.stdout, \
        f"child failed:\n{proc.stdout}\n{proc.stderr}"


def test_true_script_under_abi_mismatch_execs(tmp_path):
    # Positive control: a real top-level script under the SAME fake ABI-mismatch
    # venv DOES take the execv path — so the -m test above is not passing vacuously.
    root = _make_fake_plugin(tmp_path)
    code = _EXECV_SPY + "import agent_eval._bootstrap\nprint('NO EXEC')\n"
    proc, execd = _run_child(root, code)  # plain `python child_script.py`
    assert execd, f"expected execv on true-script ABI mismatch:\n{proc.stdout}\n{proc.stderr}"
