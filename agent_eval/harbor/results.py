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


def _extract_transcript_metrics(transcript_path: Path) -> dict:
    """Extract cost, tokens, turns, duration, version from a stream-json transcript."""
    result: dict = {
        "cost_usd": None, "token_usage": None,
        "num_turns": None, "duration_s": None, "agent_version": None,
    }
    if not transcript_path.is_file():
        return result
    try:
        for line in transcript_path.read_text().splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (ev.get("type") == "system" and ev.get("subtype") == "init"
                    and not result["agent_version"]):
                result["agent_version"] = ev.get("claude_code_version")
            if ev.get("type") == "result":
                cost = ev.get("total_cost_usd")
                if cost is not None:
                    result["cost_usd"] = float(cost)
                result["num_turns"] = ev.get("num_turns")
                duration_ms = ev.get("duration_ms")
                if duration_ms is not None:
                    result["duration_s"] = duration_ms / 1000
                usage = ev.get("usage", {})
                if usage:
                    result["token_usage"] = {
                        "input": usage.get("input_tokens"),
                        "output": usage.get("output_tokens"),
                        "cache_read": usage.get("cache_read_input_tokens"),
                        "cache_create": usage.get("cache_creation_input_tokens"),
                    }
    except OSError:
        pass
    return result


def parse_trial(trial_dir: Path) -> dict | None:
    """Parse one Harbor trial directory. Returns None if it has no reward.

    Supports both single-step trials (reward at ``verifier/reward.json``)
    and multi-step trials (per-step rewards under ``steps/<name>/verifier/``).
    """
    steps_dir = trial_dir / "steps"
    if steps_dir.is_dir() and any(steps_dir.iterdir()):
        return _parse_multi_step_trial(trial_dir, steps_dir)

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

    # Enrich from the agent transcript (turns, duration, version are only
    # available there; cost/tokens fall back to transcript when result.json
    # doesn't have them).
    transcript = trial_dir / "agent" / "claude-code.txt"
    extracted = _extract_transcript_metrics(transcript)
    if record["cost_usd"] is None:
        record["cost_usd"] = extracted["cost_usd"]
    if record["token_usage"] is None:
        record["token_usage"] = extracted["token_usage"]
    elif extracted.get("token_usage"):
        for k, v in extracted["token_usage"].items():
            if v is not None and record["token_usage"].get(k) is None:
                record["token_usage"][k] = v
    record["num_turns"] = extracted.get("num_turns")
    record["duration_s"] = extracted.get("duration_s")
    record["agent_version"] = extracted.get("agent_version")

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


def _parse_multi_step_trial(trial_dir: Path, steps_dir: Path) -> dict | None:
    """Parse a multi-step Harbor trial into the same shape as a single-step one.

    Aggregates per-step rewards (mean), cost (sum), and tokens (sum) into
    a single record. Each step's reward becomes a judge keyed by step name.
    """
    step_dirs = sorted(d for d in steps_dir.iterdir() if d.is_dir())
    if not step_dirs:
        return None

    rewards = []
    per_judge: dict = {}
    total_cost = 0.0
    total_turns = 0
    total_duration = 0.0
    token_totals: dict = {}
    agent_version = None

    for step_dir in step_dirs:
        step_name = step_dir.name

        reward_path = step_dir / "verifier" / "reward.json"
        step_reward = None
        if reward_path.is_file():
            try:
                rd = json.loads(reward_path.read_text())
                step_reward = rd.get("reward")
            except (json.JSONDecodeError, OSError):
                pass

        if isinstance(step_reward, (int, float)):
            rewards.append(step_reward)

        transcript = step_dir / "agent" / "claude-code.txt"
        extracted = _extract_transcript_metrics(transcript)
        step_cost = extracted.get("cost_usd")
        step_turns = extracted.get("num_turns")
        step_duration = extracted.get("duration_s")
        if not agent_version:
            agent_version = extracted.get("agent_version")

        rationale_parts = []
        if step_turns:
            rationale_parts.append(f"{step_turns} turns")
        if step_cost:
            rationale_parts.append(f"${step_cost:.2f}")
        if step_duration:
            rationale_parts.append(f"{step_duration:.0f}s")
        rationale = ", ".join(rationale_parts) if rationale_parts else ""

        per_judge[step_name] = {
            "value": step_reward if step_reward is not None else False,
            "rationale": rationale,
            "judge_type": "step",
        }

        if isinstance(step_cost, (int, float)):
            total_cost += step_cost
        if isinstance(step_turns, (int, float)):
            total_turns += int(step_turns)
        if isinstance(step_duration, (int, float)):
            total_duration += step_duration
        for k, v in (extracted.get("token_usage") or {}).items():
            if isinstance(v, (int, float)):
                token_totals[k] = token_totals.get(k, 0) + v

    mean_reward = sum(rewards) / len(rewards) if rewards else None

    # If any step has a judges.json (from the full judge engine), merge those
    # judges into per_judge — they provide richer scoring than step rewards.
    for step_dir in reversed(step_dirs):
        judges_path = step_dir / "verifier" / "judges.json"
        if judges_path.is_file():
            try:
                jdata = json.loads(judges_path.read_text())
                engine_judges = jdata.get("per_judge", {})
                if engine_judges:
                    per_judge.update(engine_judges)
                    engine_reward = jdata.get("reward")
                    if isinstance(engine_reward, (int, float)):
                        mean_reward = engine_reward
                    break
            except (json.JSONDecodeError, OSError):
                pass

    return {
        "case_id": _case_id_from_dir(trial_dir),
        "trial_dir": trial_dir.name,
        "trial_path": str(trial_dir),
        "reward": mean_reward,
        "metrics": {s.name: per_judge[s.name]["value"] for s in step_dirs},
        "per_judge": per_judge,
        "errored": (trial_dir / "exception.txt").is_file(),
        "cost_usd": total_cost if total_cost > 0 else None,
        "token_usage": token_totals or None,
        "num_turns": total_turns if total_turns > 0 else None,
        "duration_s": total_duration if total_duration > 0 else None,
        "agent_version": agent_version,
    }


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
    cost_values = [t["cost_usd"] for t in trials
                   if isinstance(t.get("cost_usd"), (int, float))]
    total_cost = sum(cost_values) if cost_values else None
    token_usage: dict = {}
    for t in trials:
        for k, v in (t.get("token_usage") or {}).items():
            if isinstance(v, (int, float)):
                token_usage[k] = token_usage.get(k, 0) + v

    # Aggregate turns, duration, and pick agent version from trials.
    turn_values = [t["num_turns"] for t in trials
                   if isinstance(t.get("num_turns"), (int, float))]
    total_turns = sum(turn_values) if turn_values else None
    dur_values = [t["duration_s"] for t in trials
                  if isinstance(t.get("duration_s"), (int, float))]
    total_duration = sum(dur_values) if dur_values else None
    agent_version = next((t["agent_version"] for t in trials
                          if t.get("agent_version")), None)

    return {
        "job_dir": str(job_dir),
        "trials": trials,
        "mean_reward": mean_reward,
        "n_completed": len(trials),
        "n_errored": sum(1 for t in trials if t["errored"]),
        "aggregated": aggregated,
        "cost_usd": total_cost,
        "token_usage": token_usage or None,
        "num_turns": total_turns,
        "duration_s": total_duration,
        "agent_version": agent_version,
    }
