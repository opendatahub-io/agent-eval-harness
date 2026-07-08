"""Tests for agent_eval.matrix — MatrixBuilder, factorial expansion, cost estimation."""

import tempfile
from pathlib import Path

import pytest
import yaml

from agent_eval.matrix import MatrixBuilder, MatrixConfig, Condition


class TestFromYaml:
    """MatrixBuilder.from_yaml parsing."""

    def test_parses_valid_matrix(self, tmp_path):
        config = {
            "matrix": {
                "factors": {
                    "model": ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
                    "effort": ["low", "high"],
                },
                "replications": 3,
            }
        }
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))

        result = MatrixBuilder.from_yaml(p)
        assert result is not None
        assert result.factors == {
            "model": ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
            "effort": ["low", "high"],
        }
        assert result.replications == 3

    def test_returns_none_for_no_matrix_key(self, tmp_path):
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump({"cases": ["a", "b"]}))
        assert MatrixBuilder.from_yaml(p) is None

    def test_returns_none_for_nonexistent_file(self, tmp_path):
        assert MatrixBuilder.from_yaml(tmp_path / "missing.yaml") is None

    def test_default_replications(self, tmp_path):
        config = {
            "matrix": {
                "factors": {"model": ["a", "b"]},
            }
        }
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))

        result = MatrixBuilder.from_yaml(p)
        assert result is not None
        assert result.replications == 1

    def test_empty_factors_rejected(self, tmp_path):
        config = {"matrix": {"factors": {}}}
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))
        with pytest.raises(ValueError, match="at least one factor"):
            MatrixBuilder.from_yaml(p, strict=True)

    @pytest.mark.parametrize(
        "matrix",
        [
            [],
            "not-a-map",
        ],
    )
    def test_matrix_must_be_mapping(self, tmp_path, matrix):
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump({"matrix": matrix}))
        with pytest.raises(ValueError, match="matrix must be a mapping"):
            MatrixBuilder.from_yaml(p)

    @pytest.mark.parametrize("replications", [0, -1, 1.5, "two", True])
    def test_replications_must_be_positive_integer(self, tmp_path, replications):
        config = {"matrix": {"factors": {"model": ["a"]}, "replications": replications}}
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))
        with pytest.raises(ValueError, match="integer >= 1"):
            MatrixBuilder.from_yaml(p)


class TestExpandFullFactorial:
    """Full factorial expansion of factor levels."""

    def test_single_factor(self):
        factors = {"model": ["a", "b", "c"]}
        conditions = MatrixBuilder.expand_full_factorial(factors)
        assert len(conditions) == 3
        assert conditions[0].levels == {"model": "a"}
        assert conditions[1].levels == {"model": "b"}
        assert conditions[2].levels == {"model": "c"}

    def test_two_factors(self):
        factors = {"model": ["a", "b"], "effort": ["low", "high"]}
        conditions = MatrixBuilder.expand_full_factorial(factors)
        assert len(conditions) == 4
        level_sets = [c.levels for c in conditions]
        assert {"model": "a", "effort": "low"} in level_sets
        assert {"model": "a", "effort": "high"} in level_sets
        assert {"model": "b", "effort": "low"} in level_sets
        assert {"model": "b", "effort": "high"} in level_sets

    def test_three_factors(self):
        factors = {"a": [1, 2], "b": [3, 4], "c": [5, 6]}
        conditions = MatrixBuilder.expand_full_factorial(factors)
        assert len(conditions) == 8  # 2x2x2

    def test_condition_ids_are_unique(self):
        factors = {"model": ["a", "b"], "effort": ["low", "high"]}
        conditions = MatrixBuilder.expand_full_factorial(factors)
        ids = [c.condition_id for c in conditions]
        assert len(ids) == len(set(ids))

    def test_condition_ids_are_deterministic(self):
        factors = {"model": ["a", "b"], "effort": ["low", "high"]}
        c1 = MatrixBuilder.expand_full_factorial(factors)
        c2 = MatrixBuilder.expand_full_factorial(factors)
        assert [c.condition_id for c in c1] == [c.condition_id for c in c2]


class TestGenerateExperimentId:
    """Experiment ID generation from factor lists."""

    def test_includes_factor_names(self):
        factors = {"model": ["a", "b"], "effort": ["low", "high"]}
        exp_id = MatrixBuilder.generate_experiment_id(factors)
        assert "model" in exp_id
        assert "effort" in exp_id

    def test_sanitizes_factor_names_for_experiment_id(self):
        exp_id = MatrixBuilder.generate_experiment_id({"model/name": ["a"]})
        assert "model_name" in exp_id
        assert "/" not in exp_id

    def test_deterministic(self):
        factors = {"model": ["a", "b"]}
        id1 = MatrixBuilder.generate_experiment_id(factors)
        id2 = MatrixBuilder.generate_experiment_id(factors)
        assert id1 == id2

    def test_different_factors_different_ids(self):
        f1 = {"model": ["a", "b"]}
        f2 = {"model": ["a", "b", "c"]}
        assert MatrixBuilder.generate_experiment_id(f1) != MatrixBuilder.generate_experiment_id(f2)


class TestEstimateCost:
    """Cost estimation for experiment runs."""

    def test_basic_cost(self):
        result = MatrixBuilder.estimate_cost(
            n_conditions=4, n_cases=10, replications=3, avg_cost_per_run=0.50
        )
        assert result["total_runs"] == 120  # 4 * 10 * 3
        assert result["estimated_cost"] == pytest.approx(60.0)

    def test_single_run(self):
        result = MatrixBuilder.estimate_cost(
            n_conditions=1, n_cases=1, replications=1, avg_cost_per_run=1.0
        )
        assert result["total_runs"] == 1
        assert result["estimated_cost"] == pytest.approx(1.0)

    def test_zero_cost(self):
        result = MatrixBuilder.estimate_cost(
            n_conditions=4, n_cases=10, replications=3, avg_cost_per_run=0.0
        )
        assert result["estimated_cost"] == pytest.approx(0.0)

    def test_result_keys(self):
        result = MatrixBuilder.estimate_cost(
            n_conditions=2, n_cases=5, replications=2, avg_cost_per_run=0.10
        )
        assert "total_runs" in result
        assert "estimated_cost" in result
        assert "n_conditions" in result
        assert "n_cases" in result
        assert "replications" in result
