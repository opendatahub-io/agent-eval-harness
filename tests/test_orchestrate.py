"""Tests for eval-anova analysis + design helpers (analyze_experiment, design)."""

import sys
from pathlib import Path

import pytest
import yaml

# Add skills scripts to import path
_scripts_dir = str(Path(__file__).parent.parent / "skills" / "eval-anova" / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from agent_eval.anova.matrix import Condition, MatrixBuilder
from orchestrate import RunResult
from design import design_experiment, print_design_summary
from analyze import analyze_experiment


class TestAnalyzeReportSchema:
    """analyze_experiment must emit a report-ready document that report.py
    can render directly (flat factor keys + design + per_case blocks)."""

    def _results(self):
        models = ["claude-opus-4-6", "claude-haiku-4-5"]
        cases = ["fizzbuzz", "binary-search"]
        scores = {"claude-opus-4-6": 1.0, "claude-haiku-4-5": 0.5}
        out = []
        for m in models:
            cond = Condition(condition_id=m, levels={"model": m})
            for c in cases:
                out.append(RunResult(condition=cond, case_id=c, replication=0,
                                     judge_results={"correct": scores[m] == 1.0},
                                     composite=scores[m], metadata={}))
        return out

    def test_condition_summaries_have_flat_model(self):
        a = analyze_experiment(self._results(), factors=["model"])
        for cs in a["condition_summaries"]:
            assert cs.get("model") in ("claude-opus-4-6", "claude-haiku-4-5")
            assert "levels" in cs  # nested form preserved for back-compat

    def test_design_block(self):
        a = analyze_experiment(self._results(), factors=["model"])
        des = a["design"]
        assert des["n_cases"] == 2
        assert des["replications"] == 1
        assert set(des["factors"]["model"]) == {"claude-opus-4-6", "claude-haiku-4-5"}

    def test_per_case_keyed_by_model(self):
        a = analyze_experiment(self._results(), factors=["model"])
        per = a["per_case"]
        assert per["claude-opus-4-6"]["fizzbuzz"] == 1.0
        assert per["claude-haiku-4-5"]["binary-search"] == 0.5

    def test_excludes_cases_missing_from_a_condition(self):
        """A case absent from one condition (e.g. a fault-tolerant driver
        dropped a failed cell) must be excluded from the analysis and reported,
        not silently listwise-deleted while n_cases still claims the full set."""
        cases = ["fizzbuzz", "binary-search", "sort"]
        out = []
        for m in ["claude-opus-4-6", "claude-haiku-4-5"]:
            cond = Condition(condition_id=m, levels={"model": m})
            for c in cases:
                if m == "claude-haiku-4-5" and c == "sort":
                    continue  # missing under one condition
                out.append(RunResult(condition=cond, case_id=c, replication=0,
                                     judge_results={"correct": True},
                                     composite=1.0 if c != "sort" else 0.0,
                                     metadata={}))
        a = analyze_experiment(out, factors=["model"])
        assert a["excluded_cases"] == ["sort"]
        assert a["design"]["n_cases"] == 2
        assert a["design"]["excluded_cases"] == ["sort"]

    def test_multi_factor_per_case_keeps_full_condition(self, monkeypatch):
        from agent_eval.anova.stats import anova as anova_mod

        def mixed_effects_stub(df, factors, alpha=0.05):
            return {
                "p_values": {"model": 0.01, "effort": 0.2},
                "significant": {"model": True, "effort": False},
                "method": "stub",
                "alpha": alpha,
                "factors": factors,
            }

        monkeypatch.setattr(anova_mod, "mixed_effects_anova", mixed_effects_stub)

        results = []
        for model in ["claude-opus-4-6", "claude-haiku-4-5"]:
            for effort, score in [("low", 0.25), ("high", 0.75)]:
                cond = Condition(
                    condition_id=f"{model}-{effort}",
                    levels={"model": model, "effort": effort},
                )
                results.append(RunResult(condition=cond, case_id="case-a", replication=0,
                                         judge_results={"score": score},
                                         composite=score, metadata={}))

        a = analyze_experiment(results, factors=["model", "effort"])
        per = a["per_case"]

        assert per["model=claude-opus-4-6, effort=low"]["case-a"] == 0.25
        assert per["model=claude-opus-4-6, effort=high"]["case-a"] == 0.75


class TestDesignExperiment:
    """design_experiment loads config and expands matrix."""

    def test_basic_design(self, tmp_path):
        config = {
            "matrix": {
                "factors": {"model": ["a", "b"], "effort": ["low", "high"]},
                "replications": 2,
            }
        }
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))

        design = design_experiment(p, n_cases=5, avg_cost_per_run=0.10)

        assert len(design["conditions"]) == 4
        assert design["cost_estimate"]["total_runs"] == 40  # 4 * 5 * 2
        assert "experiment_id" in design

    def test_no_matrix_raises(self, tmp_path):
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump({"cases": []}))
        with pytest.raises(ValueError):
            design_experiment(p)


class TestPrintDesignSummary:
    """print_design_summary formats readable output."""

    def test_contains_key_info(self, tmp_path):
        config = {
            "matrix": {
                "factors": {"model": ["a", "b"]},
                "replications": 1,
            }
        }
        p = tmp_path / "eval.yaml"
        p.write_text(yaml.dump(config))

        design = design_experiment(p, n_cases=3)
        summary = print_design_summary(design)
        assert "model" in summary
        assert "Conditions: 2" in summary
