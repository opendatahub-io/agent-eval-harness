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

import itertools
import math
import re
import warnings
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

    ``contrasts`` holds the post-hoc pairwise level comparisons (paired tests,
    corrected within the factor); they are computed whenever pairwise tests
    are possible, regardless of the omnibus outcome.
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
            "method": "Repeated-measures ANOVA (pingouin rm_anova)",
            "alpha": alpha,
            "factor": factor,
            "note": "No variance in response — ANOVA undefined (all scores identical).",
            "details": [],
            # Constant response: every paired difference is identically 0, so
            # no pairwise test exists either — excluded with a reason, never
            # given a fabricated p.
            "contrasts": {factor: _contrast_block(
                factor, [], correction=correction, alpha=alpha,
                contrast_type="paired", omnibus_p_adjusted=None,
                reason="No variance in response — no pairwise tests computed.",
            )},
        }

    aov = pg.rm_anova(data=data, dv=response, within=factor, subject=subject)

    # Pairwise contrasts are computed even when the omnibus test below turns
    # out degenerate (e.g. perfect separation kills the F but the paired tests
    # simply report their own degeneracy per pair) — no hidden gating.
    contrasts = {factor: _rm_pairwise_contrasts(
        data, factor, subject=subject, response=response, alpha=alpha,
        correction=correction, omnibus_p_adjusted=None,
    )}

    if "F" not in aov.columns or pd.isna(aov["F"].iloc[0]):
        return {
            "f_statistic": None,
            "p_value": None,
            "p_adjusted": None,
            "significant": False,
            "correction": correction,
            "family_size": 0,
            "method": "Repeated-measures ANOVA (pingouin rm_anova)",
            "alpha": alpha,
            "factor": factor,
            "note": "Degenerate design — no F statistic produced.",
            "details": aov.to_dict(orient="records"),
            "contrasts": contrasts,
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
            "method": "Repeated-measures ANOVA (pingouin rm_anova)",
            "alpha": alpha,
            "factor": factor,
            "note": "Degenerate design — near-zero within-subject variance produced a non-finite F.",
            "details": aov.to_dict(orient="records"),
            "contrasts": contrasts,
        }

    contrasts[factor]["omnibus_p_adjusted"] = p_val
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
        "contrasts": contrasts,
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

    design_info = getattr(fit.model.data, "design_info", None)
    if design_info is None:
        return {}
    # Contrast rows select a term's design columns; padding to len(params)
    # zeroes the trailing random-effect variance parameters mixedlm appends.
    n_params = len(fit.params)
    p_by_term: dict[str, float | None] = {}
    for term, slc in design_info.term_name_slices.items():
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


# --------------------------------------------------------------------------
# Post-hoc pairwise level contrasts (per factor, corrected within the factor)
# --------------------------------------------------------------------------

# What a contrast's ``estimate`` is, per computation path — stamped on each
# factor block so a report never presents a reference-cell contrast as a
# marginal mean.
_CONTRAST_NOTES = {
    "paired": ("Estimates are observed paired mean differences (a − b) "
               "across cases."),
    "marginal": ("Estimates are fixed-effect coefficient differences from the "
                 "fitted model (single-factor model — marginal differences)."),
    "reference-cell": ("Estimates are reference-cell contrasts from the fitted "
                       "model — level differences at the other factors' "
                       "reference levels, NOT marginal means (the model "
                       "includes interactions)."),
}


def _contrast_block(
    factor: str,
    pairs: list[dict[str, Any]],
    *,
    correction: str,
    alpha: float,
    contrast_type: str,
    omnibus_p_adjusted: float | None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Assemble one factor's contrast block, applying the multiplicity
    correction across that factor's pairs only.

    The family is the pairwise contrasts *within this factor* (never pooled
    across factors), and — like the term family — counts only real tests: a
    pair with no finite raw p gets ``p_adjusted=None`` / ``significant=False``
    plus a ``reason``, and never inflates the other pairs' adjusted values.
    ``omnibus_p_adjusted`` carries the factor's omnibus result for context;
    contrasts are computed regardless of it (no significance gating).
    """
    raw = {str(i): p.get("p_raw") for i, p in enumerate(pairs)}
    adjusted, significant, family_size = adjust_term_p_values(
        raw, correction=correction, alpha=alpha
    )
    for i, pair in enumerate(pairs):
        pair["p_adjusted"] = adjusted[str(i)]
        pair["significant"] = significant[str(i)]
        if pair.get("p_raw") is None:
            pair.setdefault(
                "reason",
                "no finite p — degenerate pair, excluded from the correction family",
            )
    block = {
        "correction": correction,
        "family": f"pairwise level contrasts within factor '{factor}'",
        "family_size": family_size,
        "contrast_type": contrast_type,
        "omnibus_p_adjusted": omnibus_p_adjusted,
        "pairs": pairs,
    }
    # An estimate-interpretation note makes no sense on a block with no
    # estimates — the reason field carries the story for empty blocks.
    if pairs:
        block["note"] = _CONTRAST_NOTES[contrast_type]
    if reason:
        block["reason"] = reason
    return block


def _rm_pairwise_contrasts(
    data: pd.DataFrame,
    factor: str,
    *,
    subject: str,
    response: str,
    alpha: float,
    correction: str,
    omnibus_p_adjusted: float | None,
) -> dict[str, Any]:
    """Pairwise paired comparisons for the single-factor design.

    p-values come from pingouin's paired ``pairwise_tests`` (the same engine
    as ``rm_anova``); estimates and SEs are the observed paired differences
    (mean and SE of per-case ``a − b``), so the estimate is on the composite
    scale. The correction is applied by ``_contrast_block`` so degenerate
    pairs are excluded from the family exactly like everywhere else (pingouin's
    own ``padjust`` would count them).
    """
    levels = sorted(data[factor].dropna().unique().tolist(), key=str)
    # One observation per subject×level (replications averaged), aligned by
    # subject so the differences are truly paired.
    wide = data.pivot_table(index=subject, columns=factor, values=response,
                            aggfunc="mean")
    p_by_pair: dict[frozenset, Any] = {}
    try:
        # Degenerate pairs (zero-variance differences) make scipy/pingouin
        # emit RuntimeWarnings for NaN/inf intermediates; the resulting
        # non-finite p is handled and reported explicitly below, so the
        # chatter carries no extra information.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            pw = pg.pairwise_tests(data=data, dv=response, within=factor,
                                   subject=subject, padjust="none")
        p_col = "p_unc" if "p_unc" in pw.columns else "p-unc"
        for _, row in pw.iterrows():
            p_by_pair[frozenset({str(row["A"]), str(row["B"])})] = row[p_col]
        pw_error = None
    except Exception as exc:  # noqa: BLE001 — no fabricated p, but a wholesale
        # pairwise_tests failure is a systemic condition, not per-pair
        # degeneracy — record it at block level so the artifact tells the
        # right story.
        pw_error = f"pairwise tests unavailable: {exc}"

    pairs = []
    for a, b in itertools.combinations(levels, 2):
        diff = (wide[a] - wide[b]).dropna()
        n = int(diff.count())
        estimate = float(diff.mean()) if n else None
        se = float(diff.std(ddof=1) / math.sqrt(n)) if n >= 2 else None
        if se is not None and not math.isfinite(se):
            se = None
        pair = {
            "a": str(a),
            "b": str(b),
            "estimate": estimate,
            "se": se,
            "p_raw": _finite_p_or_none(p_by_pair.get(frozenset({str(a), str(b)}))),
        }
        # Zero-variance differences (e.g. perfect separation) drive the paired
        # t to ±inf, which scipy renders as p = 0.0 — a fabricated certainty,
        # not a test. The estimate stays (it is the observed difference); the
        # p does not.
        if se == 0.0 and pair["p_raw"] is not None:
            pair["p_raw"] = None
            pair["reason"] = ("zero-variance paired differences — "
                              "t-test undefined, excluded from the correction family")
        elif pw_error and pair["p_raw"] is None:
            pair["reason"] = pw_error
        pairs.append(pair)
    return _contrast_block(
        factor, pairs, correction=correction, alpha=alpha,
        contrast_type="paired", omnibus_p_adjusted=omnibus_p_adjusted,
        reason=pw_error,
    )


def _mixedlm_pairwise_contrasts(
    fit: Any,
    *,
    alpha: float,
    correction: str,
    omnibus_p_adjusted: dict[str, float | None],
) -> dict[str, dict[str, Any]]:
    """Pairwise level contrasts per factor from the FITTED mixed model.

    No refitting: level-vs-reference is a single fixed-effect coefficient,
    level A vs level B the coefficient difference — each tested with a
    contrast vector through ``fit.t_test`` (MixedLM's ``t_test`` takes only
    numeric contrast matrices over the fixed effects, not string constraints).
    With interactions in the model these are reference-cell contrasts, flagged
    as such via ``contrast_type``.
    """
    model_data = getattr(getattr(fit, "model", None), "data", None)
    design_info = getattr(model_data, "design_info", None)
    if design_info is None:
        return {}
    k_fe = len(fit.fe_params)
    has_interactions = any(len(t.factors) > 1 for t in design_info.terms)
    contrast_type = "reference-cell" if has_interactions else "marginal"

    contrasts: dict[str, dict[str, Any]] = {}
    for term in design_info.terms:
        if len(term.factors) != 1:  # main effects only — one block per factor
            continue
        info = design_info.factor_infos.get(term.factors[0])
        categories = list(getattr(info, "categories", None) or [])
        slc = design_info.term_name_slices[term.name()]
        # Treatment coding: the term's columns map to categories[1:] in order,
        # categories[0] being the reference. Anything else is a coding this
        # helper doesn't understand — skip rather than mislabel contrasts.
        if len(categories) < 2 or slc.stop - slc.start != len(categories) - 1:
            continue
        col_by_level = {lvl: slc.start + i for i, lvl in enumerate(categories[1:])}
        factor = _readable_term(term.name())

        pairs = []
        for a, b in itertools.combinations(categories, 2):
            # estimate = effect(a) − effect(b); the reference level's
            # coefficient is identically 0 under treatment coding.
            row = np.zeros((1, k_fe))
            estimate = 0.0
            if a in col_by_level:
                row[0, col_by_level[a]] = 1.0
                estimate += float(fit.fe_params.iloc[col_by_level[a]])
            if b in col_by_level:
                row[0, col_by_level[b]] = -1.0
                estimate -= float(fit.fe_params.iloc[col_by_level[b]])
            try:
                tt = fit.t_test(row)
                se = float(np.asarray(tt.sd).ravel()[0])
                p_raw = _finite_p_or_none(np.asarray(tt.pvalue).ravel()[0])
            except Exception:  # noqa: BLE001 — this pair's test is degenerate
                se, p_raw = None, None
            pairs.append({
                "a": str(a),
                "b": str(b),
                "estimate": estimate,
                "se": se if se is None or math.isfinite(se) else None,
                "p_raw": p_raw,
            })
        contrasts[factor] = _contrast_block(
            factor, pairs, correction=correction, alpha=alpha,
            contrast_type=contrast_type,
            omnibus_p_adjusted=omnibus_p_adjusted.get(factor),
        )
    return contrasts


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

    ``contrasts`` adds the post-hoc pairwise level comparisons per factor,
    computed from this same fit (no refitting) and corrected within each
    factor — reference-cell contrasts when the model has interactions.
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
        "contrasts": _mixedlm_pairwise_contrasts(
            model, alpha=alpha, correction=correction,
            omnibus_p_adjusted=p_adjusted,
        ),
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
    return result
