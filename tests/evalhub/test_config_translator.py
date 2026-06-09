"""Tests for EvalConfig to EvalHub provider translation."""

from agent_eval.config import EvalConfig, JudgeConfig, ExecutionConfig
from agent_eval.evalhub.config_translator import eval_config_to_provider


def test_basic_provider_generation():
    """Config with 2 judges produces correct provider with all metrics."""
    config = EvalConfig(
        name="test-eval",
        description="Test evaluation suite",
        skill="test-skill",
        execution=ExecutionConfig(mode="case", arguments="--arg {field}"),
        judges=[
            JudgeConfig(
                name="correctness",
                description="Checks if output is correct",
                check="return (True, 'pass')"
            ),
            JudgeConfig(
                name="completeness",
                description="Checks if output is complete",
                prompt="Is the output complete?"
            ),
        ]
    )

    provider = eval_config_to_provider(config)

    # Provider structure
    assert provider["name"] == "agent-eval"
    assert provider["id"] == "agent-eval"
    assert "description" in provider
    assert isinstance(provider["description"], str)

    # Benchmarks
    assert "benchmarks" in provider
    assert len(provider["benchmarks"]) == 1

    benchmark = provider["benchmarks"][0]
    assert benchmark["id"] == "test-eval"
    assert benchmark["name"] == "test-eval"
    assert benchmark["description"] == "Test evaluation suite"
    assert benchmark["category"] == "agent-evaluation"

    # Metrics - should have built-in + judge metrics
    metrics = benchmark["metrics"]
    metric_names = [m["name"] for m in metrics]

    # Built-in metrics
    assert "exit_code" in metric_names
    assert "duration_seconds" in metric_names
    assert "cost_usd" in metric_names
    assert "num_turns" in metric_names
    assert "num_examples_evaluated" in metric_names

    # Judge metrics
    assert "correctness" in metric_names
    assert "completeness" in metric_names

    # Check metric types
    exit_code_metric = next(m for m in metrics if m["name"] == "exit_code")
    assert exit_code_metric["type"] == "int"

    cost_metric = next(m for m in metrics if m["name"] == "cost_usd")
    assert cost_metric["type"] == "float"


def test_primary_score_uses_first_judge():
    """Primary score metric is the first judge's name."""
    config = EvalConfig(
        name="test-eval",
        judges=[
            JudgeConfig(name="accuracy", prompt="Is it accurate?"),
            JudgeConfig(name="speed", check="return (True, 'fast')"),
        ]
    )

    provider = eval_config_to_provider(config)
    benchmark = provider["benchmarks"][0]

    assert "primary_score" in benchmark
    assert benchmark["primary_score"]["metric"] == "accuracy"
    assert isinstance(benchmark["primary_score"]["lower_is_better"], bool)


def test_pass_criteria_from_thresholds():
    """Thresholds dict maps to pass_criteria."""
    config = EvalConfig(
        name="test-eval",
        judges=[JudgeConfig(name="quality", prompt="Good quality?")],
        thresholds={
            "min_mean": 0.8,
            "min_pass_rate": 0.9,
            "min_win_rate": 0.7,
        }
    )

    provider = eval_config_to_provider(config)
    benchmark = provider["benchmarks"][0]

    assert "pass_criteria" in benchmark
    assert benchmark["pass_criteria"]["threshold"] == config.thresholds


def test_no_judges_still_has_built_in_metrics():
    """Even with no judges, built-in metrics are present."""
    config = EvalConfig(
        name="test-eval",
        description="Eval without judges",
        judges=[]
    )

    provider = eval_config_to_provider(config)
    benchmark = provider["benchmarks"][0]

    metrics = benchmark["metrics"]
    metric_names = [m["name"] for m in metrics]

    # Built-in metrics still present
    assert "exit_code" in metric_names
    assert "duration_seconds" in metric_names
    assert "cost_usd" in metric_names
    assert "num_turns" in metric_names
    assert "num_examples_evaluated" in metric_names

    # Primary score falls back to exit_code
    assert benchmark["primary_score"]["metric"] == "exit_code"
    assert benchmark["primary_score"]["lower_is_better"] is True
