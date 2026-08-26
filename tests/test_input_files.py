"""Tests for dataset.workspace.files support."""

import os

import pytest

from agent_eval.config import (
    DatasetConfig,
    EvalConfig,
    RunnerConfig,
    WorkspaceConfig,
    WorkspaceFile,
)
from agent_eval.workspace_provisioning import materialize_shared_files
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


# ── Config parsing: {dest, source} shared entries ───────────────────


def test_workspace_files_accepts_dict_entry(tmp_path):
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
      - triage-skill.md
      - dest: skill.md
        source: plugins/p/skills/x/SKILL.md
""",
        )
    )
    assert cfg.dataset.workspace.files == [
        "triage-skill.md",
        WorkspaceFile(dest="skill.md", source="plugins/p/skills/x/SKILL.md"),
    ]


def test_workspace_files_dict_missing_source_rejected(tmp_path):
    with pytest.raises(ValueError, match=r"missing required key.*source"):
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
      - dest: skill.md
""",
            )
        )


def test_workspace_files_dict_unknown_key_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown key"):
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
      - dest: skill.md
        source: x
        mode: "0644"
""",
            )
        )


def test_workspace_files_dict_dest_traversal_rejected(tmp_path):
    with pytest.raises(ValueError, match=r"must not contain '\.\.'"):
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
      - dest: ../escape.md
        source: x
""",
            )
        )


def test_workspace_files_dict_absolute_dest_rejected(tmp_path):
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
      - dest: /etc/passwd
        source: x
""",
            )
        )


def test_workspace_files_dict_dot_dest_rejected(tmp_path):
    """dest '.' would materialize over the workspace root — must be rejected."""
    with pytest.raises(ValueError, match=r"cannot be '\.'"):
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
      - dest: .
        source: x
""",
            )
        )


# ── Local materialization of shared {dest, source} entries ──────────


def _shared_config(files, plugin_dirs=None):
    return EvalConfig(
        name="t",
        skill="s",
        runner=RunnerConfig(plugin_dirs=plugin_dirs or []),
        dataset=DatasetConfig(workspace=WorkspaceConfig(files=files)),
    )


def test_materialize_shared_file_copies_in_project_source(tmp_path, monkeypatch):
    """A source under the project root is copied to dest as a real file."""
    project = tmp_path / "project"
    skill = project / "skills" / "x"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# live skill\n")
    monkeypatch.chdir(project)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _shared_config(
        [WorkspaceFile(dest="triage-skill.md", source="skills/x/SKILL.md")]
    )
    materialize_shared_files(workspace, config)

    dest = workspace / "triage-skill.md"
    assert dest.is_file() and not dest.is_symlink()
    assert dest.read_text() == "# live skill\n"


def test_materialize_shared_file_follows_source_symlink(tmp_path, monkeypatch):
    """A source that is itself a symlink (inside the project) is materialized."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "real.md").write_text("real")
    os.symlink(project / "real.md", project / "link.md")
    monkeypatch.chdir(project)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _shared_config([WorkspaceFile(dest="skill.md", source="link.md")])
    materialize_shared_files(workspace, config)

    dest = workspace / "skill.md"
    assert dest.is_file() and not dest.is_symlink()
    assert dest.read_text() == "real"


def test_materialize_shared_file_via_plugin_dir(tmp_path, monkeypatch):
    """An absolute source inside a configured plugin_dir is allowed."""
    consumer = tmp_path / "consumer"
    plugin = tmp_path / "plugin"
    consumer.mkdir()
    skill = plugin / "skills" / "x"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# from plugin\n")
    monkeypatch.chdir(consumer)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _shared_config(
        [WorkspaceFile(dest="skill.md", source=str(skill / "SKILL.md"))],
        plugin_dirs=[str(plugin)],
    )
    materialize_shared_files(workspace, config)

    assert (workspace / "skill.md").read_text() == "# from plugin\n"


def test_materialize_shared_file_skips_source_outside_roots(
    tmp_path, monkeypatch, capsys
):
    """A source outside the project/plugin roots is skipped with a warning."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    monkeypatch.chdir(project)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _shared_config(
        [WorkspaceFile(dest="leak.txt", source=str(outside / "secret.txt"))]
    )
    materialize_shared_files(workspace, config)

    assert not (workspace / "leak.txt").exists()
    err = capsys.readouterr().err
    assert "WARNING" in err and "leak.txt" in err


def test_materialize_shared_file_skips_missing_source(tmp_path, monkeypatch, capsys):
    """A missing source is skipped with a warning, not an error."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _shared_config([WorkspaceFile(dest="skill.md", source="nope.md")])
    materialize_shared_files(workspace, config)

    assert list(workspace.iterdir()) == []
    assert "WARNING" in capsys.readouterr().err


def test_materialize_shared_file_skips_reserved_dest(tmp_path, monkeypatch, capsys):
    """A dest colliding with a harness-reserved name is skipped with a warning."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "evil.yaml").write_text("gotcha: true")
    monkeypatch.chdir(project)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "answers.yaml").write_text("gold: 42")
    config = _shared_config(
        [WorkspaceFile(dest="answers.yaml", source="evil.yaml")]
    )
    materialize_shared_files(workspace, config)

    assert (workspace / "answers.yaml").read_text() == "gold: 42"
    err = capsys.readouterr().err
    assert "WARNING" in err and "reserved" in err


def test_materialize_shared_directory_drops_nested_symlinks(tmp_path, monkeypatch):
    """A directory source copies regular files and drops nested symlinks."""
    project = tmp_path / "project"
    lib = project / "lib"
    lib.mkdir(parents=True)
    (lib / "util.py").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside / "secret.txt", lib / "link.txt")
    monkeypatch.chdir(project)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _shared_config([WorkspaceFile(dest="lib", source="lib")])
    materialize_shared_files(workspace, config)

    assert (workspace / "lib" / "util.py").read_text() == "x"
    assert not os.path.lexists(workspace / "lib" / "link.txt")


def test_copy_input_files_ignores_dict_entries(tmp_path):
    """_copy_input_files handles string entries and skips WorkspaceFile ones."""
    case_dir = tmp_path / "cases" / "case-001"
    case_dir.mkdir(parents=True)
    (case_dir / "real.txt").write_text("real")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _shared_config(
        ["real.txt", WorkspaceFile(dest="skill.md", source="x")]
    )
    _copy_input_files(case_dir, workspace, config)

    assert (workspace / "real.txt").read_text() == "real"
    assert not (workspace / "skill.md").exists()


def test_workspace_files_dict_slash_dest_rejected(tmp_path):
    """dest '/' strips to '' and must not slip past the root guard."""
    with pytest.raises(ValueError, match=r"cannot be '\.'"):
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
      - dest: /
        source: x
""",
            )
        )


def test_materialize_shared_file_skips_empty_dest(tmp_path, monkeypatch, capsys):
    """A programmatic WorkspaceFile with an empty dest can't clobber the root."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "evil.txt").write_text("evil")
    monkeypatch.chdir(project)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.yaml").write_text("prompt: keep")
    # bypasses config-load validation (dest="" would be rejected there)
    config = _shared_config([WorkspaceFile(dest="", source="evil.txt")])
    materialize_shared_files(workspace, config)

    assert (workspace / "input.yaml").read_text() == "prompt: keep"
    assert not (workspace / "evil.txt").exists()
    assert "WARNING" in capsys.readouterr().err


def test_materialize_shared_file_skips_recursive_dest(tmp_path, monkeypatch, capsys):
    """A dest inside a directory source must not trigger a recursive copytree."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "keep.txt").write_text("keep")
    monkeypatch.chdir(project)

    # target_dir is UNDER the project root (like an S3 export staging dir), and
    # source '.' resolves to the project root — dst would be inside the source.
    target_dir = project / "export" / "case-001"
    target_dir.mkdir(parents=True)
    config = _shared_config([WorkspaceFile(dest="sub", source=".")])
    materialize_shared_files(target_dir, config)

    assert not (target_dir / "sub").exists()
    err = capsys.readouterr().err
    assert "WARNING" in err and "recursive" in err


def test_materialize_shared_file_tolerates_symlink_loop_plugin_dir(
    tmp_path, monkeypatch
):
    """A symlink-loop plugin_dir is dropped, not fatal; a valid source still copies."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "SKILL.md").write_text("# live\n")
    loop = project / "loop"
    os.symlink(loop, loop)  # self-referential symlink -> resolve() may RuntimeError
    monkeypatch.chdir(project)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _shared_config(
        [WorkspaceFile(dest="skill.md", source="SKILL.md")],
        plugin_dirs=[str(loop)],
    )
    materialize_shared_files(workspace, config)  # must not raise

    assert (workspace / "skill.md").read_text() == "# live\n"
