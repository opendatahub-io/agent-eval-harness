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

Reward composition (resolution order):
1. If a ``reward:`` section exists in eval.yaml, use its formula/weights
   to compose the reward from judge results. Supports ``weighted``,
   single judge reference, or Python expression modes.
2. If a ``grpo_reward`` judge exists (legacy), use its value directly.
3. Otherwise: boolean judges gate (any fail -> 0.0), numeric judges
   normalized to [0,1] and averaged.

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

from agent_eval.config import EvalConfig, RewardConfig

_SCORE_MIN_DEFAULT = 1.0
_SCORE_MAX_DEFAULT = 5.0

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


def _extract_metrics(per_judge: dict) -> dict[str, float]:
    """Build flat metric dict from per-judge results."""
    metrics: dict[str, float] = {}
    for name, rec in per_judge.items():
        value = rec.get("value")
        if value is None:
            continue
        if isinstance(value, bool):
            metrics[name] = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            metrics[name] = float(value)
    return metrics


def _normalize(value: float, score_range: list[float]) -> float:
    """Normalize a score to [0, 1] given a [min, max] range."""
    lo, hi = score_range
    span = hi - lo
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / span))


def _judge_value(per_judge: dict, name: str,
                 score_range: list[float],
                 raw_judges: list[str] = ()) -> Optional[float]:
    """Extract a judge's value as a float in [0, 1]."""
    rec = per_judge.get(name, {})
    val = rec.get("value")
    if val is None:
        return None
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    if isinstance(val, (int, float)):
        if name in raw_judges:
            return max(0.0, min(1.0, float(val)))
        return _normalize(float(val), score_range)
    return None


def compute_reward_from_config(per_judge: dict,
                               reward_cfg: RewardConfig) -> float:
    """Compute reward using the eval.yaml reward: section.

    Supports three formula modes:
    - "weighted": weighted sum of named judges
    - single judge name: use that judge's value directly
    - expression: Python expression with judge names as variables
    """
    score_range = reward_cfg.score_range
    raw_judges = reward_cfg.raw

    if reward_cfg.gate:
        for name, rec in per_judge.items():
            val = rec.get("value")
            if isinstance(val, bool) and not val:
                return 0.0

    formula = reward_cfg.formula.strip()

    if formula == "weighted":
        if not reward_cfg.weights:
            return 0.0
        total = 0.0
        weight_sum = 0.0
        for judge_name, weight in reward_cfg.weights.items():
            jv = _judge_value(per_judge, judge_name, score_range, raw_judges)
            if jv is not None:
                total += float(weight) * jv
                weight_sum += float(weight)
        return max(0.0, min(1.0, total)) if weight_sum > 0 else 0.0

    if formula in per_judge:
        rec = per_judge[formula]
        val = rec.get("value")
        if isinstance(val, (int, float)):
            return max(0.0, min(1.0, float(val)))
        return 0.0

    judge_vars: dict[str, float] = {}
    for name, rec in per_judge.items():
        jv = _judge_value(per_judge, name, score_range, raw_judges)
        if jv is not None:
            judge_vars[name] = jv

    safe_builtins = {
        "min": min, "max": max, "abs": abs, "round": round,
        "sum": sum, "len": len,
        "mean": lambda xs: sum(xs) / len(xs) if xs else 0.0,
    }
    try:
        ns = {**judge_vars, **safe_builtins}
        lines = [l for l in formula.splitlines() if l.strip()]
        if len(lines) > 1:
            exec(compile("\n".join(lines[:-1]), "<reward>", "exec"),
                 {"__builtins__": safe_builtins}, ns)
            result = eval(compile(lines[-1].strip(), "<reward>", "eval"),
                          {"__builtins__": {}}, ns)
        else:
            result = eval(formula, {"__builtins__": {}}, ns)
        return max(0.0, min(1.0, float(result)))
    except Exception as exc:
        import sys
        print(f"Warning: reward formula evaluation failed: {exc}",
              file=sys.stderr)
        return 0.0


def compose_reward(per_judge: dict, *,
                   score_min: float = _SCORE_MIN_DEFAULT,
                   score_max: float = _SCORE_MAX_DEFAULT,
                   reward_cfg: Optional[RewardConfig] = None) -> tuple[float, dict]:
    """Collapse per-judge results into an overall reward + flat metric dict.

    Resolution order:
    1. If reward_cfg is provided (from eval.yaml reward: section), use it.
    2. If a grpo_reward judge exists (legacy), use its value directly.
    3. Otherwise fall back to: boolean gates + average of normalized numerics.
    """
    metrics = _extract_metrics(per_judge)

    if reward_cfg is not None:
        reward = compute_reward_from_config(per_judge, reward_cfg)
        return reward, metrics

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

    reward_cfg = getattr(config, "reward", None)

    score_range = getattr(config, "score_range", None) or {}
    score_min = float(score_range.get("min", _SCORE_MIN_DEFAULT))
    score_max = float(score_range.get("max", _SCORE_MAX_DEFAULT))

    reward, metrics = compose_reward(per_judge,
                                     score_min=score_min, score_max=score_max,
                                     reward_cfg=reward_cfg)
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
