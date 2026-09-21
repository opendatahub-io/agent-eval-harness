"""MatrixBuilder — factorial experiment design for agent evaluations."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_eval.anova.corrections import normalize_correction


@dataclass(frozen=True)
class Condition:
    """A single experimental condition (one combination of factor levels)."""

    condition_id: str
    levels: dict[str, Any]


@dataclass
class MatrixConfig:
    """Parsed matrix configuration from an eval YAML."""

    factors: dict[str, list[Any]]
    replications: int = 1
    # matrix.analysis.correction — multiple-comparison correction across the
    # ANOVA term family (holm | bh | none). None = not configured (holm).
    correction: str | None = None
    # matrix.analysis.per_judge — opt-in per-judge ANOVA fan-out, BH-corrected
    # across the judges×terms family. Off by default: extra model fits and
    # report rows; the composite ANOVA stays the headline.
    per_judge: bool = False


class MatrixBuilder:
    """Builds full-factorial experiment designs from YAML configs."""

    @staticmethod
    def from_yaml(path: Path, *, strict: bool = False) -> MatrixConfig | None:
        path = Path(path)
        if not path.exists():
            return None

        with open(path) as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict) or "matrix" not in raw:
            return None

        matrix = raw["matrix"]
        if not isinstance(matrix, Mapping):
            raise ValueError("matrix must be a mapping")

        factors = matrix.get("factors", {})
        if not isinstance(factors, Mapping):
            raise ValueError("matrix.factors must be a mapping")

        if strict and not factors:
            raise ValueError("Matrix must contain at least one factor")

        if not factors:
            return None

        # Each factor's levels must be a non-empty list. A scalar (e.g.
        # `model: claude-opus-4-8`) would otherwise be iterated character-by-
        # character by itertools.product in expand_full_factorial, silently
        # producing a garbage design; an empty list yields zero conditions.
        for name, levels in factors.items():
            if not isinstance(levels, list) or not levels:
                raise ValueError(
                    f"matrix.factors['{name}'] must be a non-empty list of levels "
                    f"(got {type(levels).__name__}); write it as a YAML list, "
                    f"e.g. '{name}: [a, b]'."
                )

        replications = _parse_replications(matrix.get("replications", 1))
        analysis = _parse_analysis(matrix.get("analysis"))
        return MatrixConfig(
            factors=dict(factors),
            replications=replications,
            correction=_parse_correction(analysis),
            per_judge=_parse_per_judge(analysis),
        )

    @staticmethod
    def expand_full_factorial(factors: dict[str, list[Any]]) -> list[Condition]:
        factor_names = sorted(factors.keys())
        level_lists = [factors[name] for name in factor_names]

        conditions = []
        for combo in itertools.product(*level_lists):
            levels = dict(zip(factor_names, combo, strict=True))
            condition_id = _condition_id(levels)
            conditions.append(Condition(condition_id=condition_id, levels=levels))

        return conditions

    @staticmethod
    def generate_experiment_id(factors: dict[str, list[Any]]) -> str:
        canonical = json.dumps(factors, sort_keys=True, default=str)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
        factor_slug = "-".join(_safe_id_segment(name) for name in sorted(factors.keys()))
        return f"exp-{factor_slug}-{digest}"

    @staticmethod
    def estimate_cost(
        n_conditions: int,
        n_cases: int,
        replications: int,
        avg_cost_per_run: float,
    ) -> dict[str, Any]:
        total_runs = n_conditions * n_cases * replications
        return {
            "n_conditions": n_conditions,
            "n_cases": n_cases,
            "replications": replications,
            "total_runs": total_runs,
            "estimated_cost": total_runs * avg_cost_per_run,
        }


def _condition_id(levels: dict[str, Any]) -> str:
    canonical = json.dumps(levels, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _parse_replications(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("matrix.replications must be an integer >= 1")
    return value


def _parse_analysis(analysis: Any) -> Mapping:
    """The matrix.analysis block as a mapping ({} when absent)."""
    if analysis is None:
        return {}
    if not isinstance(analysis, Mapping):
        raise ValueError("matrix.analysis must be a mapping")
    return analysis


def _parse_correction(analysis: Mapping) -> str | None:
    """matrix.analysis.correction, canonicalised — or None when unset.

    A typo has to fail here, at config parse, not after the matrix has already
    burned its budget executing runs.
    """
    correction = analysis.get("correction")
    if correction is None:
        return None
    try:
        return normalize_correction(correction)
    except ValueError as exc:
        raise ValueError(f"matrix.analysis.correction: {exc}") from None


def _parse_per_judge(analysis: Mapping) -> bool:
    """matrix.analysis.per_judge as a strict boolean (default off).

    Same rationale as correction: a mistyped value must fail at config parse,
    not surface as a silently-missing per_judge block after the runs.
    """
    value = analysis.get("per_judge", False)
    if not isinstance(value, bool):
        raise ValueError(
            f"matrix.analysis.per_judge must be a boolean "
            f"(got {type(value).__name__})")
    return value


def _safe_id_segment(value: Any) -> str:
    segment = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(value))
    segment = segment.strip("._-")
    return segment or "factor"
