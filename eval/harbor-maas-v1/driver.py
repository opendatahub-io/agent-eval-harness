"""harbor-maas-v1 reproducer: run /eval-anova over a subset of real
models-as-a-service PR tasks via the agent_eval.anova_runner bridge.

Prerequisites:
    1. Clone opendatahub-io/models-as-a-service at commit a24c8c8 somewhere
    2. Set HARBOR_REPO_CLONE to that path (or pass --repo-clone)

Usage:
    python driver.py smoke                          # 1 task × sonnet (cheap gate)
    python driver.py matrix                         # 3 models × 4 tasks + ANOVA
    python driver.py matrix --repo-clone /path/to/models-as-a-service
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "skills" / "eval-anova" / "scripts"))

from agent_eval.anova_runner import make_run_fn  # noqa: E402
from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.matrix import Condition, MatrixBuilder  # noqa: E402
from orchestrate import run_cell  # noqa: E402
from analyze import analyze_experiment  # noqa: E402

HERE = Path(__file__).resolve().parent
EVAL_YAML = HERE / "eval.yaml"
BASE_COMMIT = "a24c8c8"
RUNS_DIR = REPO / "eval" / "runs"

TASKS = ["task-0031", "task-0034", "task-0008", "task-0010"]

JUDGE_CONFIGS = {
    "has_code_changes": {"type": "boolean", "gate": True},
    "solution_quality": {"type": "numeric", "weight": 1.0},
}


def _normalize(name, value, jtype):
    if name == "solution_quality" and isinstance(value, (int, float)):
        return (value - 1) / 4.0  # 1..5 -> 0..1
    return value


def _prepare_workspace(ws: Path, case_id: str, dataset_root: Path):
    """Reverse-apply the oracle so the agent faces the pre-PR state."""
    oracle = dataset_root / case_id / "oracle.diff"
    if not oracle.exists() or oracle.stat().st_size == 0:
        return
    chk = subprocess.run(["git", "-C", str(ws), "apply", "--reverse",
                          "--check", str(oracle)], capture_output=True, text=True,
                         timeout=30)
    if chk.returncode != 0:
        print(f"    [prepare] {case_id}: oracle does not reverse-apply "
              f"(skipping): {chk.stderr.strip()[:120]}", flush=True)
        return
    apply = subprocess.run(["git", "-C", str(ws), "apply", "--reverse", str(oracle)],
                           capture_output=True, text=True, timeout=30)
    if apply.returncode != 0:
        print(f"    [prepare] {case_id}: reverse-apply failed "
              f"(skipping): {apply.stderr.strip()[:120]}", flush=True)
        return
    print(f"    [prepare] {case_id}: reverse-applied oracle -> pre-PR state",
          flush=True)


def _bridge(run_dir: Path, repo_clone: Path, log_prefix: str | None):
    config = EvalConfig.from_yaml(EVAL_YAML)
    return make_run_fn(
        config,
        runs_dir=run_dir,
        repo_clone=repo_clone,
        base_commit=BASE_COMMIT,
        project_root=REPO,
        timeout_s=900,
        max_budget_usd=5.0,
        log_prefix=log_prefix,
        normalize=_normalize,
        prepare_workspace=_prepare_workspace,
    )


def run_smoke(repo_clone: Path):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / f"harbor-smoke-{ts}"
    run_fn = _bridge(run_dir, repo_clone, "smoke")
    cond = Condition(condition_id="smoke", levels={"model": "claude-sonnet-4-6"})
    print(f"== SMOKE: sonnet x task-0031 -> {run_dir} ==", flush=True)
    res = run_cell(cond, "task-0031", 0, {}, JUDGE_CONFIGS, run_fn=run_fn)
    print(f"== judge_results={res.judge_results} composite={res.composite} ==")
    ok = bool(res.judge_results.get("has_code_changes")) and \
        res.judge_results.get("solution_quality") is not None
    print("SMOKE PASS" if ok else "SMOKE FAIL — diagnose before matrix")
    return 0 if ok else 1


def run_matrix(repo_clone: Path):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / f"anova-harbor-{ts}"
    run_fn = _bridge(run_dir, repo_clone, "anova")
    factors = {"model": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"]}
    conditions = MatrixBuilder.expand_full_factorial(factors)
    print(f"== MATRIX: {len(conditions)} models x {len(TASKS)} tasks x 1 rep "
          f"= {len(conditions)*len(TASKS)} cells -> {run_dir} ==", flush=True)

    results, per_case = [], {}
    for cond in conditions:
        m = cond.levels["model"]
        print(f"  condition: {m}", flush=True)
        for cid in TASKS:
            r = run_cell(cond, cid, 0, {}, JUDGE_CONFIGS, run_fn=run_fn)
            results.append(r)
            per_case.setdefault(m, {})[cid] = r.composite

    analysis = analyze_experiment(results, factors=["model"])

    def ser(o):
        if isinstance(o, dict): return {k: ser(v) for k, v in o.items()}
        if isinstance(o, list): return [ser(i) for i in o]
        if isinstance(o, float) and o != o: return None
        if hasattr(o, "to_dict"): return o.to_dict()
        return o

    doc = ser(analysis)
    doc["run_id"] = run_dir.name
    doc["timestamp"] = ts
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "analysis.json").write_text(json.dumps(doc, indent=2))
    (run_dir / "all_results.json").write_text(json.dumps([
        {"case_id": r.case_id, "model": r.condition.levels["model"],
         "composite": r.composite, "judge_results": r.judge_results}
        for r in results], indent=2))

    print("\n== CONDITION MEANS ==")
    for cs in sorted(analysis["condition_summaries"], key=lambda c: -c["mean"]):
        print(f"  {cs.get('model'):24s} mean={cs['mean']:.3f} std={cs['std']:.3f} n={cs['n']}")
    print("\n== ANOVA ==", json.dumps(ser(analysis["anova"]))[:300])
    print("run_dir:", run_dir)
    return run_dir


def main():
    parser = argparse.ArgumentParser(description="harbor-maas-v1 ANOVA reproducer")
    parser.add_argument("mode", choices=["smoke", "matrix"], default="smoke", nargs="?")
    parser.add_argument("--repo-clone", type=Path,
                        default=os.environ.get("HARBOR_REPO_CLONE"),
                        help="Path to models-as-a-service clone at commit a24c8c8 "
                             "(or set HARBOR_REPO_CLONE env var)")
    args = parser.parse_args()

    if not args.repo_clone:
        parser.error("--repo-clone or HARBOR_REPO_CLONE is required.\n"
                     "Clone opendatahub-io/models-as-a-service and checkout a24c8c8.")
    repo_clone = Path(args.repo_clone).resolve()
    if not (repo_clone / ".git").exists():
        parser.error(f"{repo_clone} is not a git repository")

    if args.mode == "smoke":
        sys.exit(run_smoke(repo_clone))
    else:
        run_matrix(repo_clone)


if __name__ == "__main__":
    main()
