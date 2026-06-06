"""Parse Harbor job result.json into structured trial data."""

import json
from pathlib import Path

try:
    from evalhub.adapter import EvaluationResult
except ImportError:
    from agent_eval.evalhub.stubs import EvaluationResult


_METRIC_TYPES = {
    "reward": "benchmark",
    "mean_reward": "benchmark",
    "duration_s": "performance",
    "cost_usd": "cost",
    "input_tokens": "count",
    "output_tokens": "count",
    "env_build_seconds": "performance",
    "agent_exec_seconds": "performance",
    "verifier_seconds": "performance",
}


def parse_job(job_dir: Path) -> dict:
    """Parse a Harbor job directory and return structured results.

    Args:
        job_dir: Path to the job directory containing result.json.

    Returns:
        Dict with keys: job_id, trials, mean_reward, n_completed, n_errored.
        Each trial has task_name and metrics (list of EvaluationResult).

    Raises:
        FileNotFoundError: If result.json doesn't exist in job_dir.
    """
    result_path = job_dir / "result.json"
    if not result_path.exists():
        raise FileNotFoundError(f"No result.json found in {job_dir}")

    with open(result_path) as f:
        raw = json.load(f)

    trials = []
    completed_rewards = []
    n_errored = 0

    for raw_trial in raw.get("trials", []):
        status = raw_trial.get("status", "completed")

        if status == "errored":
            n_errored += 1
            trials.append({
                "task_name": raw_trial["task_name"],
                "metrics": [],
            })
            continue

        reward = raw_trial.get("reward")
        if reward is not None:
            completed_rewards.append(reward)

        metrics = []
        metrics.append(EvaluationResult(
            metric_name="reward",
            metric_value=reward if reward is not None else 0.0,
            metric_type="benchmark",
        ))

        for key, value in raw_trial.get("metrics", {}).items():
            if value is not None:
                metrics.append(EvaluationResult(
                    metric_name=key,
                    metric_value=value,
                    metric_type=_METRIC_TYPES.get(key, "float"),
                ))

        trials.append({
            "task_name": raw_trial["task_name"],
            "metrics": metrics,
        })

    mean_reward = (
        sum(completed_rewards) / len(completed_rewards)
        if completed_rewards
        else None
    )

    return {
        "job_id": raw.get("job_id", ""),
        "trials": trials,
        "mean_reward": mean_reward,
        "n_completed": len(completed_rewards),
        "n_errored": n_errored,
    }
