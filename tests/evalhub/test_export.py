"""Tests for the S3/EvalHub dataset export step (agent_eval.evalhub.export)."""

import os
from unittest.mock import MagicMock

import yaml

from agent_eval.config import EvalConfig
from agent_eval.evalhub.export import export_dataset


def _make_project(tmp_path, files):
    """Build a project checkout: eval.yaml + two cases + a plugin source.

    ``files`` is the raw ``dataset.workspace.files`` list. Returns the loaded
    EvalConfig (config_dir == tmp_path so dataset.path resolves there).
    """
    cases = tmp_path / "cases"
    for cid in ("case-001", "case-002"):
        (cases / cid).mkdir(parents=True)
        (cases / cid / "input.yaml").write_text(
            yaml.safe_dump({"prompt": f"do {cid}"})
        )
        (cases / cid / "annotations.yaml").write_text("expected: pass\n")
    skill = tmp_path / "plugins" / "x" / "skills" / "triage"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# live triage skill\n")

    raw = {
        "name": "t",
        "execution": {"prompt": "{prompt}"},
        "dataset": {"path": "cases", "schema": "x", "workspace": {"files": files}},
    }
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return EvalConfig.from_yaml(cfg_path)


def test_export_materializes_shared_file_into_every_case(tmp_path, monkeypatch):
    config = _make_project(
        tmp_path,
        [{"dest": "triage-skill.md", "source": "plugins/x/skills/triage/SKILL.md"}],
    )
    monkeypatch.chdir(tmp_path)  # project_root == cwd for source resolution
    out = tmp_path / "export"

    info = export_dataset(config, out)

    assert info.num_cases == 2
    assert info.case_ids == ["case-001", "case-002"]
    for cid in ("case-001", "case-002"):
        dest = out / cid / "triage-skill.md"
        assert dest.is_file() and not dest.is_symlink()
        assert dest.read_text() == "# live triage skill\n"
        # case files come across as real files
        assert (out / cid / "input.yaml").is_file()
        assert (out / cid / "annotations.yaml").is_file()


def test_export_drops_symlinks_in_case_dir(tmp_path, monkeypatch):
    config = _make_project(tmp_path, [])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside / "secret.txt", tmp_path / "cases" / "case-001" / "link.txt")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "export"

    export_dataset(config, out)

    assert (out / "case-001" / "input.yaml").is_file()
    assert not os.path.lexists(out / "case-001" / "link.txt")


def test_export_skips_out_of_bounds_source(tmp_path, monkeypatch, capsys):
    config = _make_project(tmp_path, [{"dest": "leak.txt", "source": "/etc/hostname"}])
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "export"

    export_dataset(config, out)  # must not raise

    assert not (out / "case-001" / "leak.txt").exists()
    assert "WARNING" in capsys.readouterr().err


def test_export_follows_source_symlink(tmp_path, monkeypatch):
    config = _make_project(
        tmp_path, [{"dest": "skill.md", "source": "link.md"}]
    )
    (tmp_path / "real.md").write_text("real")
    os.symlink(tmp_path / "real.md", tmp_path / "link.md")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "export"

    export_dataset(config, out)

    dest = out / "case-001" / "skill.md"
    assert dest.is_file() and not dest.is_symlink()
    assert dest.read_text() == "real"


def test_export_optional_s3_upload(tmp_path, monkeypatch):
    config = _make_project(
        tmp_path,
        [{"dest": "triage-skill.md", "source": "plugins/x/skills/triage/SKILL.md"}],
    )
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "export"
    s3 = MagicMock()

    export_dataset(config, out, s3_client=s3, bucket="b", prefix="dataset")

    uploaded_keys = {call.args[2] for call in s3.upload_file.call_args_list}
    # keys mirror download_dataset's {prefix}/{case_id}/{rel} layout
    assert "dataset/case-001/triage-skill.md" in uploaded_keys
    assert "dataset/case-002/triage-skill.md" in uploaded_keys
    assert "dataset/case-001/input.yaml" in uploaded_keys
