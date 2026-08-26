"""Tests for dataset.workspace.files support."""

import os

import pytest

from agent_eval.config import DatasetConfig, EvalConfig, RunnerConfig, WorkspaceConfig
from workspace_files import _copy_input_files


def _write(tmp_path, body):
    p = tmp_path / "eval.yaml"
    p.write_text(body)
    return p


# ── Config parsing ──────────────────────────────────────────────────


def test_workspace_files_defaults_to_empty(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: t\nexecution:\n  skill: s\n"))
    assert cfg.dataset.workspace.files == []


def test_workspace_files_parsed(tmp_path):
    cfg = EvalConfig.from_yaml(
        _write(
            tmp_path,
            """
name: t
execution:
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
execution:
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
execution:
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
execution:
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
execution:
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


def test_copy_workspace_files_skips_nested_symlinks(tmp_path, monkeypatch, capsys):
    """Nested (unlisted) symlinks inside a copied directory are skipped."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    monkeypatch.chdir(project)

    case_dir = project / "cases" / "case-001"
    (case_dir / "src").mkdir(parents=True)
    (case_dir / "src" / "real.py").write_text("real")
    os.symlink(outside / "secret.txt", case_dir / "src" / "link.txt")
    (project / "live.md").write_text("live")
    (case_dir / "src" / "nested.md").symlink_to(project / "live.md")

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
    assert not os.path.lexists(workspace / "src" / "nested.md")
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "link.txt" in err
    assert "nested.md" in err


def test_copy_workspace_files_skips_sibling_case_symlink(
    tmp_path, monkeypatch, capsys,
):
    """A listed symlink to another case's answers.yaml is not copied."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    case_002 = project / "cases" / "case-002"
    case_002.mkdir(parents=True)
    (case_002 / "answers.yaml").write_text("gold: secret\n")

    case_dir = project / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    (case_dir / "peek.yaml").symlink_to("../case-002/answers.yaml")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(workspace=WorkspaceConfig(files=["peek.yaml"])),
    )
    _copy_input_files(case_dir, workspace, config)

    assert not os.path.lexists(workspace / "peek.yaml")
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "peek.yaml" in err


def test_copy_workspace_files_skips_listed_dir_symlink(
    tmp_path, monkeypatch, capsys,
):
    """A listed directory symlink is not walked (would copy the whole repo)."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "secret.env").write_text("noleak")
    monkeypatch.chdir(project)

    case_dir = project / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    (case_dir / "peek").symlink_to(project)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(workspace=WorkspaceConfig(files=["peek"])),
    )
    _copy_input_files(case_dir, workspace, config)

    assert not os.path.lexists(workspace / "peek")
    assert list(workspace.iterdir()) == []
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "peek" in err


def test_copy_workspace_files_skips_external_symlinked_entry(
    tmp_path, monkeypatch, capsys,
):
    """A top-level symlink escaping the project is skipped, with a warning."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    monkeypatch.chdir(project)

    case_dir = project / "cases" / "case-001"
    case_dir.mkdir(parents=True)
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
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "evil" in err


def test_copy_workspace_files_materializes_internal_symlink(tmp_path, monkeypatch):
    """A case symlink to a live SKILL.md under the project is copied as a file."""
    project = tmp_path / "project"
    skill = project / "skills" / "address-ci-failures"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# live skill\n")
    monkeypatch.chdir(project)

    case_dir = project / "eval" / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    (case_dir / "triage-skill.md").symlink_to(
        "../../../skills/address-ci-failures/SKILL.md"
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(
            workspace=WorkspaceConfig(files=["triage-skill.md"]),
        ),
    )
    _copy_input_files(case_dir, workspace, config)

    dest = workspace / "triage-skill.md"
    assert dest.is_file()
    assert not dest.is_symlink()
    assert dest.read_text() == "# live skill\n"


def test_copy_workspace_files_copies_file_resolved_outside_case(
    tmp_path, monkeypatch,
):
    """A listed file may resolve outside the case if it stays in the project."""
    project = tmp_path / "project"
    live = project / "skills" / "foo"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text("live")
    monkeypatch.chdir(project)

    case_dir = project / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    (case_dir / "docs").symlink_to(live)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(
            workspace=WorkspaceConfig(files=["docs/SKILL.md"]),
        ),
    )
    _copy_input_files(case_dir, workspace, config)

    dest = workspace / "docs" / "SKILL.md"
    assert dest.is_file()
    assert not dest.is_symlink()
    assert dest.read_text() == "live"


def test_copy_workspace_files_materializes_plugin_symlink(
    tmp_path, monkeypatch,
):
    """A symlink into a configured plugin dir is materialized even off-project."""
    consumer = tmp_path / "consumer"
    plugin = tmp_path / "plugin"
    consumer.mkdir()
    skill = plugin / "skills" / "address-ci-failures"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# from plugin\n")
    monkeypatch.chdir(consumer)

    case_dir = consumer / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    (case_dir / "triage-skill.md").symlink_to(skill / "SKILL.md")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        runner=RunnerConfig(plugin_dirs=[str(plugin)]),
        dataset=DatasetConfig(
            workspace=WorkspaceConfig(files=["triage-skill.md"]),
        ),
    )
    _copy_input_files(case_dir, workspace, config)

    dest = workspace / "triage-skill.md"
    assert dest.is_file()
    assert not dest.is_symlink()
    assert dest.read_text() == "# from plugin\n"


def test_copy_workspace_files_plugin_symlink_rejected_without_plugin_dirs(
    tmp_path, monkeypatch, capsys,
):
    """The same plugin symlink is skipped when plugin_dirs is not configured."""
    consumer = tmp_path / "consumer"
    plugin = tmp_path / "plugin"
    consumer.mkdir()
    skill = plugin / "skills" / "address-ci-failures"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# from plugin\n")
    monkeypatch.chdir(consumer)

    case_dir = consumer / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    (case_dir / "triage-skill.md").symlink_to(skill / "SKILL.md")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(
            workspace=WorkspaceConfig(files=["triage-skill.md"]),
        ),
    )
    _copy_input_files(case_dir, workspace, config)

    assert not os.path.lexists(workspace / "triage-skill.md")
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "triage-skill.md" in err


def test_copy_workspace_files_warns_on_dangling_symlink(
    tmp_path, monkeypatch, capsys,
):
    """A dangling symlink is skipped with a warning rather than silently."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    case_dir = project / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    (case_dir / "gone.md").symlink_to("missing.md")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(workspace=WorkspaceConfig(files=["gone.md"])),
    )
    _copy_input_files(case_dir, workspace, config)

    assert list(workspace.iterdir()) == []
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "gone.md" in err


def test_copy_workspace_files_skips_listed_dir_symlink_inside_project(
    tmp_path, monkeypatch, capsys,
):
    """A contained directory symlink is skipped; only listed file links copy."""
    project = tmp_path / "project"
    real = project / "lib"
    real.mkdir(parents=True)
    (real / "util.py").write_text("x")
    monkeypatch.chdir(project)

    case_dir = project / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    (case_dir / "lib").symlink_to(real)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(workspace=WorkspaceConfig(files=["lib"])),
    )
    _copy_input_files(case_dir, workspace, config)

    assert not os.path.lexists(workspace / "lib")
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "lib" in err


def test_copy_workspace_files_rejects_lexical_escape(
    tmp_path, monkeypatch, capsys,
):
    """A listed path with .. must not write outside the workspace."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    leaked = project / "cases" / "leaked.md"
    leaked.parent.mkdir(parents=True)
    leaked.write_text("leaked")

    case_dir = project / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = EvalConfig(
        name="t",
        skill="s",
        dataset=DatasetConfig(
            workspace=WorkspaceConfig(files=["../leaked.md"]),
        ),
    )
    _copy_input_files(case_dir, workspace, config)

    assert list(workspace.iterdir()) == []
    assert not (tmp_path / "leaked.md").exists()
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "leaked.md" in err
