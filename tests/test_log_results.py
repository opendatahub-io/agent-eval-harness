"""Tests for log_results.py path-safety helpers (harbor job-dir traversal)."""

import pytest

try:
    from log_results import _is_within, _safe_trajectory_path
    _has_mlflow = True
except ModuleNotFoundError as exc:
    # Skip only when mlflow itself is missing; re-raise unrelated import errors
    # so path-safety regressions are never silently skipped.
    if exc.name != "mlflow":
        raise
    _has_mlflow = False
except SystemExit as exc:
    # log_results.py calls sys.exit(0) when mlflow is missing at import time.
    if exc.code not in (0, None):
        raise
    _has_mlflow = False


@pytest.mark.skipif(not _has_mlflow, reason="mlflow not installed")
class TestSafeTrajectoryPath:
    def test_accepts_real_file_under_job_root(self, tmp_path):
        """A regular trajectory.json alongside the transcript is accepted."""
        job_root = tmp_path / "job"
        step_dir = job_root / "case__abc" / "agent"
        step_dir.mkdir(parents=True)
        transcript = step_dir / "claude-code.txt"
        transcript.write_text("{}\n")
        traj = step_dir / "trajectory.json"
        traj.write_text("{}")

        result = _safe_trajectory_path(transcript, job_root)
        assert result == traj

    def test_missing_trajectory_returns_none(self, tmp_path):
        """No trajectory.json present -> None, no error."""
        job_root = tmp_path / "job"
        step_dir = job_root / "case__abc" / "agent"
        step_dir.mkdir(parents=True)
        transcript = step_dir / "claude-code.txt"
        transcript.write_text("{}\n")

        assert _safe_trajectory_path(transcript, job_root) is None

    def test_rejects_symlinked_trajectory(self, tmp_path):
        """A trajectory.json symlink pointing outside job_root is rejected."""
        job_root = tmp_path / "job"
        step_dir = job_root / "case__abc" / "agent"
        step_dir.mkdir(parents=True)
        transcript = step_dir / "claude-code.txt"
        transcript.write_text("{}\n")

        secret = tmp_path / "outside" / "secret.json"
        secret.parent.mkdir(parents=True)
        secret.write_text('{"leaked": true}')
        traj = step_dir / "trajectory.json"
        traj.symlink_to(secret)

        assert _safe_trajectory_path(transcript, job_root) is None

    def test_rejects_symlink_even_when_target_is_inside_root(self, tmp_path):
        """Symlinks are rejected outright, regardless of where they point."""
        job_root = tmp_path / "job"
        step_dir = job_root / "case__abc" / "agent"
        step_dir.mkdir(parents=True)
        transcript = step_dir / "claude-code.txt"
        transcript.write_text("{}\n")

        real = job_root / "real_trajectory.json"
        real.write_text("{}")
        traj = step_dir / "trajectory.json"
        traj.symlink_to(real)

        assert _safe_trajectory_path(transcript, job_root) is None


@pytest.mark.skipif(not _has_mlflow, reason="mlflow not installed")
class TestIsWithin:
    def test_descendant_path_is_within(self, tmp_path):
        root = tmp_path / "root"
        child = root / "a" / "b.txt"
        child.parent.mkdir(parents=True)
        child.write_text("x")
        assert _is_within(child, root) is True

    def test_escaping_path_is_not_within(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        assert _is_within(outside, root) is False

    def test_nonexistent_path_is_not_within(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        assert _is_within(root / "missing.txt", root) is False
