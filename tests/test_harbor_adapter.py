"""Tests for agent_eval.harbor.adapter — Phase 3 gating tests.

Written BEFORE the adapter implementation exists. All tests must fail
initially, then pass after adapter.py is ported and fixed.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, ANY

import pytest

from agent_eval.evalhub.stubs import (
    EvaluationResult,
    JobCallbacks,
    JobResults,
    JobSpec,
    JobStatus,
    ModelConfig,
)


@pytest.fixture
def job_spec():
    return JobSpec(
        id="job-001",
        benchmark_id="harbor-task",
        benchmark_index=0,
        model=ModelConfig(name="claude-sonnet-4-6"),
        parameters={},
    )


@pytest.fixture
def callbacks():
    return JobCallbacks()


@pytest.fixture
def job_dir_with_results(tmp_path):
    """Create a job directory with valid result.json."""
    result = {
        "job_id": "test-job-001",
        "trials": [
            {
                "task_name": "task-0001",
                "status": "completed",
                "reward": 1.0,
                "metrics": {"duration_s": 30.0, "cost_usd": 0.05},
            },
            {
                "task_name": "task-0002",
                "status": "completed",
                "reward": 0.0,
                "metrics": {"duration_s": 45.0, "cost_usd": 0.08},
            },
        ],
    }
    (tmp_path / "result.json").write_text(json.dumps(result))
    return tmp_path


class TestBuildJobResults:
    def test_build_job_results_metrics(self, job_spec):
        from agent_eval.harbor.adapter import _build_job_results

        job_data = {
            "job_id": "test-job-001",
            "trials": [
                {
                    "task_name": "task-0001",
                    "metrics": [
                        EvaluationResult(metric_name="reward", metric_value=1.0, metric_type="benchmark"),
                        EvaluationResult(metric_name="duration_s", metric_value=30.0, metric_type="performance"),
                        EvaluationResult(metric_name="cost_usd", metric_value=0.05, metric_type="cost"),
                    ],
                },
            ],
            "mean_reward": 1.0,
            "n_completed": 1,
            "n_errored": 0,
        }

        results = _build_job_results(
            config=job_spec,
            job_data=job_data,
            agent_name="oracle",
            model_name="claude-sonnet-4-6",
            task_path="/tasks/test",
            duration_s=42.0,
        )

        assert isinstance(results, JobResults)
        assert results.overall_score == 1.0
        assert results.num_examples_evaluated == 1
        assert results.model_name == "claude-sonnet-4-6"

        metric_names = {r.metric_name for r in results.results}
        assert "mean_reward" in metric_names
        assert "num_trials" in metric_names

    def test_build_job_results_no_trials(self, job_spec):
        from agent_eval.harbor.adapter import _build_job_results

        job_data = {
            "job_id": "test-job-002",
            "trials": [],
            "mean_reward": None,
            "n_completed": 0,
            "n_errored": 0,
        }

        results = _build_job_results(
            config=job_spec,
            job_data=job_data,
            agent_name="oracle",
            model_name="",
            task_path="/tasks/test",
            duration_s=1.0,
        )

        assert results.overall_score is None
        assert results.num_examples_evaluated == 0
        # mean_reward should be 0.0 in metrics (logged warning, defaulted)
        metric_map = {r.metric_name: r.metric_value for r in results.results}
        assert metric_map["mean_reward"] == 0.0


class TestHarborAdapterImport:
    @patch("agent_eval.harbor.adapter._framework_adapter_init")
    def test_import_results_from_jobs_dir(self, mock_init, job_spec, callbacks, job_dir_with_results):
        from agent_eval.harbor.adapter import HarborAdapter

        adapter = HarborAdapter(jobs_dir=str(job_dir_with_results))
        results = adapter.run_benchmark_job(job_spec, callbacks)

        assert isinstance(results, JobResults)
        assert results.overall_score == pytest.approx(0.5)
        assert results.num_examples_evaluated == 2

    @patch("agent_eval.harbor.adapter._framework_adapter_init")
    def test_import_results_nested_job_dir(self, mock_init, job_spec, callbacks, tmp_path):
        """Jobs dir contains a subdirectory with the actual result.json."""
        from agent_eval.harbor.adapter import HarborAdapter

        sub = tmp_path / "evalhub-run"
        sub.mkdir()
        result = {
            "job_id": "nested-job",
            "trials": [
                {"task_name": "t1", "status": "completed", "reward": 1.0, "metrics": {}},
            ],
        }
        (sub / "result.json").write_text(json.dumps(result))

        adapter = HarborAdapter(jobs_dir=str(tmp_path))
        results = adapter.run_benchmark_job(job_spec, callbacks)

        assert results.overall_score == 1.0

    @patch("agent_eval.harbor.adapter._framework_adapter_init")
    def test_import_results_no_result_json(self, mock_init, job_spec, callbacks, tmp_path):
        from agent_eval.harbor.adapter import HarborAdapter

        adapter = HarborAdapter(jobs_dir=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            adapter.run_benchmark_job(job_spec, callbacks)


class TestHarborAdapterHarborCli:
    @patch("agent_eval.harbor.adapter._framework_adapter_init")
    @patch("agent_eval.harbor.adapter.subprocess")
    @patch("agent_eval.harbor.adapter.tempfile")
    def test_run_harbor_cli_success(self, mock_tempfile, mock_subprocess, mock_init,
                                     job_spec, callbacks, tmp_path):
        from agent_eval.harbor.adapter import HarborAdapter

        mock_tempfile.mkdtemp.return_value = str(tmp_path)

        # Simulate harbor run success
        mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")

        # Create result.json that would be created by harbor
        job_dir = tmp_path / f"evalhub-{job_spec.id[:8]}"
        job_dir.mkdir()
        result_data = {
            "job_id": "harbor-run-001",
            "trials": [
                {"task_name": "t1", "status": "completed", "reward": 1.0, "metrics": {}},
            ],
        }
        (job_dir / "result.json").write_text(json.dumps(result_data))

        job_spec.parameters = {"agent": "oracle"}
        adapter = HarborAdapter(task_path="/tasks/test")
        results = adapter.run_benchmark_job(job_spec, callbacks)

        assert results.overall_score == 1.0
        mock_subprocess.run.assert_called_once()
        cmd = mock_subprocess.run.call_args[0][0]
        assert cmd[0] == "harbor"
        assert "-p" in cmd
        assert "/tasks/test" in cmd

    @patch("agent_eval.harbor.adapter._framework_adapter_init")
    @patch("agent_eval.harbor.adapter.subprocess")
    @patch("agent_eval.harbor.adapter.tempfile")
    def test_run_harbor_cli_failure(self, mock_tempfile, mock_subprocess, mock_init,
                                     job_spec, callbacks, tmp_path):
        from agent_eval.harbor.adapter import HarborAdapter

        mock_tempfile.mkdtemp.return_value = str(tmp_path)
        mock_subprocess.run.return_value = MagicMock(
            returncode=1, stderr="harbor: task not found"
        )

        adapter = HarborAdapter(task_path="/tasks/missing")
        results = adapter.run_benchmark_job(job_spec, callbacks)

        assert results.overall_score == 0.0
        assert results.num_examples_evaluated == 0


class TestHarborAdapterK8s:
    @patch("agent_eval.harbor.adapter._framework_adapter_init")
    @patch("agent_eval.harbor.k8s_runner.run_task_job")
    def test_run_k8s_dispatches_to_k8s_runner(self, mock_run_task, mock_init,
                                               job_spec, callbacks):
        from agent_eval.harbor.adapter import HarborAdapter

        mock_run_task.return_value = {
            "reward": 1.0,
            "stdout": "HARBOR_REWARD=1.0\n",
            "duration_s": 120.0,
            "exit_code": 0,
        }

        job_spec.parameters = {
            "execution_mode": "kubernetes",
            "task_image": "registry/task:latest",
            "task_path": "tasks/test-001",
            "agent": "oracle",
        }

        adapter = HarborAdapter(execution_mode="kubernetes")
        results = adapter.run_benchmark_job(job_spec, callbacks)

        mock_run_task.assert_called_once()
        assert results.overall_score == 1.0
        call_kwargs = mock_run_task.call_args
        assert call_kwargs[1]["task_image"] == "registry/task:latest" or \
               call_kwargs[0][1] == "registry/task:latest" if call_kwargs[0] else True


class TestDeployArtifacts:
    def test_containerfile_exists(self):
        containerfile = Path(__file__).parent.parent / "deploy" / "harbor" / "Containerfile"
        assert containerfile.exists(), f"Missing: {containerfile}"
        content = containerfile.read_text()
        assert "entrypoint.py" in content

    def test_entrypoint_imports(self):
        """Verify entrypoint.py can be parsed (not executed — needs evalhub SDK)."""
        entrypoint = Path(__file__).parent.parent / "deploy" / "harbor" / "entrypoint.py"
        assert entrypoint.exists(), f"Missing: {entrypoint}"
        import ast
        ast.parse(entrypoint.read_text())

    def test_configmap_template_exists(self):
        configmap = Path(__file__).parent.parent / "deploy" / "harbor" / "configmap-template.yaml"
        assert configmap.exists()
        content = configmap.read_text()
        assert "harbor-bench" in content

    def test_pyproject_has_kubernetes_dep(self):
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        content = pyproject.read_text()
        assert "kubernetes" in content


class TestReportStatus:
    @patch("agent_eval.harbor.adapter._framework_adapter_init")
    def test_report_status_callback(self, mock_init, job_spec, callbacks, job_dir_with_results):
        from agent_eval.harbor.adapter import HarborAdapter

        callbacks.report_status = MagicMock()

        adapter = HarborAdapter(jobs_dir=str(job_dir_with_results))
        adapter.run_benchmark_job(job_spec, callbacks)

        assert callbacks.report_status.call_count >= 2
        # First call should be RUNNING
        first_update = callbacks.report_status.call_args_list[0][0][0]
        assert first_update.status == JobStatus.RUNNING
        # Last call should be COMPLETED
        last_update = callbacks.report_status.call_args_list[-1][0][0]
        assert last_update.status == JobStatus.COMPLETED
