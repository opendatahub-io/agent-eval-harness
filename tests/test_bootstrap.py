"""Tests for agent_eval._bootstrap venv activation.

Regression coverage for the duplicate-run bug: _bootstrap must NEVER os.execv
when it is reached via `python -m`, `python -c`, or importlib exec_module —
only for a genuine top-level script entry under an ABI-mismatched interpreter.
The default action is sys.path patching, which is re-entrant and side-effect-free.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval import _bootstrap

_REPO = Path(__file__).parent.parent
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
# (a dirname-too-deep bug here silently disables ABI-mismatch detection)
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
# Integration: faithfully reproduce the harbor path in a fresh process —
# deferred first import of _bootstrap via exec_module must NOT os.execv.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not (_REPO / ".eval-venv").is_dir(),
                    reason="no .eval-venv to activate")
def test_exec_module_deferred_import_never_execs(tmp_path):
    loaded = tmp_path / "loaded.py"
    loaded.write_text("import agent_eval._bootstrap\nVALUE = 42\n")

    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent(f"""
        import os, sys, importlib.util
        # Detect any process replacement attempt without performing it.
        os.execv = lambda *a, **k: (_ for _ in ()).throw(
            SystemExit("EXECV_CALLED"))
        # Mimic run.py: import agent_eval WITHOUT _bootstrap, then load a file
        # by path whose first line imports _bootstrap (the deferred first import).
        import agent_eval
        assert "agent_eval._bootstrap" not in sys.modules, "bootstrap leaked early"
        spec = importlib.util.spec_from_file_location("loaded_mod", {str(loaded)!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print("OK", mod.VALUE)
    """))

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO)
    env.pop("_AGENT_EVAL_BOOTSTRAP_DONE", None)
    # Run from a neutral cwd under the SYSTEM python (the bug's trigger).
    proc = subprocess.run([sys.executable, str(child)], cwd=str(tmp_path),
                          env=env, capture_output=True, text=True, timeout=120)
    assert "EXECV_CALLED" not in (proc.stdout + proc.stderr), \
        f"os.execv fired on the exec_module path:\n{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, f"child failed:\n{proc.stdout}\n{proc.stderr}"
    assert "OK 42" in proc.stdout
