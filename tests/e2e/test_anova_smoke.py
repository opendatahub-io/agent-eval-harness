"""End-to-end smoke test: 2 models × 2 cases × 2 reps with mock runner."""

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

# Add scripts to path
_scripts_dir = str(Path(__file__).parent.parent.parent / "skills" / "eval-anova" / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from agent_eval.composite import aggregate_replications, composite_score
from agent_eval.matrix import Condition, MatrixBuilder
from agent_eval.stats.anova import repeated_measures_anova
from agent_eval.stats.pareto import pareto_frontier
from orchestrate import RunResult, apply_condition, run_cell
from design import design_experiment
from analyze import analyze_experiment, build_results_dataframe


class TestEndToEndSmoke:
    """Full pipeline: design → run → score → analyze."""

    @pytest.fixture
    def eval_yaml(self, tmp_path):
        config = {
            "matrix": {
                "factors": {
                    "model": ["model_good", "model_bad"],
                },
                "replications": 2,
            }
        }
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))
        return p

    @pytest.fixture
    def cases(self):
        return ["case_easy", "case_hard"]

    @pytest.fixture
    def judge_configs(self):
        return {
            "correctness": {"type": "boolean", "gate": True},
            "quality": {"type": "numeric", "weight": 2.0},
            "style": {"type": "numeric", "weight": 1.0},
        }

    def _mock_runner(self, case_id, model="", **kwargs):
        """Deterministic mock: good model scores higher, hard cases score lower."""
        rng = np.random.default_rng(hash((case_id, model)) % (2**32))

        base_quality = 0.8 if model == "model_good" else 0.4
        case_penalty = 0.2 if case_id == "case_hard" else 0.0
        noise = rng.normal(0, 0.05)

        return {
            "correctness": model == "model_good" or case_id == "case_easy",
            "quality": max(0, min(1, base_quality - case_penalty + noise)),
            "style": max(0, min(1, base_quality - case_penalty * 0.5 + noise)),
        }

    def test_full_pipeline(self, eval_yaml, cases, judge_configs):
        design = design_experiment(eval_yaml, n_cases=len(cases))
        config = design["config"]
        conditions = design["conditions"]

        assert len(conditions) == 2
        assert config.replications == 2

        run_results = []
        for condition in conditions:
            for case_id in cases:
                for rep in range(config.replications):
                    result = run_cell(
                        condition=condition,
                        case_id=case_id,
                        replication=rep,
                        eval_config={},
                        judge_configs=judge_configs,
                        run_fn=self._mock_runner,
                    )
                    run_results.append(result)

        assert len(run_results) == 8  # 2 models * 2 cases * 2 reps

        for r in run_results:
            assert 0.0 <= r.composite <= 1.0

        df = build_results_dataframe(run_results)
        assert len(df) == 8
        assert "case_id" in df.columns
        assert "model" in df.columns
        assert "composite" in df.columns

        analysis = analyze_experiment(run_results, factors=["model"])

        assert "anova" in analysis
        assert "condition_summaries" in analysis
        assert analysis["n_runs"] == 8
        assert analysis["n_conditions"] == 2

        assert "p_value" in analysis["anova"]
        assert "f_statistic" in analysis["anova"]

    def test_good_model_scores_higher(self, eval_yaml, cases, judge_configs):
        """Verify the mock produces expected ordering."""
        design = design_experiment(eval_yaml, n_cases=len(cases))

        run_results = []
        for condition in design["conditions"]:
            for case_id in cases:
                for rep in range(design["config"].replications):
                    result = run_cell(
                        condition=condition,
                        case_id=case_id,
                        replication=rep,
                        eval_config={},
                        judge_configs=judge_configs,
                        run_fn=self._mock_runner,
                    )
                    run_results.append(result)

        good_scores = [r.composite for r in run_results if r.condition.levels["model"] == "model_good"]
        bad_scores = [r.composite for r in run_results if r.condition.levels["model"] == "model_bad"]

        assert np.mean(good_scores) > np.mean(bad_scores)

    def test_aggregation(self, eval_yaml, cases, judge_configs):
        """Verify replication aggregation produces valid statistics."""
        design = design_experiment(eval_yaml, n_cases=len(cases))

        run_results = []
        for condition in design["conditions"]:
            for case_id in cases:
                for rep in range(design["config"].replications):
                    result = run_cell(
                        condition=condition,
                        case_id=case_id,
                        replication=rep,
                        eval_config={},
                        judge_configs=judge_configs,
                        run_fn=self._mock_runner,
                    )
                    run_results.append(result)

        for condition in design["conditions"]:
            scores = [r.composite for r in run_results if r.condition == condition]
            agg = aggregate_replications(scores)
            assert agg["n"] == len(cases) * design["config"].replications
            assert agg["min"] <= agg["mean"] <= agg["max"]
