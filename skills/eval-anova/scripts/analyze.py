"""Post-experiment analysis — ANOVA + Pareto + archival.

Two entry points:

- ``analyze_experiment(run_results, factors)`` — analyse in-memory RunResult
  objects (used by the harbor benchmark driver and tests).
- ``analyze_runs(runs_dir, eval_config)`` — analyse a directory of *standard*
  eval-run runs (each a dir with ``summary.yaml`` [+ ``run_result.json`` /
  ``condition.json``]) and write an ``anova.json`` stats artifact next to them.
  This is what ``/eval-anova`` (and ``--analyze-only``) use, and what lets any
  set of runs — including ones produced by an external CI fan-out — be analysed
  without eval-anova having executed them.
"""

from __future__ import annotations

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import datetime
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from agent_eval.anova.stats import ANOVA_AVAILABLE, missing_deps_message

# pandas is module-level (DataFrames are this module's currency), so a missing
# anova extra fails here — long before the ANOVA_AVAILABLE check below could
# explain it. Raise the actionable message from the point that actually breaks.
try:
    import pandas as pd
except ImportError as exc:
    raise ImportError(missing_deps_message(exc)) from exc

import yaml

from agent_eval.anova.archive import ResultsArchiver
from agent_eval.anova.composite import aggregate_replications
from agent_eval.harbor.reward import compose_reward

logger = logging.getLogger(__name__)


def build_results_dataframe(
    run_results: list[Any],
) -> pd.DataFrame:
    """Convert RunResult list to a DataFrame for statistical analysis."""
    rows = []
    for r in run_results:
        row = {
            "case_id": r.case_id,
            "replication": r.replication,
            "composite": r.composite,
            "condition_id": r.condition.condition_id,
        }
        row.update(r.condition.levels)
        rows.append(row)
    return pd.DataFrame(rows)


def analyze_experiment(
    run_results: list[Any],
    factors: list[str],
    *,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Run statistical analysis on in-memory RunResult objects.

    Uses repeated-measures ANOVA for single-factor designs,
    mixed-effects model for multi-factor designs.
    """
    df = build_results_dataframe(run_results)
    return _analyze_df(df, factors, alpha=alpha, n_runs=len(run_results))


def analyze_runs(
    runs_dir: Path | str,
    eval_config: Any,
    *,
    alpha: float = 0.05,
    write_to: Path | str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Analyse a directory of standard eval-run runs and write ``anova.json``.

    Discovers runs by ``summary.yaml``, computes each case's composite via the
    canonical harness reward composition (``compose_reward`` — honours the
    eval.yaml ``reward:`` section, else boolean-gates + normalised-numeric
    average), groups by condition (from ``condition.json`` levels, falling back
    to the model in ``run_result.json``), runs the ANOVA + Pareto, and writes
    the stats artifact. Returns ``(analysis, artifact_path)``.
    """
    runs_dir = Path(runs_dir)
    rows, factors, cost_by_condition = load_conditions_from_runs(runs_dir, eval_config)
    if not rows:
        raise ValueError(
            f"No scored runs found under {runs_dir} "
            "(expected run directories with a summary.yaml containing per_case)."
        )
    df = pd.DataFrame(rows)
    n_runs = int(df[["condition_id", "replication"]].drop_duplicates().shape[0])
    analysis = _analyze_df(
        df, factors, alpha=alpha, n_runs=n_runs, cost_by_condition=cost_by_condition
    )
    analysis["generated_at"] = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat(timespec="seconds")

    out = Path(write_to) if write_to else runs_dir / "anova.json"
    out.write_text(json.dumps(_make_serializable(analysis), indent=2, default=str))
    logger.info("Wrote stats artifact %s", out)
    return analysis, out


def _analyze_df(
    df: pd.DataFrame,
    factors: list[str],
    *,
    alpha: float = 0.05,
    n_runs: int | None = None,
    cost_by_condition: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Core statistical analysis over a results DataFrame.

    Columns required: ``case_id``, ``composite``, ``condition_id`` and one
    column per factor. ``replication`` is optional.
    """
    if not ANOVA_AVAILABLE:
        raise ImportError(missing_deps_message())

    from agent_eval.anova.stats.anova import mixed_effects_anova, repeated_measures_anova
    from agent_eval.anova.stats.pareto import pareto_frontier

    # Repeated-measures / mixed-effects ANOVA assume a fully-crossed design;
    # pingouin/statsmodels silently drop (listwise) any case missing from a
    # condition, which would leave the reported case count overstating what was
    # actually analysed. Restrict to cases present under every condition and
    # record the rest explicitly so the design/report stay honest.
    df, common_cases, excluded_cases = _restrict_to_common_cases(df)
    if excluded_cases:
        logger.warning(
            "Excluding %d case(s) not present under every condition: %s",
            len(excluded_cases), ", ".join(excluded_cases),
        )

    # rm_anova / mixedlm expect one observation per subject×cell, so average
    # replications per (condition, case) for the ANOVA input. The full df is
    # kept for the spread stats below.
    anova_df = df
    if {"condition_id", "case_id"}.issubset(df.columns):
        agg_spec: dict[str, Any] = {"composite": "mean"}
        for f in factors:
            if f in df.columns:
                agg_spec[f] = "first"
        anova_df = df.groupby(
            ["condition_id", "case_id"], as_index=False, dropna=False
        ).agg(agg_spec)

    n_conditions = int(df["condition_id"].nunique()) if "condition_id" in df.columns else 0
    # A factor with only one observed level can't contribute to the ANOVA (its
    # dummy coding has no contrasts) — drop it so, e.g., a single-model × context
    # matrix analyses cleanly as a one-way context comparison.
    effective = [f for f in factors if f in anova_df.columns and anova_df[f].nunique() >= 2]
    if not effective or n_conditions < 2:
        anova_result = {
            "method": "ANOVA (skipped)",
            "factor": effective[0] if effective else (factors[0] if factors else None),
            "factors": effective,
            "f_statistic": None,
            "p_value": None,
            "significant": False,
            "alpha": alpha,
            "note": (f"No factor has >=2 levels to compare (conditions={n_conditions})."
                     if factors else "No factors to analyse."),
        }
    elif len(effective) == 1:
        anova_result = repeated_measures_anova(anova_df, factor=effective[0], alpha=alpha)
    else:
        anova_result = mixed_effects_anova(anova_df, factors=effective, alpha=alpha)

    cost_by_condition = cost_by_condition or {}
    condition_summaries = []
    for cid, group in df.groupby("condition_id"):
        scores = group["composite"].tolist()
        agg = aggregate_replications(scores)
        levels = {f: group[f].iloc[0] for f in factors if f in group.columns}
        summary = {
            "condition_id": cid,
            "levels": levels,
            # Factor levels are also flattened to top level (e.g. "model") so
            # the report renderer can read them directly without unpacking
            # "levels". Keep "levels" too for programmatic consumers.
            **levels,
            **agg,
        }
        if cid in cost_by_condition:
            summary["cost"] = cost_by_condition[cid]
        condition_summaries.append(summary)

    # Pareto frontier needs a real per-condition cost. Only compute it when
    # every condition has one; otherwise leave the frontier as all conditions
    # (no domination possible without a cost axis).
    if condition_summaries and all("cost" in c for c in condition_summaries):
        frontier = pareto_frontier(
            condition_summaries, cost_key="cost", quality_key="mean"
        )
    else:
        frontier = condition_summaries

    design = _build_design(df, factors)
    if excluded_cases:
        design["excluded_cases"] = excluded_cases
    per_case = _build_per_case(df, factors)

    return {
        "anova": anova_result,
        "condition_summaries": condition_summaries,
        "pareto_frontier": frontier,
        "design": design,
        "per_case": per_case,
        "excluded_cases": excluded_cases,
        "n_runs": n_runs if n_runs is not None else int(len(df)),
        "n_conditions": len(condition_summaries),
    }


# --------------------------------------------------------------------------
# Loading standard eval-run runs into analysis rows
# --------------------------------------------------------------------------

def load_conditions_from_runs(
    runs_dir: Path | str,
    eval_config: Any,
) -> tuple[list[dict[str, Any]], list[str], dict[str, float]]:
    """Build analysis rows from a directory of standard eval-run runs.

    Returns ``(rows, factors, cost_by_condition)`` where each row is
    ``{case_id, replication, composite, condition_id, **levels}``. Runs sharing
    the same factor levels are treated as replications of one condition.
    """
    runs_dir = Path(runs_dir)
    reward_cfg = getattr(eval_config, "reward", None)

    rows: list[dict[str, Any]] = []
    factor_keys: set[str] = set()
    rep_counter: dict[str, int] = {}
    cost_sum: dict[str, float] = {}
    cost_n: dict[str, int] = {}

    for run_dir in _discover_run_dirs(runs_dir):
        try:
            summary = yaml.safe_load((run_dir / "summary.yaml").read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Skipping %s: unreadable summary.yaml (%s)", run_dir, exc)
            continue
        per_case = summary.get("per_case") or {}
        if not per_case:
            continue
        levels = _condition_levels_for_run(run_dir)
        if not levels:
            logger.warning(
                "Skipping %s: no condition.json and no model in run_result.json "
                "— cannot assign it to a condition.", run_dir,
            )
            continue
        factor_keys.update(levels.keys())
        condition_id = _levels_id(levels)
        rep = rep_counter.get(condition_id, 0)
        rep_counter[condition_id] = rep + 1

        cost = _run_cost(run_dir)
        if cost is not None:
            cost_sum[condition_id] = cost_sum.get(condition_id, 0.0) + cost
            cost_n[condition_id] = cost_n.get(condition_id, 0) + 1

        for case_id, judges in per_case.items():
            if not isinstance(judges, dict):
                continue
            composite, _ = compose_reward(judges, reward_cfg=reward_cfg)
            row = {
                "case_id": str(case_id),
                "replication": rep,
                "composite": float(composite),
                "condition_id": condition_id,
            }
            row.update(levels)
            rows.append(row)

    factors = sorted(factor_keys)
    cost_by_condition = {
        cid: cost_sum[cid] / cost_n[cid] for cid in cost_sum if cost_n.get(cid)
    }
    return rows, factors, cost_by_condition


def _discover_run_dirs(runs_dir: Path) -> list[Path]:
    """Run directories under ``runs_dir`` (each containing a ``summary.yaml``)."""
    if not runs_dir.is_dir():
        return []
    return sorted({p.parent for p in runs_dir.rglob("summary.yaml")})


def _condition_levels_for_run(run_dir: Path) -> dict[str, Any]:
    """Factor levels for a run: ``condition.json`` if present, else the model
    from ``run_result.json`` (covers single-factor / externally-produced runs)."""
    cond_path = run_dir / "condition.json"
    if cond_path.is_file():
        try:
            data = json.loads(cond_path.read_text())
            levels = data.get("levels", data) if isinstance(data, dict) else None
            if isinstance(levels, dict) and levels:
                return {str(k): v for k, v in levels.items()}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Ignoring unreadable %s: %s", cond_path, exc)

    rr_path = run_dir / "run_result.json"
    if rr_path.is_file():
        try:
            model = json.loads(rr_path.read_text()).get("model")
            if model:
                return {"model": str(model)}
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _run_cost(run_dir: Path) -> float | None:
    """Total USD cost for a run from ``run_result.json`` (``cost_usd``)."""
    rr_path = run_dir / "run_result.json"
    if not rr_path.is_file():
        return None
    try:
        cost = json.loads(rr_path.read_text()).get("cost_usd")
        return float(cost) if isinstance(cost, (int, float)) else None
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def _levels_id(levels: dict[str, Any]) -> str:
    """Stable condition id from factor levels (matches matrix._condition_id)."""
    canonical = json.dumps(levels, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# Design / per-case helpers (shared)
# --------------------------------------------------------------------------

def _restrict_to_common_cases(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Keep only cases present under *every* condition (a balanced design).

    Returns ``(filtered_df, common_cases, excluded_cases)``. If the frame lacks
    the needed columns, is empty, or has a single condition, nothing is
    excluded. If no case is shared across all conditions the frame is returned
    unchanged (the ANOVA guards then flag the degenerate design).
    """
    if not {"condition_id", "case_id"}.issubset(df.columns) or df.empty:
        return df, [], []
    case_sets = [set(g["case_id"]) for _, g in df.groupby("condition_id")]
    all_cases = set().union(*case_sets)
    common = set(all_cases)
    for s in case_sets:
        common &= s
    excluded = sorted(str(c) for c in (all_cases - common))
    if not excluded:
        return df, sorted(str(c) for c in all_cases), []
    if not common:
        return df, [], excluded
    filtered = df[df["case_id"].isin(common)].copy()
    return filtered, sorted(str(c) for c in common), excluded


def _build_design(df: pd.DataFrame, factors: list[str]) -> dict[str, Any]:
    """Derive the experiment design (factors/levels, case count, reps)."""
    factor_levels = {
        f: sorted(df[f].dropna().unique().tolist())
        for f in factors
        if f in df.columns
    }
    n_cases = int(df["case_id"].nunique()) if "case_id" in df.columns else 0
    # Replications = the largest number of rows for any condition×case pair.
    if {"condition_id", "case_id"}.issubset(df.columns):
        replications = int(df.groupby(["condition_id", "case_id"]).size().max())
    else:
        replications = 1
    return {
        "factors": factor_levels,
        "n_cases": n_cases,
        "replications": replications,
    }


def _build_per_case(df: pd.DataFrame, factors: list[str]) -> dict[str, Any]:
    key_cols = [factor for factor in factors if factor in df.columns]
    if not key_cols and "condition_id" in df.columns:
        key_cols = ["condition_id"]
    if not key_cols or "case_id" not in df.columns:
        return {}

    per_case: dict[str, dict[str, float]] = {}
    for keys, group in df.groupby([*key_cols, "case_id"], dropna=False):
        values = keys if isinstance(keys, tuple) else (keys,)
        factor_values = values[:-1]
        case_id = values[-1]
        if len(key_cols) == 1:
            condition_key = str(factor_values[0])
        else:
            condition_key = _condition_key(key_cols, factor_values)
        per_case.setdefault(condition_key, {})[str(case_id)] = float(
            group["composite"].mean()
        )
    return per_case


def _condition_key(factors: list[str], values: tuple[Any, ...]) -> str:
    return ", ".join(f"{factor}={value}" for factor, value in zip(factors, values))


def archive_results(
    experiment_id: str,
    analysis: dict[str, Any],
    run_results: list[Any],
    repo_path: Path,
) -> Path:
    """Archive experiment results to the results repo."""
    archiver = ResultsArchiver(repo_path=repo_path)

    data = {
        "experiment_id": experiment_id,
        "analysis": _make_serializable(analysis),
        "n_runs": len(run_results),
    }

    return archiver.archive_experiment(experiment_id, data, fallback=True)


def _make_serializable(obj: Any) -> Any:
    """Convert non-serializable types for JSON output."""
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_serializable(i) for i in obj]
    if isinstance(obj, float) and (obj != obj):  # NaN check
        return None
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj
