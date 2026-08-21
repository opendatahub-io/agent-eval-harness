"""Tests for log_results.py path-safety helpers (harbor job-dir traversal)."""

import pytest

try:
    from log_results import (
        _is_within, _safe_trajectory_path, _validity_mlflow_fields,
    )
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


@pytest.mark.skipif(not _has_mlflow, reason="mlflow not installed")
class TestValidityMlflowFields:
    """MLflow routing for summary['validity'] — pure helper, no tracking
    server: numeric coefficients become metrics (never fabricated for
    degenerate/absent values), statuses and metric names become tags."""

    def _summary(self):
        return {"validity": {
            "judges": [
                {"judge": "quality",
                 "irr": {"metric": "krippendorff_alpha", "value": 0.72,
                         "threshold": 0.75, "rationale": "r"},
                 "human_agreement": {"metric": "cohen_kappa",
                                     "value": 0.61, "n": 8}},
                {"judge": "format", "irr": None, "human_agreement": None},
                {"judge": "degenerate",
                 "irr": {"metric": "krippendorff_alpha", "value": None,
                         "reason_code": "perfect_agreement",
                         "threshold": None, "rationale": "r"},
                 "human_agreement": None},
            ],
            "layers": {
                "v1": {"status": "unmeasured"},
                "v2": {"status": "not-applicable"},
                "v3": {"status": "partially-measured"},
            },
            "v_total": {"frame": "V_total <= V1 x V2 x V3", "value": None},
            "same_family": {"family": "anthropic",
                            "models": ["claude-opus-4-8"]},
        }}

    def test_absent_validity_returns_empty(self):
        assert _validity_mlflow_fields({}) == ({}, {})
        assert _validity_mlflow_fields(None) == ({}, {})
        assert _validity_mlflow_fields(
            {"judges": {"q": {"mean": 4.0}}}) == ({}, {})

    def test_metrics_only_for_numeric_values(self):
        metrics, _ = _validity_mlflow_fields(self._summary())
        assert metrics == {"quality/irr_value": 0.72,
                           "quality/human_agreement": 0.61}
        # No metric fabricated for the null-irr or degenerate rows.
        assert not any(k.startswith(("format/", "degenerate/"))
                       for k in metrics)

    def test_tags_shape(self):
        _, tags = _validity_mlflow_fields(self._summary())
        assert tags["validity/v1"] == "unmeasured"
        assert tags["validity/v2"] == "not-applicable"
        assert tags["validity/v3"] == "partially-measured"
        assert tags["validity/same_family"] == "anthropic"
        assert tags["validity/quality/irr_metric"] == "krippendorff_alpha"
        # The degenerate row still names its metric (searchable), the
        # null-irr row does not.
        assert tags["validity/degenerate/irr_metric"] == "krippendorff_alpha"
        assert "validity/format/irr_metric" not in tags

    def test_same_family_no_when_absent(self):
        summary = self._summary()
        summary["validity"]["same_family"] = None
        _, tags = _validity_mlflow_fields(summary)
        assert tags["validity/same_family"] == "no"

    def test_boolean_value_is_not_a_metric(self):
        summary = self._summary()
        summary["validity"]["judges"][0]["irr"]["value"] = True
        metrics, _ = _validity_mlflow_fields(summary)
        assert "quality/irr_value" not in metrics

    def test_pure_function_no_tracking_calls(self):
        """Callable without a tracking server or env config — pure dict work."""
        metrics, tags = _validity_mlflow_fields(self._summary())
        assert isinstance(metrics, dict) and isinstance(tags, dict)
