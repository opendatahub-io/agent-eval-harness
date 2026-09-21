"""Tests for agent_eval.anova.stats — repeated-measures ANOVA, mixed-effects, Pareto frontier."""

import shlex
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import statsmodels.formula.api as smf

import agent_eval.anova.stats
from agent_eval.anova.stats import missing_deps_message

from agent_eval.anova.stats.anova import (
    adjust_term_p_values,
    mixed_effects_anova,
    one_way_anova,
    repeated_measures_anova,
)
from agent_eval.anova.stats.pareto import pareto_frontier


def _install_argv(msg):
    """The argv a shell would actually build from the pip line in `msg`."""
    # Not just any line mentioning pip — the prose above it does too. The command
    # is the indented one invoking the interpreter directly.
    line = next(l for l in msg.splitlines() if " -m pip install -e " in l)
    return shlex.split(line)


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

    def test_perfect_separation_is_degenerate_not_infinitely_significant(self):
        """Perfect separation (one condition passes every case, the other fails
        every case) drives the within-subject residual to ~0, so pingouin
        returns a non-finite F. That must be reported as a degenerate design,
        not as an 'infinitely significant' result."""
        rows = []
        for i in range(6):
            rows.append({"case_id": f"case_{i}", "model": "model_a", "composite": 1.0})
            rows.append({"case_id": f"case_{i}", "model": "model_b", "composite": 0.0})
        result = repeated_measures_anova(pd.DataFrame(rows), factor="model")

        assert result["f_statistic"] is None
        assert result["p_value"] is None
        assert result["significant"] is False
        assert "note" in result

    def test_consistent_effect_is_degenerate_not_negative_f(self):
        """A perfectly consistent effect (condition B always a fixed amount
        above A) also yields a ~0 residual and a nonsensical negative F; report
        it as degenerate rather than emitting the negative F and p=1.0."""
        rows = []
        for i in range(6):
            base = i * 0.1
            rows.append({"case_id": f"case_{i}", "model": "model_a", "composite": base})
            rows.append({"case_id": f"case_{i}", "model": "model_b", "composite": base + 0.1})
        result = repeated_measures_anova(pd.DataFrame(rows), factor="model")

        assert result["f_statistic"] is None
        assert result["significant"] is False

    def test_multiplicity_fields_are_a_family_of_one(self):
        """One factor = one test: p_adjusted equals p_value under any method;
        the fields exist so downstream consumers see one result shape."""
        rng = np.random.default_rng(42)
        df = self._make_repeated_data(rng, n_cases=10, effect_size=0.5)
        result = repeated_measures_anova(df, factor="model")
        assert result["p_adjusted"] == result["p_value"]
        assert result["correction"] == "holm"
        assert result["family_size"] == 1

    def test_degenerate_design_has_empty_family(self):
        """No test ran, so the family must be empty — not a family of one with
        a fabricated member."""
        rows = [{"case_id": f"case_{i}", "model": m, "composite": 1.0}
                for i in range(5) for m in ("model_a", "model_b")]
        result = repeated_measures_anova(pd.DataFrame(rows), factor="model")
        assert result["p_adjusted"] is None
        assert result["family_size"] == 0
        assert result["excluded_terms"] == ["model"]
        assert result["correction"] == "holm"

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

    def _make_three_level_data(self, rng, n_cases=12):
        """One factor, three levels: b barely differs from a, c clearly does.

        The old min-over-dummies shortcut reported the most extreme dummy's p
        as the factor p; the joint 2-df Wald test answers the omnibus question
        ("does the factor matter at all") instead.
        """
        rows = []
        for i in range(n_cases):
            case_effect = rng.normal(0, 1.0)
            for model, effect in [("a", 0.0), ("b", 0.05), ("c", 0.6)]:
                rows.append({
                    "case_id": f"case_{i}",
                    "model": model,
                    "composite": case_effect + effect + rng.normal(0, 0.3),
                })
        return pd.DataFrame(rows)

    def test_result_keys(self):
        rng = np.random.default_rng(42)
        df = self._make_two_factor_data(rng)
        result = mixed_effects_anova(df, factors=["model", "effort"])
        assert "method" in result
        assert "coefficients" in result
        assert "p_values" in result
        assert "p_adjusted" in result
        assert "correction" in result
        assert "family_size" in result

    def test_method_is_mixed_effects(self):
        rng = np.random.default_rng(42)
        df = self._make_two_factor_data(rng)
        result = mixed_effects_anova(df, factors=["model", "effort"])
        assert "mixed" in result["method"].lower()

    def test_term_p_is_joint_wald_with_readable_names(self, monkeypatch):
        """Per-term p-values come from the Wald term table, with patsy names
        unwrapped (C(model) -> model, C(a):C(b) -> a:b) and the Intercept
        dropped — not cherry-picked from dummy coefficient p-values (which
        also removes the old substring-matching hazard: model vs model_size)."""
        class FakeWald:
            table = pd.DataFrame(
                {"pvalue": [0.9, 0.03, 0.001, 0.002]},
                index=[
                    "Intercept",
                    "C(model)",
                    "C(model_size)",
                    "C(model):C(model_size)",
                ],
            )

        class FakeFit:
            fe_params = pd.Series(
                [0.0, 0.1, 0.2, 0.3],
                index=[
                    "Intercept",
                    "C(model)[T.b]",
                    "C(model_size)[T.large]",
                    "C(model)[T.b]:C(model_size)[T.large]",
                ],
            )
            pvalues = pd.Series([0.9, 0.5, 0.4, 0.3], index=fe_params.index)
            aic = 1.0
            bic = 2.0

            def wald_test_terms(self, scalar=True):
                return FakeWald()

        class FakeModel:
            def fit(self, reml=True):
                return FakeFit()

        monkeypatch.setattr(
            "agent_eval.anova.stats.anova.smf.mixedlm",
            lambda formula, data, groups: FakeModel(),
        )
        df = pd.DataFrame({"case_id": ["c1"], "model": ["a"], "composite": [1.0]})

        result = mixed_effects_anova(df, factors=["model", "model_size"])

        assert result["p_values"] == {
            "model": 0.03, "model_size": 0.001, "model:model_size": 0.002}
        # coefficient-level detail is preserved for transparency, not reused
        # as the factor p
        assert result["all_p_values"]["C(model)[T.b]"] == 0.5

    def test_three_level_factor_gets_joint_wald_not_min_dummy(self):
        rng = np.random.default_rng(7)
        df = self._make_three_level_data(rng)
        result = mixed_effects_anova(df, factors=["model"])

        fit = smf.mixedlm("composite ~ C(model)", data=df,
                          groups=df["case_id"]).fit(reml=True)
        joint = float(
            fit.wald_test_terms(scalar=True).table.loc["C(model)", "pvalue"])
        min_dummy = min(
            float(fit.pvalues[k]) for k in fit.fe_params.index
            if "C(model)" in k and ":" not in k)

        assert result["p_values"]["model"] == pytest.approx(joint, rel=1e-9)
        # the two statistics genuinely differ on this design, so the equality
        # above proves the joint test is what is being reported
        assert joint != pytest.approx(min_dummy, rel=1e-3)
        assert result["p_values"]["model"] != pytest.approx(min_dummy, rel=1e-3)

    def test_interaction_terms_reported(self):
        rng = np.random.default_rng(42)
        df = self._make_two_factor_data(rng)
        result = mixed_effects_anova(df, factors=["model", "effort"])
        for key in ("p_values", "p_adjusted", "significant"):
            assert "model:effort" in result[key]

    def test_holm_adjusts_across_main_effects_and_interaction(self):
        rng = np.random.default_rng(42)
        df = self._make_two_factor_data(rng)
        result = mixed_effects_anova(df, factors=["model", "effort"])
        assert result["correction"] == "holm"
        assert result["family_size"] == 3  # model, effort, model:effort
        for term, raw in result["p_values"].items():
            adj = result["p_adjusted"][term]
            assert adj >= raw - 1e-15
            if result["significant"][term]:
                assert adj <= result["alpha"]

    def test_fdr_bh_correction_supported(self):
        rng = np.random.default_rng(42)
        df = self._make_two_factor_data(rng)
        result = mixed_effects_anova(df, factors=["model", "effort"],
                                     correction="fdr_bh")
        assert result["correction"] == "bh"  # canonical name in the artifact
        for term, raw in result["p_values"].items():
            assert result["p_adjusted"][term] >= raw - 1e-15

    def test_correction_none_preserves_raw_semantics(self):
        rng = np.random.default_rng(42)
        df = self._make_two_factor_data(rng)
        result = mixed_effects_anova(df, factors=["model", "effort"],
                                     correction="none")
        assert result["correction"] == "none"
        assert result["p_adjusted"] == pytest.approx(result["p_values"])
        for term, p in result["p_values"].items():
            assert result["significant"][term] == (p < result["alpha"])

    def test_degenerate_fit_reports_no_tests_not_fabricated_p(self):
        """Constant response: statsmodels cannot produce any Wald test. Every
        term must come back None and the family must be empty — a p-value is
        never fabricated for a degenerate design."""
        rows = [{"case_id": f"case_{i}", "model": m, "effort": e, "composite": 1.0}
                for i in range(6) for m in ("a", "b") for e in ("low", "high")]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # mixedlm convergence chatter on a singular fit
            result = mixed_effects_anova(pd.DataFrame(rows),
                                         factors=["model", "effort"])
        assert set(result["p_values"]) == {"model", "effort", "model:effort"}
        assert all(p is None for p in result["p_values"].values())
        assert all(p is None for p in result["p_adjusted"].values())
        assert not any(result["significant"].values())
        assert result["family_size"] == 0
        assert result["excluded_terms"] == ["effort", "model", "model:effort"]
        assert "note" in result


class TestAdjustTermPValues:
    """Multiplicity correction across one model's family of term tests."""

    def test_holm_exact_values_monotone_and_at_least_raw(self):
        raw = {"a": 0.01, "b": 0.02, "c": 0.03}
        adjusted, significant, family = adjust_term_p_values(raw)
        assert family == 3
        # Holm: sorted p × (m, m-1, ...), then a running max keeps it monotone
        assert adjusted == pytest.approx({"a": 0.03, "b": 0.04, "c": 0.04})
        assert all(adjusted[t] >= raw[t] for t in raw)
        assert adjusted["a"] <= adjusted["b"] <= adjusted["c"]
        assert significant == {"a": True, "b": True, "c": True}

    def test_none_entries_are_excluded_from_the_family(self):
        adjusted, significant, family = adjust_term_p_values(
            {"a": 0.01, "b": None, "c": 0.04})
        assert family == 2
        # b's missing test neither gets a value nor inflates the others:
        # a is adjusted ×2 (the real family), not ×3
        assert adjusted["a"] == pytest.approx(0.02)
        assert adjusted["c"] == pytest.approx(0.04)
        assert adjusted["b"] is None
        assert significant["b"] is False

    def test_bh_option(self):
        adjusted, _, family = adjust_term_p_values(
            {"a": 0.01, "b": 0.04}, correction="fdr_bh")
        assert family == 2
        assert adjusted == pytest.approx({"a": 0.02, "b": 0.04})

    def test_correction_none_is_identity_on_raw(self):
        adjusted, significant, family = adjust_term_p_values(
            {"a": 0.03, "b": 0.06}, correction="none", alpha=0.05)
        assert adjusted == {"a": 0.03, "b": 0.06}
        assert significant == {"a": True, "b": False}
        assert family == 2

    def test_unknown_correction_rejected(self):
        with pytest.raises(ValueError, match="correction"):
            adjust_term_p_values({"a": 0.01}, correction="bonferroni-ish")

    def test_none_correction_value_rejected_not_coerced(self):
        # str(None) == "None" would alias to "none" and silently disable the
        # correction — unset is the caller's decision, never this helper's.
        with pytest.raises(ValueError, match="correction"):
            adjust_term_p_values({"a": 0.01}, correction=None)


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

    def test_multiplicity_fields_are_a_family_of_one(self):
        result = one_way_anova({"a": [1.0, 2, 3], "b": [4.0, 5, 6]}, factor_name="x")
        assert result["p_adjusted"] == result["p_value"]
        assert result["correction"] == "holm"
        assert result["family_size"] == 1

    def test_zero_variance_yields_no_test_not_a_nan_p(self):
        """f_oneway returns NaN on constant input; that is no test at all, so
        it must not enter the family or be compared against alpha."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # scipy ConstantInputWarning
            result = one_way_anova({"a": [1.0, 1, 1], "b": [1.0, 1, 1]},
                                   factor_name="x")
        assert result["p_value"] is None
        assert result["p_adjusted"] is None
        assert result["significant"] is False
        assert result["family_size"] == 0
        assert result["excluded_terms"] == ["x"]
        assert "note" in result


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


class TestMissingDepsMessage:
    """The `anova` extra has to land in the interpreter the harness runs under.

    `.eval-venv` is provisioned by ensure_deps.py with pyyaml/mlflow/anthropic/
    jinja2 only, so a plain `pip install -e ".[anova]"` — which targets whatever
    environment is active — routinely installs into the wrong python. The message
    must therefore name the interpreter, not just the extra.
    """

    def test_command_targets_the_interpreter_the_harness_imports_from(self):
        """Not necessarily sys.executable: on the common ABI-*match* path bootstrap
        only patches sys.path, so the launcher is NOT where the extra must land."""
        msg = missing_deps_message()
        target = agent_eval.anova.stats._installer_python()
        plugin_root = Path(agent_eval.anova.stats.__file__).resolve().parents[3]
        assert _install_argv(msg) == [
            target, "-m", "pip", "install", "-e", f"{plugin_root}[anova]"]
        assert sys.executable in msg            # still reports what is running

    @pytest.mark.parametrize("hostile", [
        "/Users/me/My Code/agent-eval-harness",   # spaces: previously broke argv[0]
        "/tmp/$(id > /tmp/pwned)/harness",        # $() expands inside double quotes
        "/tmp/`id`/harness",                      # so do backticks
        "/tmp/glob[abc]/harness",                 # zsh would try to glob this
        "/tmp/it's/harness",                      # a bare apostrophe
    ])
    def test_command_survives_a_hostile_checkout_path(self, hostile, monkeypatch):
        """The message invites the reader to paste this into a shell, so the path
        has to round-trip through the shell exactly — not re-split on spaces and
        not execute anything."""
        root = Path(hostile)
        monkeypatch.setattr(agent_eval.anova.stats, "_plugin_root", lambda: root)
        monkeypatch.setattr(agent_eval.anova.stats, "_installer_python",
                            lambda: f"{hostile}/.eval-venv/bin/python3")

        argv = _install_argv(missing_deps_message())
        assert argv == [f"{hostile}/.eval-venv/bin/python3",
                        "-m", "pip", "install", "-e", f"{hostile}[anova]"]

    def test_installer_target_is_the_venv_when_one_exists(self, tmp_path, monkeypatch):
        root = tmp_path / "plugin"
        (root / ".eval-venv" / "bin").mkdir(parents=True)
        (root / ".eval-venv" / "bin" / "python3").write_text("")
        monkeypatch.setattr(agent_eval.anova.stats, "_plugin_root", lambda: root)
        assert agent_eval.anova.stats._installer_python() == str(
            root / ".eval-venv" / "bin" / "python3")

    def test_installer_falls_back_to_sys_executable_without_a_venv(self, tmp_path, monkeypatch):
        """No .eval-venv: harbor verifier / evalhub pod run straight off site-packages."""
        monkeypatch.setattr(agent_eval.anova.stats, "_plugin_root", lambda: tmp_path)
        assert agent_eval.anova.stats._installer_python() == sys.executable

    def test_uses_the_recorded_import_error_when_given_none(self, monkeypatch):
        """The ANOVA_AVAILABLE gate has no exception to hand over, so the module
        keeps the one it swallowed — otherwise a missing scipy reports nothing."""
        monkeypatch.setattr(agent_eval.anova.stats, "_IMPORT_ERROR",
                            ImportError("No module named 'scipy'", name="scipy"))
        assert "missing:     scipy" in missing_deps_message()

    def test_reports_which_module_was_missing(self):
        msg = missing_deps_message(ImportError("No module named 'pandas'", name="pandas"))
        assert "missing:     pandas" in msg

    def test_degrades_without_an_exception(self):
        assert "missing:     one or more of them" in missing_deps_message()

    def test_does_not_suggest_the_bare_pypi_install(self):
        # The old message said `pip install agent-eval-harness[anova]`, which both
        # targets the wrong interpreter and pulls from PyPI instead of the checkout.
        assert "pip install agent-eval-harness[anova]" not in missing_deps_message()
