"""Tests for dataset.workspace.files support."""

import os

import pytest

from agent_eval.config import DatasetConfig, EvalConfig, WorkspaceConfig
from workspace_files import _copy_input_files


def _write(tmp_path, body):
    p = tmp_path / "eval.yaml"
    p.write_text(body)
    return p


# ── Config parsing ──────────────────────────────────────────────────


def test_workspace_files_defaults_to_empty(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: t\nskill: s\n"))
    assert cfg.dataset.workspace.files == []


def test_workspace_files_parsed(tmp_path):
    cfg = EvalConfig.from_yaml(
        _write(
            tmp_path,
            """
name: t
skill: s
dataset:
  workspace:
    files:
      - src/
      - tickets/JIRA-123.md
      - config/settings.json
""",
        )
    )
    assert cfg.dataset.workspace.files == [
        "src",
        "tickets/JIRA-123.md",
        "config/settings.json",
    ]


def test_workspace_files_rejects_absolute_path(tmp_path):
    with pytest.raises(ValueError, match="must be a relative path"):
        EvalConfig.from_yaml(
            _write(
                tmp_path,
                """
name: t
skill: s
dataset:
  workspace:
    files:
      - /etc/passwd
""",
            )
        )


def test_workspace_files_rejects_non_string_entry(tmp_path):
    with pytest.raises(ValueError, match="must be a string"):
        EvalConfig.from_yaml(
            _write(
                tmp_path,
                """\
name: t
skill: s
dataset:
  workspace:
    files:
      - 42
""",
            )
        )


def test_workspace_files_rejects_parent_traversal(tmp_path):
    with pytest.raises(ValueError, match="must not contain '\\.\\.'"):
        EvalConfig.from_yaml(
            _write(
                tmp_path,
                """
name: t
skill: s
dataset:
  workspace:
    files:
      - ../secrets
""",
            )
        )


def test_dataset_config_grouped(tmp_path):
    """dataset.path and dataset.schema are accessible via DatasetConfig."""
    cfg = EvalConfig.from_yaml(
        _write(
            tmp_path,
            """
name: t
skill: s
dataset:
  path: cases
  schema: "Each case has a ticket and code."
""",
        )
    )
    assert cfg.dataset.path == "cases"
    assert cfg.dataset.schema == "Each case has a ticket and code."


# ── File copying ────────────────────────────────────────────────────


def test_copy_workspace_files_directory(tmp_path):
    """Directory entries copy the full subtree."""
    case_dir = tmp_path / "cases" / "case-001"
    (case_dir / "src").mkdir(parents=True)
    (case_dir / "src" / "main.py").write_text("print('hello')")
    (case_dir / "src" / "lib.py").write_text("x = 1")
    (case_dir / "annotations.yaml").write_text("expected: pass")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(workspace=WorkspaceConfig(files=["src"])),
    )
    _copy_input_files(case_dir, workspace, config)

    assert (workspace / "src" / "main.py").read_text() == "print('hello')"
    assert (workspace / "src" / "lib.py").read_text() == "x = 1"
    assert not (workspace / "annotations.yaml").exists()


def test_copy_workspace_files_single_file(tmp_path):
    """File entries copy only the named file."""
    case_dir = tmp_path / "cases" / "case-001"
    (case_dir / "config").mkdir(parents=True)
    (case_dir / "config" / "settings.json").write_text('{"a":1}')
    (case_dir / "config" / "secrets.json").write_text('{"key":"x"}')

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(
            workspace=WorkspaceConfig(files=["config/settings.json"]),
        ),
    )
    _copy_input_files(case_dir, workspace, config)

    assert (workspace / "config" / "settings.json").read_text() == '{"a":1}'
    assert not (workspace / "config" / "secrets.json").exists()


def test_copy_workspace_files_noop_when_empty(tmp_path):
    """No error when workspace.files is empty."""
    case_dir = tmp_path / "cases" / "case-001"
    case_dir.mkdir(parents=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(name="t", skill="s")
    _copy_input_files(case_dir, workspace, config)

    assert list(workspace.iterdir()) == []


def test_copy_workspace_files_noop_when_path_missing(tmp_path):
    """No error when a listed path doesn't exist in the case directory."""
    case_dir = tmp_path / "cases" / "case-001"
    case_dir.mkdir(parents=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(
            workspace=WorkspaceConfig(files=["nonexistent/"]),
        ),
    )
    _copy_input_files(case_dir, workspace, config)

    assert list(workspace.iterdir()) == []


def test_copy_workspace_files_skips_symlinks(tmp_path):
    """Symlinks in workspace file entries are not followed."""
    case_dir = tmp_path / "cases" / "case-001"
    (case_dir / "src").mkdir(parents=True)
    (case_dir / "src" / "real.py").write_text("real")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")

    os.symlink(outside / "secret.txt", case_dir / "src" / "link.txt")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(workspace=WorkspaceConfig(files=["src"])),
    )
    _copy_input_files(case_dir, workspace, config)

    assert (workspace / "src" / "real.py").read_text() == "real"
    assert not os.path.lexists(workspace / "src" / "link.txt")


def test_copy_workspace_files_skips_symlinked_entry(tmp_path):
    """A top-level symlinked directory entry is skipped entirely."""
    case_dir = tmp_path / "cases" / "case-001"
    case_dir.mkdir(parents=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")

    os.symlink(outside, case_dir / "evil")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(workspace=WorkspaceConfig(files=["evil"])),
    )
    _copy_input_files(case_dir, workspace, config)

    assert not os.path.lexists(workspace / "evil")
    assert list(workspace.iterdir()) == []
