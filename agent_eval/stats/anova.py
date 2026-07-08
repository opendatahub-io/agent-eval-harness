"""ANOVA methods for agent evaluation experiments.

Three analysis methods, each valid under different assumptions:

- repeated_measures_anova: Use when the SAME cases are evaluated across all
  conditions (the common case in agent eval). Accounts for case difficulty
  as a blocking factor via pingouin rm_anova.

- mixed_effects_anova: Use for multi-factor designs with repeated measures.
  Models case_id as a random effect via statsmodels mixedlm.

- one_way_anova: Plain scipy f_oneway. ONLY valid when observations are
  truly independent (cases NOT reused across conditions). Rarely appropriate
  for agent eval — included for completeness with clear documentation.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pingouin as pg
import scipy.stats
import statsmodels.formula.api as smf


def repeated_measures_anova(
    data: pd.DataFrame,
    factor: str,
    subject: str = "case_id",
    response: str = "composite",
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Repeated-measures ANOVA using pingouin.

    Appropriate when the same cases are evaluated under each condition level,
    which is the standard agent eval setup.
    """
    # Degenerate design: with no variance in the response (e.g. every cell
    # passes — a ceiling effect from easy cases) the F-ratio is 0/0 and
    # pingouin returns a frame without an "F" column. The ANOVA is undefined,
    # so report a graceful "no variance" result instead of raising KeyError.
    if data[response].nunique() <= 1:
        return {
            "f_statistic": None,
            "p_value": None,
            "significant": False,
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
            "significant": False,
            "method": "Repeated-measures ANOVA (pingouin rm_anova)",
            "alpha": alpha,
            "factor": factor,
            "note": "Degenerate design — no F statistic produced.",
            "details": aov.to_dict(orient="records"),
        }

    f_stat = float(aov["F"].iloc[0])
    p_col = "p-unc" if "p-unc" in aov.columns else "p_unc"
    p_val = float(aov[p_col].iloc[0])

    return {
        "f_statistic": f_stat,
        "p_value": p_val,
        "significant": p_val < alpha,
        "method": "Repeated-measures ANOVA (pingouin rm_anova)",
        "alpha": alpha,
        "factor": factor,
        "details": aov.to_dict(orient="records"),
    }


def mixed_effects_anova(
    data: pd.DataFrame,
    factors: list[str],
    subject: str = "case_id",
    response: str = "composite",
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Mixed-effects model with case_id as random effect.

    Appropriate for multi-factor designs where the same cases appear
    under all factor combinations.
    """
    fixed_terms = " * ".join(f"C({f})" for f in factors)
    formula = f"{response} ~ {fixed_terms}"

    model = smf.mixedlm(formula, data=data, groups=data[subject]).fit(reml=True)

    p_values = {}
    coefficients = {}
    for name in model.fe_params.index:
        p_values[name] = float(model.pvalues[name])
        coefficients[name] = float(model.fe_params[name])

    factor_p_values = {}
    for factor in factors:
        factor_token = f"C({factor})"
        matching = {
            k: v for k, v in p_values.items()
            if factor_token in k and ":" not in k
        }
        if matching:
            factor_p_values[factor] = min(matching.values())

    return {
        "p_values": factor_p_values,
        "coefficients": coefficients,
        "all_p_values": p_values,
        "significant": {f: p < alpha for f, p in factor_p_values.items()},
        "method": "Mixed-effects model (statsmodels mixedlm)",
        "alpha": alpha,
        "factors": factors,
        "aic": float(model.aic),
        "bic": float(model.bic),
    }


def one_way_anova(
    scores_by_level: dict[str, list[float]],
    factor_name: str,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Plain one-way ANOVA using scipy.stats.f_oneway.

    WARNING: Only valid when observations are independent — i.e., cases are
    NOT reused across conditions. This is rarely true in agent eval setups.
    For repeated-measures designs, use repeated_measures_anova or
    mixed_effects_anova instead.
    """
    groups = list(scores_by_level.values())
    f_stat, p_val = scipy.stats.f_oneway(*groups)

    return {
        "f_statistic": float(f_stat),
        "p_value": float(p_val),
        "significant": p_val < alpha,
        "method": "One-way ANOVA (independent samples, scipy f_oneway)",
        "alpha": alpha,
        "factor": factor_name,
        "n_groups": len(groups),
        "group_sizes": {k: len(v) for k, v in scores_by_level.items()},
    }
