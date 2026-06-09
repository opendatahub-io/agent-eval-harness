"""Tests for agent_eval.stats — repeated-measures ANOVA, mixed-effects, Pareto frontier."""

import numpy as np
import pandas as pd
import pytest

from agent_eval.stats.anova import (
    mixed_effects_anova,
    one_way_anova,
    repeated_measures_anova,
)
from agent_eval.stats.pareto import pareto_frontier


class TestRepeatedMeasuresAnova:
    """repeated_measures_anova must use case_id as subject (blocking factor)."""

    def _make_repeated_data(self, rng, n_cases=20, effect_size=0.5, case_variance=2.0):
        """Synthetic data: same cases measured under two conditions.

        case_variance controls how much individual case difficulty varies.
        effect_size controls the true difference between models.
        """
        case_effects = rng.normal(0, case_variance, size=n_cases)
        rows = []
        for i in range(n_cases):
            rows.append({
                "case_id": f"case_{i}",
                "model": "model_a",
                "composite": case_effects[i] + rng.normal(0, 0.3),
            })
            rows.append({
                "case_id": f"case_{i}",
                "model": "model_b",
                "composite": case_effects[i] + effect_size + rng.normal(0, 0.3),
            })
        return pd.DataFrame(rows)

    def test_detects_significant_effect(self):
        rng = np.random.default_rng(42)
        df = self._make_repeated_data(rng, n_cases=30, effect_size=1.0, case_variance=3.0)
        result = repeated_measures_anova(df, factor="model")
        assert result["p_value"] < 0.05
        assert result["significant"]

    def test_no_effect_not_significant(self):
        rng = np.random.default_rng(42)
        df = self._make_repeated_data(rng, n_cases=30, effect_size=0.0, case_variance=0.5)
        result = repeated_measures_anova(df, factor="model")
        assert result["p_value"] > 0.05
        assert not result["significant"]

    def test_result_keys(self):
        rng = np.random.default_rng(42)
        df = self._make_repeated_data(rng, n_cases=10, effect_size=0.5)
        result = repeated_measures_anova(df, factor="model")
        assert "f_statistic" in result
        assert "p_value" in result
        assert "significant" in result
        assert "method" in result

    def test_method_is_repeated_measures(self):
        rng = np.random.default_rng(42)
        df = self._make_repeated_data(rng, n_cases=10, effect_size=0.5)
        result = repeated_measures_anova(df, factor="model")
        assert "repeated" in result["method"].lower() or "rm" in result["method"].lower()

    def test_zero_variance_does_not_crash(self):
        """Degenerate data — every cell scores identically (a ceiling/floor
        effect, common with easy cases) — must return a graceful 'no variance'
        result rather than raising KeyError when pingouin omits the F column."""
        rows = []
        for i in range(5):
            for model in ("model_a", "model_b", "model_c"):
                rows.append({"case_id": f"case_{i}", "model": model, "composite": 1.0})
        df = pd.DataFrame(rows)

        result = repeated_measures_anova(df, factor="model")

        assert result["f_statistic"] is None
        assert result["p_value"] is None
        assert result["significant"] is False
        assert "repeated" in result["method"].lower()
        assert "note" in result

    def test_high_case_variance_masks_effect_for_oneway(self):
        """When case variance dominates, one-way ANOVA misses the effect
        but repeated-measures should still detect it."""
        rng = np.random.default_rng(42)
        df = self._make_repeated_data(rng, n_cases=30, effect_size=0.5, case_variance=5.0)

        rm_result = repeated_measures_anova(df, factor="model")

        scores_a = df[df["model"] == "model_a"]["composite"].tolist()
        scores_b = df[df["model"] == "model_b"]["composite"].tolist()
        ow_result = one_way_anova({"model_a": scores_a, "model_b": scores_b}, factor_name="model")

        assert rm_result["p_value"] < ow_result["p_value"]


class TestMixedEffectsAnova:
    """mixed_effects_anova with case_id as random effect."""

    def _make_two_factor_data(self, rng, n_cases=15):
        rows = []
        for i in range(n_cases):
            case_effect = rng.normal(0, 1.0)
            for model in ["a", "b"]:
                for effort in ["low", "high"]:
                    noise = rng.normal(0, 0.2)
                    model_effect = 0.5 if model == "b" else 0.0
                    effort_effect = 0.3 if effort == "high" else 0.0
                    rows.append({
                        "case_id": f"case_{i}",
                        "model": model,
                        "effort": effort,
                        "composite": case_effect + model_effect + effort_effect + noise,
                    })
        return pd.DataFrame(rows)

    def test_runs_with_two_factors(self):
        rng = np.random.default_rng(42)
        df = self._make_two_factor_data(rng)
        result = mixed_effects_anova(df, factors=["model", "effort"])
        assert "p_values" in result
        assert "model" in result["p_values"]

    def test_result_keys(self):
        rng = np.random.default_rng(42)
        df = self._make_two_factor_data(rng)
        result = mixed_effects_anova(df, factors=["model", "effort"])
        assert "method" in result
        assert "coefficients" in result
        assert "p_values" in result

    def test_method_is_mixed_effects(self):
        rng = np.random.default_rng(42)
        df = self._make_two_factor_data(rng)
        result = mixed_effects_anova(df, factors=["model", "effort"])
        assert "mixed" in result["method"].lower()


class TestOneWayAnova:
    """Plain one-way ANOVA — documented as valid only for independent samples."""

    def test_detects_difference(self):
        rng = np.random.default_rng(42)
        a = rng.normal(5.0, 1.0, 30).tolist()
        b = rng.normal(7.0, 1.0, 30).tolist()
        result = one_way_anova({"a": a, "b": b}, factor_name="model")
        assert result["p_value"] < 0.05

    def test_no_difference(self):
        rng = np.random.default_rng(42)
        a = rng.normal(5.0, 1.0, 30).tolist()
        b = rng.normal(5.0, 1.0, 30).tolist()
        result = one_way_anova({"a": a, "b": b}, factor_name="model")
        assert result["p_value"] > 0.05

    def test_result_keys(self):
        result = one_way_anova({"a": [1, 2, 3], "b": [4, 5, 6]}, factor_name="x")
        assert "f_statistic" in result
        assert "p_value" in result
        assert "method" in result

    def test_warns_about_independence(self):
        result = one_way_anova({"a": [1, 2], "b": [3, 4]}, factor_name="x")
        assert "independent" in result["method"].lower() or "one-way" in result["method"].lower()


class TestParetoFrontier:
    """Pareto frontier identification over cost/quality trade-off."""

    def test_single_condition(self):
        conditions = [{"name": "a", "cost": 1.0, "quality": 0.9}]
        frontier = pareto_frontier(conditions, cost_key="cost", quality_key="quality")
        assert len(frontier) == 1

    def test_dominated_excluded(self):
        conditions = [
            {"name": "a", "cost": 1.0, "quality": 0.9},
            {"name": "b", "cost": 2.0, "quality": 0.8},  # dominated: higher cost, lower quality
            {"name": "c", "cost": 0.5, "quality": 0.7},
        ]
        frontier = pareto_frontier(conditions, cost_key="cost", quality_key="quality")
        names = [c["name"] for c in frontier]
        assert "b" not in names
        assert "a" in names
        assert "c" in names

    def test_all_on_frontier(self):
        conditions = [
            {"name": "cheap", "cost": 0.1, "quality": 0.3},
            {"name": "mid", "cost": 0.5, "quality": 0.7},
            {"name": "expensive", "cost": 1.0, "quality": 0.95},
        ]
        frontier = pareto_frontier(conditions, cost_key="cost", quality_key="quality")
        assert len(frontier) == 3

    def test_empty_input(self):
        assert pareto_frontier([], cost_key="cost", quality_key="quality") == []

    def test_preserves_original_data(self):
        conditions = [{"name": "x", "cost": 1.0, "quality": 0.5, "extra": "kept"}]
        frontier = pareto_frontier(conditions, cost_key="cost", quality_key="quality")
        assert frontier[0]["extra"] == "kept"
