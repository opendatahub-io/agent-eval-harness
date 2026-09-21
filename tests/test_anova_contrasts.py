"""Tests for post-hoc pairwise level contrasts (Holm-corrected within factor).

Both computation paths — pingouin paired tests for the single-factor design
and contrast vectors on the fitted mixedlm for multi-factor designs — must
emit the same schema, correct the family within each factor only, and never
fabricate a p-value for a degenerate pair.
"""

import json
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pingouin as pg
import pytest
import yaml

from agent_eval.anova.stats.anova import (
    mixed_effects_anova,
    repeated_measures_anova,
)

_scripts_dir = str(Path(__file__).parent.parent / "skills" / "eval-anova" / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from analyze import analyze_runs  # noqa: E402

BLOCK_KEYS = {"correction", "family", "family_size", "contrast_type",
              "omnibus_p_adjusted", "note", "pairs"}
PAIR_KEYS = {"a", "b", "estimate", "se", "p_raw", "p_adjusted", "significant"}


def _three_level_data(rng, n_cases=20, effects=(0.0, 0.05, 0.6), noise=0.25):
    """Same cases under levels a/b/c with known level effects."""
    rows = []
    for i in range(n_cases):
        case_effect = rng.normal(0, 1.0)
        for level, effect in zip(("a", "b", "c"), effects):
            rows.append({
                "case_id": f"case_{i}",
                "model": level,
                "composite": case_effect + effect + rng.normal(0, noise),
            })
    return pd.DataFrame(rows)


def _two_factor_data(rng, n_cases=15):
    """model (a/b/c, effects 0/0.05/0.6) × context (low/high, +0.3)."""
    rows = []
    for i in range(n_cases):
        case_effect = rng.normal(0, 1.0)
        for model, m_eff in [("a", 0.0), ("b", 0.05), ("c", 0.6)]:
            for context, c_eff in [("low", 0.0), ("high", 0.3)]:
                rows.append({
                    "case_id": f"case_{i}",
                    "model": model,
                    "context": context,
                    "composite": case_effect + m_eff + c_eff + rng.normal(0, 0.25),
                })
    return pd.DataFrame(rows)


class TestSingleFactorContrasts:
    """repeated_measures_anova → pingouin paired tests, Holm within factor."""

    def test_three_levels_yield_three_pairs(self):
        rng = np.random.default_rng(42)
        result = repeated_measures_anova(_three_level_data(rng), factor="model")
        block = result["contrasts"]["model"]
        assert [(p["a"], p["b"]) for p in block["pairs"]] == [
            ("a", "b"), ("a", "c"), ("b", "c")]
        assert block["family_size"] == 3
        assert block["correction"] == "holm"
        assert "within factor 'model'" in block["family"]

    def test_holm_adjustment_matches_pingouin(self):
        """Our within-factor Holm must reproduce pingouin's own padjust='holm'
        on a clean design (the shared path only diverges on degenerate pairs,
        which pingouin would count in the family)."""
        rng = np.random.default_rng(42)
        df = _three_level_data(rng)
        result = repeated_measures_anova(df, factor="model")
        pairs = result["contrasts"]["model"]["pairs"]

        pw = pg.pairwise_tests(data=df, dv="composite", within="model",
                               subject="case_id", padjust="holm")
        expected = {frozenset({r["A"], r["B"]}): float(r["p_corr"])
                    for _, r in pw.iterrows()}
        for p in pairs:
            assert p["p_adjusted"] == pytest.approx(
                expected[frozenset({p["a"], p["b"]})])
            assert p["p_adjusted"] >= p["p_raw"] - 1e-15

    def test_estimates_match_constructed_deltas(self):
        rng = np.random.default_rng(42)
        result = repeated_measures_anova(
            _three_level_data(rng, n_cases=30), factor="model")
        est = {(p["a"], p["b"]): p["estimate"]
               for p in result["contrasts"]["model"]["pairs"]}
        # constructed effects: a=0, b=0.05, c=0.6 → a−c ≈ −0.6, b−c ≈ −0.55
        assert est[("a", "c")] == pytest.approx(-0.6, abs=0.2)
        assert est[("b", "c")] == pytest.approx(-0.55, abs=0.2)
        # the estimate is the paired mean difference on the composite scale
        assert est[("a", "c")] < 0 and est[("b", "c")] < 0

    def test_omnibus_context_carried_without_gating(self):
        """The factor block carries the omnibus adjusted p for context, but a
        non-significant omnibus does not suppress the contrasts."""
        rng = np.random.default_rng(42)
        df = _three_level_data(rng, effects=(0.0, 0.0, 0.0), noise=0.5)
        result = repeated_measures_anova(df, factor="model")
        block = result["contrasts"]["model"]
        assert block["omnibus_p_adjusted"] == result["p_adjusted"]
        assert not result["significant"]
        assert len(block["pairs"]) == 3  # still computed

    def test_constant_response_excluded_with_reason(self):
        """Degenerate factor (2 conditions, constant response): no pairwise
        test exists — the block says why and the family is empty, never a
        fabricated p."""
        rows = [{"case_id": f"case_{i}", "model": m, "composite": 1.0}
                for i in range(5) for m in ("a", "b")]
        result = repeated_measures_anova(pd.DataFrame(rows), factor="model")
        block = result["contrasts"]["model"]
        assert block["pairs"] == []
        assert block["family_size"] == 0
        assert "reason" in block

    def test_perfect_separation_pair_has_estimate_but_no_p(self):
        """Zero-variance paired differences drive the t to ±inf, which scipy
        renders as p=0.0 — a fabricated certainty. The observed difference is
        reported; the p is not, and the pair leaves the family."""
        rows = []
        for i in range(6):
            rows.append({"case_id": f"case_{i}", "model": "m_a", "composite": 1.0})
            rows.append({"case_id": f"case_{i}", "model": "m_b", "composite": 0.0})
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # scipy precision-loss chatter
            result = repeated_measures_anova(pd.DataFrame(rows), factor="model")
        [pair] = result["contrasts"]["model"]["pairs"]
        assert pair["estimate"] == pytest.approx(1.0)
        assert pair["p_raw"] is None and pair["p_adjusted"] is None
        assert pair["significant"] is False and "reason" in pair
        assert result["contrasts"]["model"]["family_size"] == 0


class TestMixedlmContrasts:
    """mixed_effects_anova → contrast vectors on the fitted model."""

    def test_pairs_per_factor_and_within_factor_families(self):
        rng = np.random.default_rng(42)
        result = mixed_effects_anova(_two_factor_data(rng),
                                     factors=["model", "context"])
        contrasts = result["contrasts"]
        assert set(contrasts) == {"model", "context"}  # main effects only
        assert len(contrasts["model"]["pairs"]) == 3
        assert len(contrasts["context"]["pairs"]) == 1
        # families are per factor, never pooled: context's lone contrast is a
        # family of one, so its adjusted p equals its raw p even though four
        # contrasts exist overall
        assert contrasts["model"]["family_size"] == 3
        assert contrasts["context"]["family_size"] == 1
        [ctx_pair] = contrasts["context"]["pairs"]
        assert ctx_pair["p_adjusted"] == pytest.approx(ctx_pair["p_raw"])

    def test_estimates_are_coefficient_differences_from_the_fit(self):
        """Level-vs-reference is a single coefficient; level A vs level B the
        coefficient difference — exactly, since no refitting happens."""
        rng = np.random.default_rng(42)
        result = mixed_effects_anova(_two_factor_data(rng),
                                     factors=["model", "context"])
        est = {(p["a"], p["b"]): p["estimate"]
               for p in result["contrasts"]["model"]["pairs"]}
        coef_b = result["coefficients"]["C(model)[T.b]"]
        coef_c = result["coefficients"]["C(model)[T.c]"]
        assert est[("a", "b")] == pytest.approx(-coef_b)
        assert est[("a", "c")] == pytest.approx(-coef_c)
        assert est[("b", "c")] == pytest.approx(coef_b - coef_c)
        # and the signs/magnitudes track the constructed effects (0/0.05/0.6)
        assert est[("a", "c")] == pytest.approx(-0.6, abs=0.2)

    def test_holm_ordering_within_factor(self):
        """Holm: sorted raw p × (m, m−1, …) with a running max — adjusted
        values are at least raw and preserve the raw ordering."""
        rng = np.random.default_rng(42)
        result = mixed_effects_anova(_two_factor_data(rng),
                                     factors=["model", "context"])
        pairs = sorted(result["contrasts"]["model"]["pairs"],
                       key=lambda p: p["p_raw"])
        m = len(pairs)
        running = 0.0
        for rank, p in enumerate(pairs):
            running = max(running, min(1.0, (m - rank) * p["p_raw"]))
            assert p["p_adjusted"] == pytest.approx(running)

    def test_interactions_flagged_as_reference_cell(self):
        rng = np.random.default_rng(42)
        result = mixed_effects_anova(_two_factor_data(rng),
                                     factors=["model", "context"])
        block = result["contrasts"]["model"]
        assert block["contrast_type"] == "reference-cell"
        assert "not marginal means" in block["note"].lower()
        assert block["omnibus_p_adjusted"] == result["p_adjusted"]["model"]

    def test_single_factor_model_is_marginal_not_reference_cell(self):
        rng = np.random.default_rng(42)
        result = mixed_effects_anova(_three_level_data(rng), factors=["model"])
        assert result["contrasts"]["model"]["contrast_type"] == "marginal"

    def test_same_schema_as_single_factor_path(self):
        """(c) the pingouin and mixedlm paths must emit interchangeable
        shapes, so renderers need no path-specific handling."""
        rng = np.random.default_rng(42)
        rm = repeated_measures_anova(_three_level_data(rng), factor="model")
        ml = mixed_effects_anova(_two_factor_data(rng),
                                 factors=["model", "context"])
        rm_block = rm["contrasts"]["model"]
        ml_block = ml["contrasts"]["model"]
        assert set(rm_block) == set(ml_block) == BLOCK_KEYS
        for pair in rm_block["pairs"] + ml_block["pairs"]:
            assert set(pair) == PAIR_KEYS  # no reason on clean pairs

    def test_degenerate_fit_excludes_pairs_not_fabricates(self):
        """Constant response: the singular fit yields no usable pair test;
        estimates may be reported but every p is None and the family empty."""
        rows = [{"case_id": f"case_{i}", "model": m, "context": c, "composite": 1.0}
                for i in range(6) for m in ("a", "b") for c in ("low", "high")]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # mixedlm convergence chatter
            result = mixed_effects_anova(pd.DataFrame(rows),
                                         factors=["model", "context"])
        for block in result["contrasts"].values():
            assert block["family_size"] == 0
            for pair in block["pairs"]:
                assert pair["p_adjusted"] is None
                assert pair["significant"] is False


class TestContrastsSerialization:
    """(e) contrasts land in anova.json as a top-level key, JSON-clean."""

    @staticmethod
    def _mk_run(runs_dir, run_id, model, scores):
        rd = runs_dir / run_id
        rd.mkdir(parents=True)
        per_case = {c: {"quality": {"value": s, "judge_type": "numeric"}}
                    for c, s in scores.items()}
        (rd / "summary.yaml").write_text(yaml.dump(
            {"run_id": run_id, "per_case": per_case}))
        (rd / "run_result.json").write_text(json.dumps(
            {"model": model, "cost_usd": 0.1}))

    def test_contrasts_serialized_to_anova_json(self, tmp_path):
        runs = tmp_path / "eval"
        self._mk_run(runs, "r-a", "model-a", {"c1": 5, "c2": 4, "c3": 5, "c4": 3})
        self._mk_run(runs, "r-b", "model-b", {"c1": 3, "c2": 2, "c3": 4, "c4": 1})
        analysis, artifact = analyze_runs(runs, NS(reward=None))

        data = json.loads(artifact.read_text())
        block = data["contrasts"]["model"]
        assert set(block) >= {"correction", "family_size", "contrast_type",
                              "omnibus_p_adjusted", "pairs"}
        [pair] = block["pairs"]
        assert pair["a"] == "model-a" and pair["b"] == "model-b"
        assert isinstance(pair["estimate"], float)
        assert isinstance(pair["p_raw"], float)
        assert isinstance(pair["significant"], bool)
        # lifted to the artifact top level, not left inside the omnibus result
        assert "contrasts" not in data["anova"]

    def test_skipped_anova_serializes_empty_contrasts(self, tmp_path):
        runs = tmp_path / "eval"
        self._mk_run(runs, "r-a", "model-a", {"c1": 5, "c2": 4})
        _, artifact = analyze_runs(runs, NS(reward=None))
        data = json.loads(artifact.read_text())
        assert data["contrasts"] == {}  # single condition → nothing to contrast
