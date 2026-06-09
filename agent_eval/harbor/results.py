"""Parse a Harbor job directory into structured per-case results.

The agent-eval-harness side of the Harbor boundary: after `harbor run` (local
Podman or OpenShift K8s) produces a job directory, this reads each trial's
verifier output — `reward.json` (the flat metric contract) and `judges.json`
(our richer sidecar with per-judge values + rationales) — into a shape the suite
layer can feed to MLflow and the HTML report.

It intentionally reads the per-trial verifier files our reward bridge writes
(stable contract) rather than Harbor's top-level `result.json` stats (which vary
by Harbor version). Pairwise/regression remain suite-level above this.
"""

import json
from pathlib import Path


def _case_id_from_dir(trial_dir: Path) -> str:
    """Harbor names trial dirs '<task>__<shortid>'. Strip the trailing id."""
    name = trial_dir.name
    return name.rsplit("__", 1)[0] if "__" in name else name


def parse_trial(trial_dir: Path) -> dict | None:
    """Parse one Harbor trial directory. Returns None if it has no reward."""
    reward_path = trial_dir / "verifier" / "reward.json"
    if not reward_path.is_file():
        return None

    try:
        reward_data = json.loads(reward_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    metrics = {k: v for k, v in reward_data.items() if k != "reward"}
    record = {
        "case_id": _case_id_from_dir(trial_dir),
        "trial_dir": trial_dir.name,
        "trial_path": str(trial_dir),
        "reward": reward_data.get("reward"),
        "metrics": metrics,
        "per_judge": {},
        "errored": False,
        "cost_usd": None,
        "token_usage": None,
    }

    # Agent execution metrics from Harbor's trial result.json (cost/tokens).
    trial_result = trial_dir / "result.json"
    if trial_result.is_file():
        try:
            ar = (json.loads(trial_result.read_text()).get("agent_result") or {})
            record["cost_usd"] = ar.get("cost_usd")
            if any(ar.get(k) is not None for k in
                   ("n_input_tokens", "n_output_tokens", "n_cache_tokens")):
                record["token_usage"] = {
                    "input": ar.get("n_input_tokens"),
                    "output": ar.get("n_output_tokens"),
                    "cache_read": ar.get("n_cache_tokens"),
                }
        except (json.JSONDecodeError, OSError):
            pass

    # Richer sidecar (values + rationales) when present.
    judges_path = trial_dir / "verifier" / "judges.json"
    if judges_path.is_file():
        try:
            record["per_judge"] = json.loads(judges_path.read_text()).get("per_judge", {})
        except (json.JSONDecodeError, OSError):
            pass

    # Trial-level error flag from Harbor (exception.txt present == errored).
    if (trial_dir / "exception.txt").is_file():
        record["errored"] = True

    return record


def parse_job(job_dir: Path) -> dict:
    """Parse a Harbor job directory into aggregated per-case results.

    Returns ``{trials, mean_reward, n_completed, n_errored, aggregated}`` where
    ``aggregated`` maps each metric name to ``{values, mean}`` across trials —
    the same shape the local scorer's ``aggregated`` uses, so the report/MLflow
    code can consume Harbor runs uniformly.
    """
    job_dir = Path(job_dir)
    trials = []
    for child in sorted(job_dir.iterdir()):
        if not child.is_dir():
            continue
        trial = parse_trial(child)
        if trial is not None:
            trials.append(trial)

    rewards = [t["reward"] for t in trials if isinstance(t.get("reward"), (int, float))]
    mean_reward = sum(rewards) / len(rewards) if rewards else None

    # Aggregate each metric across trials (mean), mirroring score.py's shape.
    aggregated: dict[str, dict] = {}
    for trial in trials:
        for name, value in trial["metrics"].items():
            if isinstance(value, (int, float)):
                aggregated.setdefault(name, {"values": []})["values"].append(value)
    for name, agg in aggregated.items():
        vals = agg["values"]
        agg["mean"] = sum(vals) / len(vals) if vals else None

    # Aggregate agent cost/tokens across trials for run-level metrics.
    total_cost = sum(t["cost_usd"] for t in trials
                     if isinstance(t.get("cost_usd"), (int, float))) or None
    token_usage: dict = {}
    for t in trials:
        for k, v in (t.get("token_usage") or {}).items():
            if isinstance(v, (int, float)):
                token_usage[k] = token_usage.get(k, 0) + v

    return {
        "job_dir": str(job_dir),
        "trials": trials,
        "mean_reward": mean_reward,
        "n_completed": len(trials),
        "n_errored": sum(1 for t in trials if t["errored"]),
        "aggregated": aggregated,
        "cost_usd": total_cost,
        "token_usage": token_usage or None,
    }
