"""ANOVA methods for agent evaluation experiments.

Three analysis methods, each valid under different assumptions:

- repeated_measures_anova: Use when the SAME cases are evaluated across all
  conditions (the common case in agent eval). Accounts for case difficulty
  as a blocking factor via pingouin rm_anova.

- mixed_effects_anova: Use for multi-factor designs with repeated measures.
  Models case_id as a random effect via statsmodels mixedlm; one joint Wald
  test per term (main effects and interactions), Holm/BH-corrected across the
  term family.

- one_way_anova: Plain scipy f_oneway. ONLY valid when observations are
  truly independent (cases NOT reused across conditions). Rarely appropriate
  for agent eval — included for completeness with clear documentation.
"""

from __future__ import annotations

import math
import re
from typing import Any

import numpy as np
import pandas as pd
import pingouin as pg
import scipy.stats
import statsmodels.formula.api as smf
from statsmodels.stats.multitest import multipletests

from agent_eval.anova.corrections import normalize_correction


def repeated_measures_anova(
    data: pd.DataFrame,
    factor: str,
    subject: str = "case_id",
    response: str = "composite",
    alpha: float = 0.05,
    correction: str = "holm",
) -> dict[str, Any]:
    """Repeated-measures ANOVA using pingouin.

    Appropriate when the same cases are evaluated under each condition level,
    which is the standard agent eval setup.

    A single factor is a family of one test, so the multiplicity fields
    (``p_adjusted``, ``correction``, ``family_size``) are carried purely for
    schema consistency with ``mixed_effects_anova`` — ``p_adjusted`` equals
    ``p_value`` under every correction method.
    """
    correction = normalize_correction(correction)
    # Degenerate design: with no variance in the response (e.g. every cell
    # passes — a ceiling effect from easy cases) the F-ratio is 0/0 and
    # pingouin returns a frame without an "F" column. The ANOVA is undefined,
    # so report a graceful "no variance" result instead of raising KeyError.
    if data[response].nunique() <= 1:
        return {
            "f_statistic": None,
            "p_value": None,
            "p_adjusted": None,
            "significant": False,
            "correction": correction,
            "family_size": 0,
            "excluded_terms": [factor],
            "method": "Repeated-measures ANOVA (pingouin rm_anova)",
            "alpha": alpha,
            "factor": factor,
            "note": "No variance in response — ANOVA undefined (all scores identical).",
            "details": [],
        }

    aov = pg.rm_anova(data=data, dv=response, within=factor, subject=subject)

    if "F" not in aov.columns or pd.isna(aov["F"].iloc[0]):
        return {
            "f_statistic": None,
            "p_value": None,
            "p_adjusted": None,
            "significant": False,
            "correction": correction,
            "family_size": 0,
            "excluded_terms": [factor],
            "method": "Repeated-measures ANOVA (pingouin rm_anova)",
            "alpha": alpha,
            "factor": factor,
            "note": "Degenerate design — no F statistic produced.",
            "details": aov.to_dict(orient="records"),
        }

    f_stat = float(aov["F"].iloc[0])
    p_col = "p-unc" if "p-unc" in aov.columns else "p_unc"
    p_unc = float(aov[p_col].iloc[0])
    # Prefer the Greenhouse-Geisser sphericity-corrected p-value when pingouin
    # reports it (rm_anova adds "p-GG-corr" for within designs); fall back to
    # the uncorrected p otherwise.
    p_val = p_unc
    if "p-GG-corr" in aov.columns and not pd.isna(aov["p-GG-corr"].iloc[0]):
        p_val = float(aov["p-GG-corr"].iloc[0])

    # A within-subject error sum of squares at (or near) zero — perfect
    # separation or a perfectly consistent effect, both common with binary /
    # gated composite scores — makes pingouin return a non-finite or negative F
    # that still passes the NaN check above. Such an F is not a usable
    # statistic (it manufactures "infinitely significant" results or hides a
    # real effect behind a huge negative F), so report the design as degenerate.
    if not math.isfinite(f_stat) or f_stat < 0 or not math.isfinite(p_val):
        return {
            "f_statistic": None,
            "p_value": None,
            "p_adjusted": None,
            "significant": False,
            "correction": correction,
            "family_size": 0,
            "excluded_terms": [factor],
            "method": "Repeated-measures ANOVA (pingouin rm_anova)",
            "alpha": alpha,
            "factor": factor,
            "note": "Degenerate design — near-zero within-subject variance produced a non-finite F.",
            "details": aov.to_dict(orient="records"),
        }

    return {
        "f_statistic": f_stat,
        "p_value": p_val,
        "p_uncorrected": p_unc,
        "p_adjusted": p_val,
        "significant": p_val < alpha,
        "correction": correction,
        "family_size": 1,
        "method": "Repeated-measures ANOVA (pingouin rm_anova)",
        "alpha": alpha,
        "factor": factor,
        "details": aov.to_dict(orient="records"),
    }


# Patsy wraps every categorical factor as ``C(name)``; interactions join the
# wrapped factors with ``:``. Unwrapping gives the readable term keys reported
# downstream: ``C(model)`` -> ``model``, ``C(model):C(context)`` -> ``model:context``.
_C_WRAPPER = re.compile(r"C\(([^()]*)\)")


def _readable_term(term: str) -> str:
    return _C_WRAPPER.sub(r"\1", term)


def _finite_p_or_none(value: Any) -> float | None:
    """A usable p-value, or None for anything degenerate (NaN, inf, off-range)."""
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return p if math.isfinite(p) and 0.0 <= p <= 1.0 else None


def _term_slices(fit: Any) -> dict[str, slice]:
    """Term name -> design-column slice, across statsmodels versions.

    statsmodels <= 0.14 exposes the patsy DesignInfo as
    ``fit.model.data.design_info``; 0.15 renamed the attribute to
    ``model_spec`` (still a patsy DesignInfo when patsy is installed, a
    formulaic ModelSpec otherwise). Patsy carries string-keyed
    ``term_name_slices``; formulaic only ``term_slices`` keyed by Term
    objects, so keys are stringified there.
    """
    spec = (getattr(fit.model.data, "design_info", None)
            or getattr(fit.model.data, "model_spec", None))
    if spec is None:
        return {}
    named = getattr(spec, "term_name_slices", None)
    if named:
        return dict(named)
    slices = getattr(spec, "term_slices", None)
    if slices:
        return {str(k): v for k, v in dict(slices).items()}
    return {}


def _term_wald_p_values(fit: Any) -> dict[str, float | None]:
    """One joint Wald p-value per fixed-effect model term (Intercept dropped).

    A factor with L levels is L-1 dummy coefficients; testing them jointly is
    the omnibus question "does this factor matter at all", whereas any
    per-dummy p only answers "does this level differ from the reference".
    Prefers ``wald_test_terms``; when statsmodels cannot produce the whole
    table (e.g. a singular covariance on a degenerate fit), falls back to the
    equivalent per-term contrast matrices built from the formula design info.
    A term whose test still fails maps to None — degenerate tests are reported
    as absent, never fabricated.
    """
    try:
        table = fit.wald_test_terms(scalar=True).table
        return {
            _readable_term(str(term)): _finite_p_or_none(table.loc[term, "pvalue"])
            for term in table.index
            if str(term) != "Intercept"
        }
    except Exception:  # noqa: BLE001 — degrade to per-term contrasts
        pass

    term_slices = _term_slices(fit)
    if not term_slices:
        return {}
    # Contrast rows select a term's design columns; padding to len(params)
    # zeroes the trailing random-effect variance parameters mixedlm appends.
    n_params = len(fit.params)
    p_by_term: dict[str, float | None] = {}
    for term, slc in term_slices.items():
        if term == "Intercept":
            continue
        contrast = np.zeros((slc.stop - slc.start, n_params))
        for row, col in enumerate(range(slc.start, slc.stop)):
            contrast[row, col] = 1.0
        try:
            p = fit.wald_test(contrast, scalar=True).pvalue
        except Exception:  # noqa: BLE001 — this term's test is degenerate
            p = None
        p_by_term[_readable_term(term)] = _finite_p_or_none(p)
    return p_by_term


def adjust_term_p_values(
    p_values: dict[str, float | None],
    *,
    correction: str = "holm",
    alpha: float = 0.05,
) -> tuple[dict[str, float | None], dict[str, bool], int]:
    """Multiplicity correction across one model's family of term tests.

    The family is only the *real* tests: a term with a degenerate (None)
    p-value gets ``p_adjusted=None`` / ``significant=False`` and does not
    count toward — or inflate — anyone else's adjusted p. With
    ``correction="none"`` adjusted equals raw and significance is ``p < alpha``.
    Returns ``(p_adjusted, significant, family_size)``.
    """
    correction = normalize_correction(correction)
    tested = {t: p for t, p in p_values.items() if p is not None}
    p_adjusted: dict[str, float | None] = {t: None for t in p_values}
    significant = {t: False for t in p_values}
    if tested:
        if correction == "none":
            adjusted: list[float] = list(tested.values())
            rejected = [p < alpha for p in tested.values()]
        else:
            method = {"holm": "holm", "bh": "fdr_bh"}[correction]
            rejected, adjusted, _, _ = multipletests(
                list(tested.values()), alpha=alpha, method=method
            )
        for term, adj, rej in zip(tested, adjusted, rejected):
            p_adjusted[term] = float(adj)
            significant[term] = bool(rej)
    return p_adjusted, significant, len(tested)


def mixed_effects_anova(
    data: pd.DataFrame,
    factors: list[str],
    subject: str = "case_id",
    response: str = "composite",
    alpha: float = 0.05,
    correction: str = "holm",
) -> dict[str, Any]:
    """Mixed-effects model with case_id as random effect.

    Appropriate for multi-factor designs where the same cases appear
    under all factor combinations.

    Every fixed-effect term — main effects *and* interactions — gets one joint
    Wald test (not the minimum of its level-dummy p-values, which is
    anti-conservative for factors with >2 levels and is not an omnibus test),
    then the whole term family is corrected for multiplicity (Holm by default)
    before the significance calls. ``p_values`` holds the raw per-term values,
    ``p_adjusted`` the corrected ones; ``all_p_values``/``coefficients`` keep
    the coefficient-level detail for transparency.
    """
    correction = normalize_correction(correction)
    fixed_terms = " * ".join(f"C({f})" for f in factors)
    formula = f"{response} ~ {fixed_terms}"

    model = smf.mixedlm(formula, data=data, groups=data[subject]).fit(reml=True)

    coefficient_p_values = {}
    coefficients = {}
    for name in model.fe_params.index:
        coefficient_p_values[name] = float(model.pvalues[name])
        coefficients[name] = float(model.fe_params[name])

    term_p_values = _term_wald_p_values(model)
    p_adjusted, significant, family_size = adjust_term_p_values(
        term_p_values, correction=correction, alpha=alpha
    )

    result = {
        "p_values": term_p_values,
        "p_adjusted": p_adjusted,
        "significant": significant,
        "correction": correction,
        "family_size": family_size,
        "coefficients": coefficients,
        "all_p_values": coefficient_p_values,
        "method": "Mixed-effects model (statsmodels mixedlm, per-term Wald tests)",
        "alpha": alpha,
        "factors": factors,
        "aic": float(model.aic),
        "bic": float(model.bic),
    }
    excluded = sorted(t for t, p in term_p_values.items() if p is None)
    if excluded:
        result["excluded_terms"] = excluded
        result["note"] = (
            "No Wald test for: " + ", ".join(excluded)
            + " — degenerate term(s), excluded from the correction family."
        )
    return result


def one_way_anova(
    scores_by_level: dict[str, list[float]],
    factor_name: str,
    alpha: float = 0.05,
    correction: str = "holm",
) -> dict[str, Any]:
    """Plain one-way ANOVA using scipy.stats.f_oneway.

    WARNING: Only valid when observations are independent — i.e., cases are
    NOT reused across conditions. This is rarely true in agent eval setups.
    For repeated-measures designs, use repeated_measures_anova or
    mixed_effects_anova instead.

    Single factor = family of one test: the multiplicity fields exist for
    schema consistency and ``p_adjusted`` equals ``p_value``.
    """
    correction = normalize_correction(correction)
    groups = list(scores_by_level.values())
    f_stat, p_val = scipy.stats.f_oneway(*groups)

    # f_oneway yields NaN on zero-variance input; that is no test at all, so
    # it must not be counted in the family (or compared against alpha).
    p = _finite_p_or_none(p_val)
    result = {
        "f_statistic": float(f_stat) if math.isfinite(f_stat) else None,
        "p_value": p,
        "p_adjusted": p,
        "significant": p is not None and p < alpha,
        "correction": correction,
        "family_size": 1 if p is not None else 0,
        "method": "One-way ANOVA (independent samples, scipy f_oneway)",
        "alpha": alpha,
        "factor": factor_name,
        "n_groups": len(groups),
        "group_sizes": {k: len(v) for k, v in scores_by_level.items()},
    }
    if p is None:
        result["note"] = "Degenerate design — f_oneway produced no finite p-value."
        result["excluded_terms"] = [factor_name]
    return result
