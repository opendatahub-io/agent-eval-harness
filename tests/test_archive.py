"""Tests for agent_eval.archive — ResultsArchiver with preflight validation."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_eval.archive import ResultsArchiver


class TestValidateRepo:
    """Preflight: validate repo path exists, is a git repo, and is writable."""

    def test_valid_git_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert ResultsArchiver.validate_repo(tmp_path) is True

    def test_nonexistent_path(self, tmp_path):
        assert ResultsArchiver.validate_repo(tmp_path / "nope") is False

    def test_not_a_git_repo(self, tmp_path):
        assert ResultsArchiver.validate_repo(tmp_path) is False

    def test_path_is_file_not_dir(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        assert ResultsArchiver.validate_repo(f) is False


class TestResolveRepoPath:
    """resolve_repo_path: env var → config → interactive → error."""

    def test_from_env_var(self, tmp_path):
        repo = tmp_path / "results"
        repo.mkdir()
        (repo / ".git").mkdir()
        with patch.dict(os.environ, {"RHAI_RESULTS_REPO": str(repo)}):
            path = ResultsArchiver.resolve_repo_path(interactive=False)
        assert path == repo.resolve()

    def test_env_var_invalid_repo_raises(self, tmp_path):
        with patch.dict(os.environ, {"RHAI_RESULTS_REPO": str(tmp_path / "nope")}):
            with pytest.raises(ValueError, match="RHAI_RESULTS_REPO"):
                ResultsArchiver.resolve_repo_path(interactive=False)

    def test_headless_no_env_var_raises(self):
        env = os.environ.copy()
        env.pop("RHAI_RESULTS_REPO", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="headless|RHAI_RESULTS_REPO"):
                ResultsArchiver.resolve_repo_path(interactive=False)

    def test_interactive_no_env_prompts(self, tmp_path, monkeypatch):
        repo = tmp_path / "results"
        repo.mkdir()
        (repo / ".git").mkdir()
        env = os.environ.copy()
        env.pop("RHAI_RESULTS_REPO", None)
        monkeypatch.setattr("builtins.input", lambda _: str(repo))
        with patch.dict(os.environ, env, clear=True):
            path = ResultsArchiver.resolve_repo_path(interactive=True)
        assert path == repo.resolve()


class TestArchiveExperiment:
    """archive_experiment: writes results to repo or falls back."""

    def test_writes_to_repo(self, tmp_path):
        repo = tmp_path / "results"
        repo.mkdir()
        (repo / ".git").mkdir()

        archiver = ResultsArchiver(repo_path=repo)
        exp_data = {"experiment_id": "exp-123", "conditions": [{"model": "a"}]}
        result_path = archiver.archive_experiment("exp-123", exp_data)

        assert result_path.exists()
        assert "exp-123" in str(result_path)

    def test_creates_experiment_subdir(self, tmp_path):
        repo = tmp_path / "results"
        repo.mkdir()
        (repo / ".git").mkdir()

        archiver = ResultsArchiver(repo_path=repo)
        exp_data = {"experiment_id": "exp-abc"}
        result_path = archiver.archive_experiment("exp-abc", exp_data)

        assert result_path.is_file()
        assert result_path.parent.name == "exp-abc"

    def test_fallback_on_invalid_repo(self, tmp_path):
        fallback_root = tmp_path / "fallback"
        archiver = ResultsArchiver(
            repo_path=tmp_path / "nonexistent",
            fallback_dir=fallback_root,
        )
        exp_data = {"experiment_id": "exp-fallback"}
        result_path = archiver.archive_experiment(
            "exp-fallback", exp_data, fallback=True
        )

        assert result_path.exists()
        assert result_path == fallback_root.resolve() / "exp-fallback" / "results.json"

    def test_fallback_directory_location(self, tmp_path):
        fallback_root = tmp_path / "fallback"
        archiver = ResultsArchiver(
            repo_path=tmp_path / "nonexistent",
            fallback_dir=fallback_root,
        )
        exp_data = {"experiment_id": "exp-loc"}
        result_path = archiver.archive_experiment("exp-loc", exp_data, fallback=True)
        assert result_path.parent == fallback_root.resolve() / "exp-loc"

    def test_no_fallback_raises(self, tmp_path):
        archiver = ResultsArchiver(repo_path=tmp_path / "nonexistent")
        exp_data = {"experiment_id": "exp-fail"}
        with pytest.raises(ValueError, match="archive|repo"):
            archiver.archive_experiment("exp-fail", exp_data, fallback=False)

    def test_rejects_path_traversal_experiment_id(self, tmp_path):
        repo = tmp_path / "results"
        repo.mkdir()
        (repo / ".git").mkdir()

        archiver = ResultsArchiver(repo_path=repo)
        with pytest.raises(ValueError, match="Invalid experiment_id"):
            archiver.archive_experiment("../escape", {"experiment_id": "../escape"})
