"""Unit tests for reorganize_root_config()."""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.reorganize import reorganize_root_config


def _setup_root_config(tmp_path, dataset_path="cases/", eval_md=True):
    config = {
        "skill": "my-skill",
        "name": "my-eval",
        "dataset": {"path": dataset_path},
        "outputs": [{"path": "output/", "schema": "output files"}],
    }
    (tmp_path / "eval.yaml").write_text(yaml.safe_dump(config))
    if eval_md:
        (tmp_path / "eval.md").write_text("# Analysis\n")
    (tmp_path / dataset_path.rstrip("/")).mkdir(parents=True, exist_ok=True)
    return config


def test_reorganize_moves_files(tmp_path):
    _setup_root_config(tmp_path)
    result = reorganize_root_config(tmp_path, "my-skill")

    assert not (tmp_path / "eval.yaml").exists()
    assert not (tmp_path / "eval.md").exists()
    assert (tmp_path / "eval" / "my-skill" / "eval.yaml").exists()
    assert (tmp_path / "eval" / "my-skill" / "eval.md").exists()
    assert len(result.moved) == 2


def test_reorganize_updates_dataset_path(tmp_path):
    _setup_root_config(tmp_path)
    result = reorganize_root_config(tmp_path, "my-skill")

    with open(result.target_config) as f:
        new_config = yaml.safe_load(f)

    new_ds_path = new_config["dataset"]["path"]
    resolved = (result.target_config.parent / new_ds_path).resolve()
    assert resolved == (tmp_path / "cases").resolve()


def test_reorganize_preserves_outputs_path(tmp_path):
    _setup_root_config(tmp_path)
    result = reorganize_root_config(tmp_path, "my-skill")

    with open(result.target_config) as f:
        new_config = yaml.safe_load(f)

    assert new_config["outputs"][0]["path"] == "output/"


def test_reorganize_missing_eval_md(tmp_path):
    _setup_root_config(tmp_path, eval_md=False)
    result = reorganize_root_config(tmp_path, "my-skill")

    assert len(result.moved) == 1
    assert len(result.warnings) == 1
    assert "eval.md" in result.warnings[0]


def test_reorganize_target_exists_aborts(tmp_path):
    _setup_root_config(tmp_path)
    target = tmp_path / "eval" / "my-skill"
    target.mkdir(parents=True)
    (target / "eval.yaml").write_text("existing: true\n")

    with pytest.raises(FileExistsError):
        reorganize_root_config(tmp_path, "my-skill")


def test_reorganize_no_source_aborts(tmp_path):
    with pytest.raises(FileNotFoundError):
        reorganize_root_config(tmp_path, "my-skill")


def test_reorganize_rejects_path_traversal_name(tmp_path):
    _setup_root_config(tmp_path)
    with pytest.raises(ValueError):
        reorganize_root_config(tmp_path, "../../escape")


def test_reorganize_preserves_absolute_dataset_path(tmp_path):
    config = {
        "skill": "my-skill",
        "name": "my-eval",
        "dataset": {"path": "/shared/datasets/cases"},
    }
    (tmp_path / "eval.yaml").write_text(yaml.safe_dump(config))
    (tmp_path / "eval.md").write_text("# Analysis\n")
    result = reorganize_root_config(tmp_path, "my-skill")
    with open(result.target_config) as f:
        new_config = yaml.safe_load(f)
    assert new_config["dataset"]["path"] == "/shared/datasets/cases"


def test_reorganize_nonexistent_dataset_preserves_path(tmp_path):
    """When dataset dir doesn't exist, path is not rewritten."""
    config = {"skill": "my-skill", "dataset": {"path": "cases/"}}
    (tmp_path / "eval.yaml").write_text(yaml.safe_dump(config))
    (tmp_path / "eval.md").write_text("# Analysis\n")
    result = reorganize_root_config(tmp_path, "my-skill")
    with open(result.target_config) as f:
        new_config = yaml.safe_load(f)
    assert new_config["dataset"]["path"] == "cases/"
