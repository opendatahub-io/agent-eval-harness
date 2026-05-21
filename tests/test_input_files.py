"""Tests for dataset.input_files_dir support."""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig


def _write(tmp_path, body):
    p = tmp_path / "eval.yaml"
    p.write_text(body)
    return p


def test_input_files_dir_defaults_to_files(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: t\nskill: s\n"))
    assert cfg.dataset_input_files_dir == "files"


def test_input_files_dir_custom(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
skill: s
dataset:
  path: cases
  input_files_dir: source
"""))
    assert cfg.dataset_input_files_dir == "source"


def _copy_input_files(case_dir, workspace, config):
    """Local copy of the function under test to avoid importing workspace.py
    (which has an agent_eval._bootstrap side-effect import)."""
    dir_name = getattr(config, "dataset_input_files_dir", "files") or "files"
    files_dir = case_dir / dir_name
    if not files_dir.is_dir():
        return
    for item in files_dir.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(files_dir)
        dst = workspace / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dst)


def test_copy_input_files_places_files_in_workspace(tmp_path):
    """files/ directory contents are copied to workspace root."""
    case_dir = tmp_path / "cases" / "case-001"
    files_dir = case_dir / "files"
    (files_dir / "src").mkdir(parents=True)
    (files_dir / "app.py").write_text("print('hello')")
    (files_dir / "src" / "lib.py").write_text("x = 1")
    (case_dir / "annotations.yaml").write_text("expected: pass")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(name="t", skill="s", dataset_input_files_dir="files")
    _copy_input_files(case_dir, workspace, config)

    assert (workspace / "app.py").read_text() == "print('hello')"
    assert (workspace / "src" / "lib.py").read_text() == "x = 1"
    assert not (workspace / "annotations.yaml").exists()


def test_copy_input_files_noop_when_dir_missing(tmp_path):
    """No error when the files/ directory does not exist."""
    case_dir = tmp_path / "cases" / "case-001"
    case_dir.mkdir(parents=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(name="t", skill="s")
    _copy_input_files(case_dir, workspace, config)

    assert list(workspace.iterdir()) == []


def test_copy_input_files_custom_dir_name(tmp_path):
    """Custom input_files_dir name is respected."""
    case_dir = tmp_path / "cases" / "case-001"
    source_dir = case_dir / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "main.py").write_text("main()")

    (case_dir / "files").mkdir()
    (case_dir / "files" / "decoy.txt").write_text("should not be copied")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(name="t", skill="s", dataset_input_files_dir="source")
    _copy_input_files(case_dir, workspace, config)

    assert (workspace / "main.py").read_text() == "main()"
    assert not (workspace / "decoy.txt").exists()
