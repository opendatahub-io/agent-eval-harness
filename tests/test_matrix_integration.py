"""Integration tests for matrix config — backward compatibility with eval.yaml."""

import yaml
import pytest

from agent_eval.matrix import MatrixBuilder


class TestEvalYamlBackwardCompat:
    """Matrix config coexists with standard eval config without breaking it."""

    def test_matrix_alongside_eval_keys(self, tmp_path):
        config = {
            "model": "claude-sonnet-4-20250514",
            "cases": ["case_a", "case_b"],
            "judges": {"quality": {"type": "numeric"}},
            "matrix": {
                "factors": {"model": ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"]},
                "replications": 2,
            },
        }
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))

        result = MatrixBuilder.from_yaml(p)
        assert result is not None
        assert len(result.factors["model"]) == 2
        assert result.replications == 2

    def test_no_matrix_returns_none(self, tmp_path):
        config = {
            "model": "claude-sonnet-4-20250514",
            "cases": ["case_a"],
            "judges": {},
        }
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))

        assert MatrixBuilder.from_yaml(p) is None

    def test_preserves_non_matrix_yaml(self, tmp_path):
        config = {
            "model": "claude-sonnet-4-20250514",
            "cases": ["a", "b", "c"],
            "matrix": {
                "factors": {"model": ["x", "y"]},
            },
        }
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))

        with open(p) as f:
            raw = yaml.safe_load(f)

        assert raw["model"] == "claude-sonnet-4-20250514"
        assert raw["cases"] == ["a", "b", "c"]

    def test_matrix_with_single_factor(self, tmp_path):
        config = {
            "matrix": {
                "factors": {"model": ["only-one"]},
            }
        }
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))

        result = MatrixBuilder.from_yaml(p)
        assert result is not None
        conditions = MatrixBuilder.expand_full_factorial(result.factors)
        assert len(conditions) == 1

    def test_matrix_many_factors(self, tmp_path):
        config = {
            "matrix": {
                "factors": {
                    "model": ["a", "b"],
                    "effort": ["low", "mid", "high"],
                    "temp": [0.0, 1.0],
                },
                "replications": 5,
            }
        }
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))

        result = MatrixBuilder.from_yaml(p)
        conditions = MatrixBuilder.expand_full_factorial(result.factors)
        assert len(conditions) == 12  # 2 * 3 * 2
        assert result.replications == 5
