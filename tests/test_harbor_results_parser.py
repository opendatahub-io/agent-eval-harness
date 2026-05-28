"""Tests for agent_eval.harbor.results_parser — Phase 1 gating tests.

Written BEFORE the implementation exists. All tests must fail with
ImportError initially, then pass after results_parser.py is created.
"""

import json
import pytest
from pathlib import Path


@pytest.fixture
def job_dir(tmp_path):
    """Create a minimal Harbor job directory with result.json."""
    return tmp_path


def _write_result(job_dir: Path, data: dict) -> None:
    (job_dir / "result.json").write_text(json.dumps(data))


class TestParseJob:
    def test_parse_job_single_trial(self, job_dir):
        """Parse a minimal result.json with one completed trial."""
        _write_result(job_dir, {
            "job_id": "test-job-001",
            "trials": [
                {
                    "task_name": "task-0001",
                    "status": "completed",
                    "reward": 1.0,
                    "metrics": {
                        "duration_s": 42.5,
                        "cost_usd": 0.03,
                    },
                }
            ],
        })

        from agent_eval.harbor.results_parser import parse_job

        result = parse_job(job_dir)

        assert result["job_id"] == "test-job-001"
        assert result["mean_reward"] == 1.0
        assert result["n_completed"] == 1
        assert result["n_errored"] == 0
        assert len(result["trials"]) == 1

        trial = result["trials"][0]
        assert trial["task_name"] == "task-0001"
        metric_names = {m.metric_name for m in trial["metrics"]}
        assert "reward" in metric_names
        assert "duration_s" in metric_names

    def test_parse_job_multiple_trials(self, job_dir):
        """Parse result.json with multiple trials including mixed outcomes."""
        _write_result(job_dir, {
            "job_id": "test-job-002",
            "trials": [
                {
                    "task_name": "task-0001",
                    "status": "completed",
                    "reward": 1.0,
                    "metrics": {"duration_s": 30.0},
                },
                {
                    "task_name": "task-0002",
                    "status": "completed",
                    "reward": 0.0,
                    "metrics": {"duration_s": 45.0},
                },
                {
                    "task_name": "task-0003",
                    "status": "completed",
                    "reward": 1.0,
                    "metrics": {"duration_s": 20.0},
                },
            ],
        })

        from agent_eval.harbor.results_parser import parse_job

        result = parse_job(job_dir)

        assert result["job_id"] == "test-job-002"
        assert len(result["trials"]) == 3
        assert result["n_completed"] == 3
        assert result["n_errored"] == 0
        assert result["mean_reward"] == pytest.approx(2.0 / 3.0)

    def test_parse_job_no_result_file(self, job_dir):
        """FileNotFoundError when result.json is missing."""
        from agent_eval.harbor.results_parser import parse_job

        with pytest.raises(FileNotFoundError):
            parse_job(job_dir)

    def test_parse_job_errored_trial(self, job_dir):
        """Errored trials counted in n_errored, excluded from mean_reward."""
        _write_result(job_dir, {
            "job_id": "test-job-003",
            "trials": [
                {
                    "task_name": "task-0001",
                    "status": "completed",
                    "reward": 1.0,
                    "metrics": {"duration_s": 30.0},
                },
                {
                    "task_name": "task-0002",
                    "status": "errored",
                    "reward": None,
                    "metrics": {},
                    "error": "Container OOMKilled",
                },
            ],
        })

        from agent_eval.harbor.results_parser import parse_job

        result = parse_job(job_dir)

        assert result["n_completed"] == 1
        assert result["n_errored"] == 1
        assert result["mean_reward"] == 1.0  # Only completed trial counts

    def test_parse_job_all_errored(self, job_dir):
        """All trials errored — mean_reward should be None."""
        _write_result(job_dir, {
            "job_id": "test-job-004",
            "trials": [
                {
                    "task_name": "task-0001",
                    "status": "errored",
                    "reward": None,
                    "metrics": {},
                },
            ],
        })

        from agent_eval.harbor.results_parser import parse_job

        result = parse_job(job_dir)

        assert result["n_completed"] == 0
        assert result["n_errored"] == 1
        assert result["mean_reward"] is None

    def test_parse_job_metrics_are_evaluation_results(self, job_dir):
        """Trial metrics should be EvaluationResult objects with proper fields."""
        _write_result(job_dir, {
            "job_id": "test-job-005",
            "trials": [
                {
                    "task_name": "task-0001",
                    "status": "completed",
                    "reward": 1.0,
                    "metrics": {
                        "duration_s": 42.5,
                        "cost_usd": 0.03,
                        "input_tokens": 1500,
                        "output_tokens": 800,
                    },
                },
            ],
        })

        from agent_eval.harbor.results_parser import parse_job

        result = parse_job(job_dir)
        trial = result["trials"][0]

        for m in trial["metrics"]:
            assert hasattr(m, "metric_name")
            assert hasattr(m, "metric_value")
            assert hasattr(m, "metric_type")

        metric_map = {m.metric_name: m for m in trial["metrics"]}
        assert metric_map["cost_usd"].metric_value == 0.03
        assert metric_map["cost_usd"].metric_type == "cost"
        assert metric_map["reward"].metric_value == 1.0
        assert metric_map["reward"].metric_type == "benchmark"
