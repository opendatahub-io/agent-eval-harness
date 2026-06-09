"""Smoke test: entrypoint calls callbacks.mlflow.save() and report_results().

Verifies the EvalHub SDK prescribed pattern is followed:
  results = adapter.run_benchmark_job(spec, callbacks)
  rid = callbacks.mlflow.save(results, spec)
  results.mlflow_run_id = rid
  callbacks.report_results(results)
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_eval.evalhub.stubs import JobResults, EvaluationResult


def _load_entrypoint():
    """Load entrypoint.py as a module without requiring it to be a package."""
    ep_path = Path(__file__).resolve().parent.parent.parent / "deploy" / "evalhub" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("entrypoint", ep_path)
    mod = importlib.util.module_from_spec(spec)
    return mod, spec


def _make_job_results():
    return JobResults(
        id="test-job",
        benchmark_id="skill-eval",
        benchmark_index=0,
        model_name="claude-sonnet-4-6",
        results=[
            EvaluationResult(metric_name="exit_code", metric_value=0, metric_type="status"),
            EvaluationResult(metric_name="duration_seconds", metric_value=10.5, metric_type="performance"),
        ],
        overall_score=0.85,
        num_examples_evaluated=2,
        duration_seconds=10.5,
        completed_at=datetime.now(),
    )


def _make_mock_spec():
    mock_spec = MagicMock()
    mock_spec.id = "test-job"
    mock_spec.provider_id = "agent-eval"
    mock_spec.benchmark_id = "skill-eval"
    mock_spec.benchmark_index = 0
    mock_spec.model = MagicMock()
    mock_spec.model.url = "vertex-ai"
    mock_spec.model.name = "claude-sonnet-4-6"
    mock_spec.parameters = {}
    mock_spec.experiment_name = "test-experiment"
    return mock_spec


def test_entrypoint_calls_mlflow_save_and_report_results():
    """Entrypoint must call callbacks.mlflow.save() then callbacks.report_results()."""
    mock_spec = _make_mock_spec()
    results = _make_job_results()

    mock_callbacks = MagicMock()
    mock_callbacks.mlflow.save.return_value = "mlflow-run-abc123"

    mock_adapter = MagicMock()
    mock_adapter.run_benchmark_job.return_value = results

    # Mock the SDK imports that entrypoint.py does at module level
    mock_jobspec_cls = MagicMock()
    mock_jobspec_cls.from_file.return_value = mock_spec

    mock_callbacks_cls = MagicMock(return_value=mock_callbacks)
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch.dict(sys.modules, {"evalhub": MagicMock(), "evalhub.adapter": MagicMock()}):
        # Load the entrypoint module with mocked SDK
        mod, spec = _load_entrypoint()
        mod.JobSpec = mock_jobspec_cls
        mod.DefaultCallbacks = mock_callbacks_cls
        mod.AgentEvalAdapter = mock_adapter_cls
        spec.loader.exec_module(mod)

        # Rebind after exec (exec_module re-imports)
        mod.JobSpec = mock_jobspec_cls
        mod.DefaultCallbacks = mock_callbacks_cls
        mod.AgentEvalAdapter = mock_adapter_cls

        mod.main()

    # Verify: mlflow.save was called with results and spec
    mock_callbacks.mlflow.save.assert_called_once_with(results, mock_spec)

    # Verify: mlflow_run_id was set on results
    assert results.mlflow_run_id == "mlflow-run-abc123"

    # Verify: report_results was called
    mock_callbacks.report_results.assert_called_once_with(results)

    # Verify: the results passed to report_results has the mlflow_run_id
    reported = mock_callbacks.report_results.call_args[0][0]
    assert reported.mlflow_run_id == "mlflow-run-abc123"


def test_entrypoint_handles_mlflow_save_returning_none():
    """When mlflow.save returns None (no experiment configured), mlflow_run_id stays None."""
    mock_spec = _make_mock_spec()
    results = _make_job_results()

    mock_callbacks = MagicMock()
    mock_callbacks.mlflow.save.return_value = None

    mock_adapter = MagicMock()
    mock_adapter.run_benchmark_job.return_value = results

    mock_jobspec_cls = MagicMock()
    mock_jobspec_cls.from_file.return_value = mock_spec

    mock_callbacks_cls = MagicMock(return_value=mock_callbacks)
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch.dict(sys.modules, {"evalhub": MagicMock(), "evalhub.adapter": MagicMock()}):
        mod, spec = _load_entrypoint()
        mod.JobSpec = mock_jobspec_cls
        mod.DefaultCallbacks = mock_callbacks_cls
        mod.AgentEvalAdapter = mock_adapter_cls
        spec.loader.exec_module(mod)

        mod.JobSpec = mock_jobspec_cls
        mod.DefaultCallbacks = mock_callbacks_cls
        mod.AgentEvalAdapter = mock_adapter_cls

        mod.main()

    # mlflow.save was called but returned None
    mock_callbacks.mlflow.save.assert_called_once()

    # mlflow_run_id should remain None
    assert results.mlflow_run_id is None

    # report_results still called
    mock_callbacks.report_results.assert_called_once_with(results)
