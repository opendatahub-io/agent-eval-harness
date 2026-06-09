"""Tests for EvalHub results mapper."""

import pytest
from datetime import datetime
from agent_eval.agent.base import RunResult

# Conditional import with fallback stubs (shared with production code)
try:
    from evalhub.adapter import JobResults, EvaluationResult
    EVALHUB_AVAILABLE = True
except ImportError:
    from agent_eval.evalhub.stubs import JobResults, EvaluationResult
    EVALHUB_AVAILABLE = False

from agent_eval.evalhub.results_mapper import map_to_job_results


def test_basic_mapping():
    """Test mapping with successful run and judge scores."""
    run_result = RunResult(
        exit_code=0,
        stdout="Task completed successfully",
        stderr="",
        duration_s=45.5,
        cost_usd=0.15,
        num_turns=12,
        token_usage={"input": 1000, "output": 500},
    )

    judge_scores = {
        "accuracy": {
            "mean": 0.85,
            "pass_rate": 0.8,
            "values": [1.0, 0.7, 0.9, 0.8],
        },
        "completeness": {
            "mean": 0.92,
            "pass_rate": 0.9,
            "values": [0.9, 0.95, 0.91, 0.92],
        },
    }

    job_results = map_to_job_results(
        job_id="test-job-1",
        benchmark_id="skill-eval-v1",
        model_name="claude-sonnet-4.5",
        run_result=run_result,
        judge_scores=judge_scores,
        num_cases=4,
        benchmark_index=0,
    )

    # Verify JobResults fields
    assert job_results.id == "test-job-1"
    assert job_results.benchmark_id == "skill-eval-v1"
    assert job_results.benchmark_index == 0
    assert job_results.model_name == "claude-sonnet-4.5"
    assert job_results.num_examples_evaluated == 4
    assert job_results.duration_seconds == 45.5
    assert isinstance(job_results.completed_at, datetime)

    # Verify built-in metrics
    metric_names = {r.metric_name for r in job_results.results}
    assert "exit_code" in metric_names
    assert "duration_seconds" in metric_names
    assert "cost_usd" in metric_names
    assert "num_turns" in metric_names
    assert "num_examples_evaluated" in metric_names

    # Find specific metrics
    exit_code_metric = next(r for r in job_results.results if r.metric_name == "exit_code")
    assert exit_code_metric.metric_value == 0
    assert exit_code_metric.metric_type == "status"

    cost_metric = next(r for r in job_results.results if r.metric_name == "cost_usd")
    assert cost_metric.metric_value == 0.15
    assert cost_metric.metric_type == "cost"

    turns_metric = next(r for r in job_results.results if r.metric_name == "num_turns")
    assert turns_metric.metric_value == 12
    assert turns_metric.metric_type == "usage"

    # Verify judge metrics
    assert "accuracy" in metric_names
    assert "completeness" in metric_names

    accuracy_metric = next(r for r in job_results.results if r.metric_name == "accuracy")
    assert accuracy_metric.metric_value == 0.85
    assert accuracy_metric.metric_type == "judge_score"
    assert accuracy_metric.num_samples == 4

    completeness_metric = next(r for r in job_results.results if r.metric_name == "completeness")
    assert completeness_metric.metric_value == 0.92
    assert completeness_metric.metric_type == "judge_score"
    assert completeness_metric.num_samples == 4

    # overall_score is currently disabled (mixing bool/numeric scales)
    assert job_results.overall_score is None


def test_failed_run():
    """Test mapping with failed run and no judges."""
    run_result = RunResult(
        exit_code=1,
        stdout="Partial output",
        stderr="Error: timeout exceeded",
        duration_s=600.0,
        cost_usd=0.05,
        num_turns=3,
    )

    job_results = map_to_job_results(
        job_id="test-job-2",
        benchmark_id="skill-eval-v1",
        model_name="claude-sonnet-4.5",
        run_result=run_result,
        judge_scores={},
        num_cases=0,
        benchmark_index=1,
    )

    # Verify exit_code=1
    exit_code_metric = next(r for r in job_results.results if r.metric_name == "exit_code")
    assert exit_code_metric.metric_value == 1

    # Verify num_examples_evaluated=0
    assert job_results.num_examples_evaluated == 0
    examples_metric = next(r for r in job_results.results if r.metric_name == "num_examples_evaluated")
    assert examples_metric.metric_value == 0

    # No judges means no overall_score
    assert job_results.overall_score is None

    # Verify other fields
    assert job_results.duration_seconds == 600.0
    assert job_results.benchmark_index == 1
