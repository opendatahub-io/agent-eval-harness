"""Map agent-eval-harness results to EvalHub JobResults format."""

from datetime import datetime, timezone
from typing import Any

from agent_eval.agent.base import RunResult

try:
    from evalhub.adapter import JobResults, EvaluationResult
except ImportError:
    from agent_eval.evalhub.stubs import JobResults, EvaluationResult  # type: ignore[assignment]


def map_to_job_results(
    job_id: str,
    benchmark_id: str,
    model_name: str,
    run_result: RunResult,
    judge_scores: dict,
    num_cases: int,
    benchmark_index: int = 0,
) -> JobResults:
    """Map agent-eval-harness RunResult and judge scores to EvalHub JobResults.

    Args:
        job_id: Unique identifier for this evaluation job
        benchmark_id: Identifier for the benchmark/eval suite
        model_name: Model being evaluated (e.g. "claude-sonnet-4.5")
        run_result: RunResult from agent execution
        judge_scores: Dict mapping judge name to {mean, pass_rate, values}
        num_cases: Number of test cases evaluated
        benchmark_index: Index in benchmark sequence (default 0)

    Returns:
        JobResults populated with all metrics and metadata
    """
    results = []

    # Built-in metrics from RunResult
    results.append(
        EvaluationResult(
            metric_name="exit_code",
            metric_value=run_result.exit_code,
            metric_type="status",
        )
    )

    results.append(
        EvaluationResult(
            metric_name="duration_seconds",
            metric_value=run_result.duration_s,
            metric_type="performance",
        )
    )

    if run_result.cost_usd is not None:
        results.append(
            EvaluationResult(
                metric_name="cost_usd",
                metric_value=run_result.cost_usd,
                metric_type="cost",
            )
        )

    if run_result.num_turns is not None:
        results.append(
            EvaluationResult(
                metric_name="num_turns",
                metric_value=run_result.num_turns,
                metric_type="usage",
            )
        )

    results.append(
        EvaluationResult(
            metric_name="num_examples_evaluated",
            metric_value=num_cases,
            metric_type="count",
        )
    )

    # Judge metrics
    judge_means = []
    for judge_name, scores in judge_scores.items():
        mean_score = scores.get("mean")
        if mean_score is not None:
            judge_means.append(mean_score)
            results.append(
                EvaluationResult(
                    metric_name=judge_name,
                    metric_value=mean_score,
                    metric_type="judge_score",
                    num_samples=len(scores.get("values", [])),
                    metadata={
                        "pass_rate": scores.get("pass_rate"),
                    },
                )
            )

    # TODO: overall_score needs a proper scoring model — currently skipped
    # because averaging bool judges (0-1) with numeric judges (0-10) is meaningless
    overall_score = None

    # Build evaluation metadata
    evaluation_metadata = {
        "exit_code": run_result.exit_code,
        "resolved_model": run_result.resolved_model,
    }
    if run_result.models_used:
        evaluation_metadata["models_used"] = run_result.models_used
    if run_result.token_usage:
        evaluation_metadata["token_usage"] = run_result.token_usage

    return JobResults(
        id=job_id,
        benchmark_id=benchmark_id,
        benchmark_index=benchmark_index,
        model_name=model_name,
        results=results,
        overall_score=overall_score,
        num_examples_evaluated=num_cases,
        duration_seconds=run_result.duration_s,
        completed_at=datetime.now(timezone.utc),
        evaluation_metadata=evaluation_metadata,
    )
