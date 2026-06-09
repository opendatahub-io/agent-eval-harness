"""Orchestrator — applies conditions, runs cells, manages experiment execution.

Preflight checks validate archive repo and estimate cost before any API calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_eval.archive import ResultsArchiver
from agent_eval.composite import composite_score
from agent_eval.matrix import Condition, MatrixBuilder

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    """Result of a single cell execution."""

    condition: Condition
    case_id: str
    replication: int
    judge_results: dict[str, Any]
    composite: float
    metadata: dict[str, Any]


def apply_condition(
    condition: Condition,
    eval_config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a condition's factor levels to produce runner and skill kwargs.

    Maps factor names to their config targets:
    - "model" → runner_kwargs["model"]
    - "effort" → run_skill_kwargs["effort"]
    - Other factors → run_skill_kwargs[factor_name]
    """
    runner_kwargs = dict(eval_config.get("runner_kwargs", {}))
    run_skill_kwargs = dict(eval_config.get("run_skill_kwargs", {}))

    for factor, level in condition.levels.items():
        if factor == "model":
            runner_kwargs["model"] = level
        else:
            run_skill_kwargs[factor] = level

    return runner_kwargs, run_skill_kwargs


def prepare_knowledge_context(
    workspace: Path,
    level: str | None = None,
) -> str | None:
    """Prepare knowledge context from workspace for eval runs.

    Scans workspace for context files and returns concatenated context,
    or None if no context is available.
    """
    if level is None:
        return None

    context_dir = workspace / ".knowledge" / level
    if not context_dir.is_dir():
        return None

    parts = []
    for f in sorted(context_dir.glob("*.md")):
        parts.append(f.read_text())

    return "\n\n---\n\n".join(parts) if parts else None


def run_cell(
    condition: Condition,
    case_id: str,
    replication: int,
    eval_config: dict[str, Any],
    judge_configs: dict[str, dict[str, Any]],
    *,
    run_fn: Any = None,
    score_fn: Any = None,
) -> RunResult:
    """Execute a single experimental cell: one condition × one case × one rep.

    run_fn: callable(case_id, **runner_kwargs, **run_skill_kwargs) -> judge_results
    score_fn: callable(judge_results, judge_configs) -> float (defaults to composite_score)
    """
    runner_kwargs, run_skill_kwargs = apply_condition(condition, eval_config)

    if run_fn is None:
        raise ValueError("run_fn is required — pass the eval runner callable")

    judge_results = run_fn(case_id, **runner_kwargs, **run_skill_kwargs)

    if score_fn is None:
        score_fn = composite_score

    score = score_fn(judge_results, judge_configs)

    return RunResult(
        condition=condition,
        case_id=case_id,
        replication=replication,
        judge_results=judge_results,
        composite=score,
        metadata={
            "runner_kwargs": runner_kwargs,
            "run_skill_kwargs": run_skill_kwargs,
        },
    )


def preflight(
    eval_config: dict[str, Any],
    n_conditions: int,
    n_cases: int,
    replications: int,
    avg_cost_per_run: float = 0.0,
    *,
    interactive: bool = True,
) -> dict[str, Any]:
    """Preflight checks before experiment execution.

    1. Validate archive repo (fail fast in headless mode)
    2. Estimate cost
    """
    repo_path = ResultsArchiver.resolve_repo_path(interactive=interactive)

    cost = MatrixBuilder.estimate_cost(
        n_conditions=n_conditions,
        n_cases=n_cases,
        replications=replications,
        avg_cost_per_run=avg_cost_per_run,
    )

    return {
        "repo_path": repo_path,
        "cost_estimate": cost,
        "valid": True,
    }
