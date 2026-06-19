#!/usr/bin/env python3
"""Judge -> Harbor ``reward.json`` bridge.

Runs the agent-eval-harness judge engine against a SINGLE case directory and
writes Harbor's reward contract (``reward.json`` / ``reward.txt``) plus a richer
``judges.json`` sidecar (per-judge value + rationale) for the suite layer.

This is what makes judgment-graded tasks work on Harbor: inside the task
container, the verifier (``tests/test.sh``) calls this module, which reuses
``load_judges`` + ``score_cases`` from ``skills/eval-run/scripts/score.py`` —
the same engine the local ``/eval-run`` path uses — so per-case grading is
identical whether run locally or in a Harbor trial.

Reward composition:
- If a ``grpo_reward`` judge exists in the eval config, its value is used
  directly as the overall reward. This lets the eval.yaml define the full
  aggregation formula (weighted layers, gating, efficiency) in one place.
- Otherwise falls back to the legacy default: boolean judges are GATES
  (any fail -> 0.0), numeric judges (1-N) are normalized and averaged.
- Score range is auto-detected from the eval config (``score_range`` field)
  or defaults to 1-5 for backward compatibility.

Pairwise comparison and regression thresholds are SUITE-level (need >=2 runs /
the full set) and stay above Harbor — they are not computed here.

Usage:
    python3 -m agent_eval.harbor.reward --config eval.yaml [--case-dir .] \\
        [--run-id <id>] [--out-dir /logs/verifier]
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Optional

from agent_eval.config import EvalConfig

# Legacy default score range for backward compatibility.
# Overridden when a grpo_reward judge handles aggregation, or when
# the eval config specifies a different range.
_SCORE_MIN_DEFAULT = 1.0
_SCORE_MAX_DEFAULT = 5.0

# Judge name that, when present, provides the pre-aggregated reward.
_GRPO_REWARD_JUDGE = "grpo_reward"

# Harbor's canonical verifier output directory inside the container.
_DEFAULT_OUT_DIR = "/logs/verifier"


def _load_score_module():
    """Load the judge engine from skills/eval-run/scripts/score.py.

    The engine lives with the eval-run skill (not in the agent_eval package),
    so load it by path — mirrors agent_eval/evalhub/adapter.py._get_score_module.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    score_path = repo_root / "skills" / "eval-run" / "scripts" / "score.py"
    if not score_path.exists():
        raise FileNotFoundError(f"Judge engine not found: {score_path}")
    spec = importlib.util.spec_from_file_location("agent_eval_score", score_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for required in ("load_judges", "score_cases"):
        if not hasattr(mod, required):
            raise AttributeError(f"score.py is missing {required}()")
    return mod


def score_case(config: EvalConfig, case_dir: Path,
               run_id: Optional[str] = None) -> dict:
    """Run all (non-pairwise) judges against one case directory.

    Returns the per-judge result dict: ``{judge_name: {value, rationale,
    judge_type}}`` for the single case.
    """
    score = _load_score_module()
    judges = score.load_judges(config, project_root=Path.cwd())
    # load_case_record only reads stdout/stderr/metrics when a run_id is set; in
    # the container there is no run dir, so default it to the case dir name so
    # stdout-based judges (e.g. pipeline_flow) see case_dir/stdout.log.
    effective_run_id = run_id or case_dir.name
    result = score.score_cases(judges, [case_dir], config, run_id=effective_run_id)
    return result.get("per_case", {}).get(case_dir.name, {})


def compose_reward(per_judge: dict, *,
                   score_min: float = _SCORE_MIN_DEFAULT,
                   score_max: float = _SCORE_MAX_DEFAULT) -> tuple[float, dict]:
    """Collapse per-judge results into an overall reward + flat metric dict.

    Returns ``(reward, metrics)`` where ``metrics`` maps each judge name to a
    number (bool -> 1.0/0.0, numeric -> raw score). Judges that were skipped
    (condition false) or errored have ``value is None`` and are recorded with
    no value rather than gated on.

    If a ``grpo_reward`` judge is present and produced a numeric value, that
    value is used directly as the reward (it already encodes gating, weighted
    aggregation, and efficiency). Individual judge scores are still recorded
    in metrics for diagnostics.
    """
    metrics: dict[str, float] = {}

    for name, rec in per_judge.items():
        value = rec.get("value")
        if value is None:
            continue
        if isinstance(value, bool):
            metrics[name] = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            metrics[name] = float(value)

    grpo_rec = per_judge.get(_GRPO_REWARD_JUDGE, {})
    grpo_val = grpo_rec.get("value")
    if grpo_val is not None and isinstance(grpo_val, (int, float)):
        reward = max(0.0, min(1.0, float(grpo_val)))
        return reward, metrics

    gate_ok = True
    normalized_scores: list[float] = []

    for name, rec in per_judge.items():
        if name == _GRPO_REWARD_JUDGE:
            continue
        value = rec.get("value")
        if value is None:
            continue
        if isinstance(value, bool) and not value:
            gate_ok = False
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            span = score_max - score_min
            norm = (float(value) - score_min) / span if span else 0.0
            normalized_scores.append(max(0.0, min(1.0, norm)))

    if not gate_ok:
        reward = 0.0
    elif normalized_scores:
        reward = sum(normalized_scores) / len(normalized_scores)
    else:
        reward = 1.0
    return reward, metrics


def build_reward(config: EvalConfig, case_dir: Path,
                 run_id: Optional[str] = None) -> dict:
    """Score a case and build the full reward payload.

    Returns a dict with ``reward`` (overall float), per-judge ``metrics``, and
    the full ``per_judge`` detail (value + rationale) for the sidecar.
    """
    per_judge = score_case(config, case_dir, run_id=run_id)

    score_range = getattr(config, "score_range", None) or {}
    score_min = float(score_range.get("min", _SCORE_MIN_DEFAULT))
    score_max = float(score_range.get("max", _SCORE_MAX_DEFAULT))

    reward, metrics = compose_reward(per_judge,
                                     score_min=score_min, score_max=score_max)
    return {"reward": reward, "metrics": metrics, "per_judge": per_judge}


def write_reward(payload: dict, out_dir: Path, case_dir: Optional[Path] = None) -> None:
    """Write Harbor's reward files + the judges.json sidecar.

    - ``<out_dir>/reward.json``  flat {reward, <judge>: <num>, ...} (Harbor reads this)
    - ``<out_dir>/reward.txt``   the scalar reward (Harbor's fallback)
    - ``<out_dir>/judges.json``  full per-judge detail (value + rationale)
    - ``<case_dir>/judges.json`` same sidecar, alongside the artifacts, for the
      suite layer / report when results are downloaded back to the host
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    reward = payload["reward"]

    reward_json = {"reward": reward, **payload["metrics"]}
    (out_dir / "reward.json").write_text(json.dumps(reward_json, indent=2))
    (out_dir / "reward.txt").write_text(str(reward))

    sidecar = json.dumps(
        {"reward": reward, "per_judge": payload["per_judge"]}, indent=2, default=str)
    (out_dir / "judges.json").write_text(sidecar)
    if case_dir is not None:
        (case_dir / "judges.json").write_text(sidecar)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True, help="Path to eval.yaml")
    parser.add_argument("--case-dir", default=".",
                        help="Case workspace dir holding the collected artifacts "
                             "(default: cwd)")
    parser.add_argument("--run-id", default=None,
                        help="Run id for execution-metadata lookup (optional)")
    parser.add_argument("--out-dir", default=_DEFAULT_OUT_DIR,
                        help=f"Where to write reward files (default: {_DEFAULT_OUT_DIR})")
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)
    case_dir = Path(args.case_dir).resolve()

    payload = build_reward(config, case_dir, run_id=args.run_id)
    write_reward(payload, Path(args.out_dir), case_dir=case_dir)

    metric_summary = ", ".join(
        f"{k}={v}" for k, v in payload["metrics"].items()) or "(no judges scored)"
    print(f"reward={payload['reward']:.4f}  [{metric_summary}]")
    print(f"wrote {args.out_dir}/reward.json, reward.txt, judges.json")


if __name__ == "__main__":
    main()
