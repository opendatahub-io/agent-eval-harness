"""Unit tests for per-eval run isolation (US4).

Verifies that _get_runs_dir() appends the eval name to the base
runs directory, producing $AGENT_EVAL_RUNS_DIR/<eval-name>/.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"))

from score import _get_runs_dir


def test_runs_dir_includes_eval_name(monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", "eval/runs")
    result = _get_runs_dir("my-skill")
    assert result == Path("eval/runs/my-skill")


def test_runs_dir_without_eval_name(monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", "eval/runs")
    result = _get_runs_dir()
    assert result == Path("eval/runs")


def test_runs_dir_custom_base(monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", "/tmp/custom-runs")
    result = _get_runs_dir("alpha")
    assert result == Path("/tmp/custom-runs/alpha")


def test_runs_dir_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("AGENT_EVAL_RUNS_DIR", raising=False)
    result = _get_runs_dir("alpha")
    assert result == Path("eval/runs/alpha")


def test_runs_dir_rejects_path_traversal(monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", "eval/runs")
    with pytest.raises(ValueError):
        _get_runs_dir("../escape")


def test_runs_dir_rejects_path_separator(monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", "eval/runs")
    with pytest.raises(ValueError):
        _get_runs_dir("foo/bar")
