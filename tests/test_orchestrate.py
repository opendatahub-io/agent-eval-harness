"""Tests for orchestration scripts — apply_condition, run_cell, design, analyze."""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# Add skills scripts to import path
_scripts_dir = str(Path(__file__).parent.parent / "skills" / "eval-anova" / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from agent_eval.matrix import Condition, MatrixBuilder
from orchestrate import (
    RunResult,
    apply_condition,
    prepare_knowledge_context,
    run_cell,
)
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


class TestApplyCondition:
    """apply_condition maps factor levels to runner/skill kwargs."""

    def test_model_factor(self):
        condition = Condition(condition_id="abc", levels={"model": "claude-sonnet-4-20250514"})
        runner, skill = apply_condition(condition, {})
        assert runner["model"] == "claude-sonnet-4-20250514"

    def test_effort_factor(self):
        condition = Condition(condition_id="abc", levels={"effort": "high"})
        runner, skill = apply_condition(condition, {})
        assert skill["effort"] == "high"

    def test_mixed_factors(self):
        condition = Condition(
            condition_id="abc", levels={"model": "a", "effort": "low", "temp": 0.5}
        )
        runner, skill = apply_condition(condition, {})
        assert runner["model"] == "a"
        assert skill["effort"] == "low"
        assert skill["temp"] == 0.5

    def test_preserves_existing_config(self):
        condition = Condition(condition_id="abc", levels={"model": "b"})
        config = {"runner_kwargs": {"timeout": 30}, "run_skill_kwargs": {"verbose": True}}
        runner, skill = apply_condition(condition, config)
        assert runner["model"] == "b"
        assert runner["timeout"] == 30
        assert skill["verbose"] is True


class TestPrepareKnowledgeContext:
    """prepare_knowledge_context reads .knowledge/{level}/ markdown files."""

    def test_returns_none_for_no_level(self, tmp_path):
        assert prepare_knowledge_context(tmp_path, level=None) is None

    def test_returns_none_for_missing_dir(self, tmp_path):
        assert prepare_knowledge_context(tmp_path, level="full") is None

    def test_reads_markdown_files(self, tmp_path):
        ctx_dir = tmp_path / ".knowledge" / "basic"
        ctx_dir.mkdir(parents=True)
        (ctx_dir / "01-intro.md").write_text("# Intro")
        (ctx_dir / "02-rules.md").write_text("# Rules")
        result = prepare_knowledge_context(tmp_path, level="basic")
        assert "# Intro" in result
        assert "# Rules" in result


class TestRunCell:
    """run_cell executes one condition × case × replication."""

    def test_basic_execution(self):
        condition = Condition(condition_id="c1", levels={"model": "test"})

        def mock_run(case_id, **kwargs):
            return {"pass": True, "score": 0.8}

        judge_configs = {
            "pass": {"type": "boolean", "gate": True},
            "score": {"type": "numeric", "weight": 1.0},
        }

        result = run_cell(
            condition=condition,
            case_id="case_1",
            replication=0,
            eval_config={},
            judge_configs=judge_configs,
            run_fn=mock_run,
        )

        assert isinstance(result, RunResult)
        assert result.case_id == "case_1"
        assert result.replication == 0
        assert result.composite == pytest.approx(0.8)

    def test_requires_run_fn(self):
        condition = Condition(condition_id="c1", levels={"model": "test"})
        with pytest.raises(ValueError, match="run_fn"):
            run_cell(
                condition=condition,
                case_id="case_1",
                replication=0,
                eval_config={},
                judge_configs={},
            )


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
