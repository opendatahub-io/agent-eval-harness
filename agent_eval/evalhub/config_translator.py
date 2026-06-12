"""Translate EvalConfig to EvalHub provider definition format."""

from agent_eval.config import EvalConfig


# Built-in metrics captured from every evaluation run
BUILT_IN_METRICS = [
    {"name": "exit_code", "type": "int"},
    {"name": "duration_seconds", "type": "float"},
    {"name": "cost_usd", "type": "float"},
    {"name": "num_turns", "type": "int"},
    {"name": "num_examples_evaluated", "type": "int"},
]


def eval_config_to_provider(config: EvalConfig) -> dict:
    """Convert an EvalConfig to EvalHub provider YAML format.

    Args:
        config: Evaluation configuration loaded from eval.yaml

    Returns:
        Dictionary matching EvalHub provider schema with:
        - name: "agent-eval"
        - id: "agent-eval"
        - description: string
        - benchmarks: list with one benchmark per eval config
    """
    # Build metrics list: built-in + one per judge
    metrics = BUILT_IN_METRICS.copy()
    for judge in config.judges:
        # Judge metrics are typically boolean (pass/fail) but can be other types
        # based on feedback_type in the judge config
        metric_type = _infer_metric_type(judge)
        metrics.append({
            "name": judge.name,
            "type": metric_type,
        })

    # Primary score: first judge if present, otherwise exit_code
    if config.judges:
        primary_metric = config.judges[0].name
        # For judges, lower is typically not better (higher scores = better)
        # unless it's a timing or error-based metric
        lower_is_better = False
    else:
        primary_metric = "exit_code"
        # For exit_code, lower is better (0 = success)
        lower_is_better = True

    # Build benchmark
    benchmark = {
        "id": config.name,
        "name": config.name,
        "description": config.description or f"Evaluation suite for {config.skill or 'prompt'}",
        "category": "agent-evaluation",
        "metrics": metrics,
        "primary_score": {
            "metric": primary_metric,
            "lower_is_better": lower_is_better,
        }
    }

    # Add pass_criteria if thresholds are defined
    if config.thresholds:
        benchmark["pass_criteria"] = {
            "threshold": config.thresholds
        }

    # Build provider
    provider = {
        "name": "agent-eval",
        "id": "agent-eval",
        "description": "Agent evaluation harness provider for EvalHub",
        "benchmarks": [benchmark]
    }

    return provider


def _infer_metric_type(judge) -> str:
    """Infer the metric type from a judge configuration.

    Results mapper emits aggregated mean scores (floats) for all judge types,
    so the default is always "float" unless the judge explicitly declares otherwise.
    """
    if judge.feedback_type and judge.feedback_type != "bool":
        return judge.feedback_type
    return "float"
