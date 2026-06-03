"""Unit tests for discover_configs()."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import DiscoveryResult, discover_configs


def _write_eval(path: Path, skill: str = "test-skill"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"skill: {skill}\n")


def test_nested_layout(tmp_path):
    _write_eval(tmp_path / "eval" / "alpha" / "eval.yaml", "alpha")
    _write_eval(tmp_path / "eval" / "beta" / "eval.yaml", "beta")
    results = discover_configs(tmp_path)
    assert len(results) == 2
    assert results[0].eval_name == "alpha"
    assert results[1].eval_name == "beta"
    assert not results[0].is_root
    assert not results[1].is_root


def test_flat_layout(tmp_path):
    _write_eval(tmp_path / "eval" / "alpha.yaml", "alpha")
    _write_eval(tmp_path / "eval" / "beta.yaml", "beta")
    results = discover_configs(tmp_path)
    assert len(results) == 2
    names = [r.eval_name for r in results]
    assert "alpha" in names
    assert "beta" in names


def test_root_layout(tmp_path):
    _write_eval(tmp_path / "eval.yaml", "my-skill")
    results = discover_configs(tmp_path)
    assert len(results) == 1
    assert results[0].eval_name == "my-skill"
    assert results[0].is_root


def test_mixed_layout(tmp_path):
    _write_eval(tmp_path / "eval.yaml", "root-skill")
    _write_eval(tmp_path / "eval" / "nested" / "eval.yaml", "nested-skill")
    results = discover_configs(tmp_path)
    assert len(results) == 2


def test_empty_project(tmp_path):
    results = discover_configs(tmp_path)
    assert results == []


def test_discovers_configs_without_skill_field(tmp_path):
    """Configs without skill field are discovered (for prompt mode support)."""
    cfg = tmp_path / "eval.yaml"
    cfg.write_text("name: just-a-name\n")
    results = discover_configs(tmp_path)
    assert len(results) == 1
    assert results[0].eval_name == "just-a-name"
    assert results[0].is_root is True


def test_skips_invalid_yaml(tmp_path):
    cfg = tmp_path / "eval.yaml"
    cfg.write_text(": invalid: yaml: {{{\n")
    results = discover_configs(tmp_path)
    assert results == []


def test_deduplicates_same_file(tmp_path):
    _write_eval(tmp_path / "eval.yaml", "root-skill")
    results = discover_configs(tmp_path)
    assert len(results) == 1


def test_nested_eval_yaml_in_eval_dir_excluded(tmp_path):
    """eval/eval.yaml (not inside a subdirectory) is not a flat config."""
    (tmp_path / "eval").mkdir()
    _write_eval(tmp_path / "eval" / "eval.yaml", "should-skip")
    results = discover_configs(tmp_path)
    assert results == []


def test_special_chars_in_eval_name_rejected(tmp_path):
    """Eval names with path separators are rejected."""
    cfg = tmp_path / "eval.yaml"
    cfg.write_text("skill: ../escape\n")
    results = discover_configs(tmp_path)
    assert results == []


def test_control_chars_in_eval_name_rejected(tmp_path):
    cfg = tmp_path / "eval.yaml"
    cfg.write_text("skill: bad\x00name\n")
    results = discover_configs(tmp_path)
    assert results == []


def test_flat_name_collision_warns(tmp_path, capsys):
    """Duplicate eval names across configs produce a warning."""
    _write_eval(tmp_path / "eval" / "a.yaml", "same-name")
    _write_eval(tmp_path / "eval" / "b.yaml", "same-name")
    results = discover_configs(tmp_path)
    assert len(results) == 2
    captured = capsys.readouterr()
    assert "duplicate eval name" in captured.err


def test_backslash_in_eval_name_rejected(tmp_path):
    cfg = tmp_path / "eval.yaml"
    cfg.write_text("skill: 'bad\\\\name'\n")
    results = discover_configs(tmp_path)
    assert results == []


def test_dotdot_eval_name_rejected(tmp_path):
    cfg = tmp_path / "eval.yaml"
    cfg.write_text("skill: ..\n")
    results = discover_configs(tmp_path)
    assert results == []


def test_dot_eval_name_rejected(tmp_path):
    cfg = tmp_path / "eval.yaml"
    cfg.write_text("skill: .\n")
    results = discover_configs(tmp_path)
    assert results == []
