"""Tests for agent_eval.composite — composite scoring with bool/int separation."""

import pytest

from agent_eval.composite import composite_score, aggregate_replications


class TestBoolIntSeparation:
    """Critical fix: Python bool is subclass of int. Bools must NOT enter the numeric path."""

    def test_bool_true_not_treated_as_int(self):
        results = {"pass": True, "score": 0.8}
        configs = {"pass": {"type": "boolean"}, "score": {"type": "numeric", "weight": 1.0}}
        score = composite_score(results, configs)
        assert 0.0 <= score <= 1.0

    def test_bool_false_gates_to_zero(self):
        results = {"pass": False, "score": 0.9}
        configs = {
            "pass": {"type": "boolean", "gate": True},
            "score": {"type": "numeric", "weight": 1.0},
        }
        score = composite_score(results, configs)
        assert score == 0.0

    def test_all_bool_all_pass(self):
        results = {"a": True, "b": True, "c": True}
        configs = {
            "a": {"type": "boolean"},
            "b": {"type": "boolean"},
            "c": {"type": "boolean"},
        }
        score = composite_score(results, configs)
        assert score == 1.0

    def test_all_bool_one_fails(self):
        results = {"a": True, "b": False, "c": True}
        configs = {
            "a": {"type": "boolean"},
            "b": {"type": "boolean"},
            "c": {"type": "boolean"},
        }
        score = composite_score(results, configs)
        assert score < 1.0

    def test_bool_value_1_not_treated_as_numeric(self):
        """True == 1 in Python, but must stay in the boolean path."""
        results = {"judge": True}
        configs = {"judge": {"type": "boolean"}}
        score = composite_score(results, configs)
        assert score == 1.0

    def test_bool_value_0_not_treated_as_numeric(self):
        """False == 0 in Python, but must stay in the boolean path."""
        results = {"judge": False}
        configs = {"judge": {"type": "boolean"}}
        score = composite_score(results, configs)
        assert score == 0.0


class TestGateLogic:
    """Gate judges: if any gate fails, entire composite is 0."""

    def test_gate_pass_allows_numeric(self):
        results = {"gate": True, "quality": 0.7}
        configs = {
            "gate": {"type": "boolean", "gate": True},
            "quality": {"type": "numeric", "weight": 1.0},
        }
        score = composite_score(results, configs)
        assert score == pytest.approx(0.7)

    def test_gate_fail_zeroes_everything(self):
        results = {"gate": False, "quality": 0.9}
        configs = {
            "gate": {"type": "boolean", "gate": True},
            "quality": {"type": "numeric", "weight": 1.0},
        }
        assert composite_score(results, configs) == 0.0

    def test_multiple_gates_all_must_pass(self):
        results = {"g1": True, "g2": True, "score": 0.5}
        configs = {
            "g1": {"type": "boolean", "gate": True},
            "g2": {"type": "boolean", "gate": True},
            "score": {"type": "numeric", "weight": 1.0},
        }
        assert composite_score(results, configs) == pytest.approx(0.5)

    def test_multiple_gates_one_fails(self):
        results = {"g1": True, "g2": False, "score": 0.5}
        configs = {
            "g1": {"type": "boolean", "gate": True},
            "g2": {"type": "boolean", "gate": True},
            "score": {"type": "numeric", "weight": 1.0},
        }
        assert composite_score(results, configs) == 0.0

    def test_non_gate_bool_fail_reduces_but_not_gates(self):
        """Non-gate bool failure reduces score proportionally (via bool pass rate),
        but doesn't unconditionally zero like a gate does."""
        results = {"check1": True, "check2": False, "score": 0.8}
        configs = {
            "check1": {"type": "boolean"},
            "check2": {"type": "boolean"},
            "score": {"type": "numeric", "weight": 1.0},
        }
        score = composite_score(results, configs)
        assert score == pytest.approx(0.4)  # 0.8 * (1/2 non-gate bools pass)


class TestWeightedNumericScoring:
    """Weighted average of numeric judges."""

    def test_single_numeric(self):
        results = {"quality": 0.75}
        configs = {"quality": {"type": "numeric", "weight": 1.0}}
        assert composite_score(results, configs) == pytest.approx(0.75)

    def test_equal_weights(self):
        results = {"a": 0.6, "b": 0.8}
        configs = {
            "a": {"type": "numeric", "weight": 1.0},
            "b": {"type": "numeric", "weight": 1.0},
        }
        assert composite_score(results, configs) == pytest.approx(0.7)

    def test_unequal_weights(self):
        results = {"a": 1.0, "b": 0.0}
        configs = {
            "a": {"type": "numeric", "weight": 3.0},
            "b": {"type": "numeric", "weight": 1.0},
        }
        assert composite_score(results, configs) == pytest.approx(0.75)

    def test_numeric_clamp_above_one(self):
        results = {"score": 1.5}
        configs = {"score": {"type": "numeric", "weight": 1.0}}
        score = composite_score(results, configs)
        assert score <= 1.0

    def test_numeric_clamp_below_zero(self):
        results = {"score": -0.5}
        configs = {"score": {"type": "numeric", "weight": 1.0}}
        score = composite_score(results, configs)
        assert score >= 0.0


class TestMixedBoolNumeric:
    """Combined boolean and numeric judges."""

    def test_mixed_all_pass(self):
        results = {"gate": True, "check": True, "quality": 0.8, "style": 0.6}
        configs = {
            "gate": {"type": "boolean", "gate": True},
            "check": {"type": "boolean"},
            "quality": {"type": "numeric", "weight": 2.0},
            "style": {"type": "numeric", "weight": 1.0},
        }
        score = composite_score(results, configs)
        expected_numeric = (0.8 * 2.0 + 0.6 * 1.0) / 3.0
        bool_bonus = 1.0  # all non-gate bools pass
        expected = expected_numeric * bool_bonus
        assert score == pytest.approx(expected, abs=0.01)

    def test_no_numeric_judges(self):
        results = {"a": True, "b": True}
        configs = {
            "a": {"type": "boolean"},
            "b": {"type": "boolean"},
        }
        assert composite_score(results, configs) == 1.0

    def test_no_boolean_judges(self):
        results = {"a": 0.5, "b": 0.5}
        configs = {
            "a": {"type": "numeric", "weight": 1.0},
            "b": {"type": "numeric", "weight": 1.0},
        }
        assert composite_score(results, configs) == pytest.approx(0.5)


class TestDefaultConfigs:
    """When configs are minimal or missing fields."""

    def test_default_weight_is_one(self):
        results = {"a": 0.6, "b": 0.8}
        configs = {
            "a": {"type": "numeric"},
            "b": {"type": "numeric"},
        }
        assert composite_score(results, configs) == pytest.approx(0.7)

    def test_inferred_type_from_value(self):
        results = {"check": True, "score": 0.5}
        configs = {}
        score = composite_score(results, configs)
        assert 0.0 <= score <= 1.0


class TestAggregateReplications:
    """Aggregating scores across replications."""

    def test_mean_of_scores(self):
        scores = [0.6, 0.8, 1.0]
        result = aggregate_replications(scores)
        assert result["mean"] == pytest.approx(0.8)

    def test_std_of_scores(self):
        scores = [0.5, 0.5, 0.5]
        result = aggregate_replications(scores)
        assert result["std"] == pytest.approx(0.0)

    def test_min_max(self):
        scores = [0.2, 0.5, 0.9]
        result = aggregate_replications(scores)
        assert result["min"] == pytest.approx(0.2)
        assert result["max"] == pytest.approx(0.9)

    def test_single_score(self):
        scores = [0.7]
        result = aggregate_replications(scores)
        assert result["mean"] == pytest.approx(0.7)
        assert result["std"] == pytest.approx(0.0)
        assert result["n"] == 1

    def test_result_keys(self):
        result = aggregate_replications([0.5, 0.6])
        assert "mean" in result
        assert "std" in result
        assert "min" in result
        assert "max" in result
        assert "n" in result
