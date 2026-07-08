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

        replications = _parse_replications(matrix.get("replications", 1))
        return MatrixConfig(factors=dict(factors), replications=replications)

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


def _safe_id_segment(value: Any) -> str:
    segment = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(value))
    segment = segment.strip("._-")
    return segment or "factor"
