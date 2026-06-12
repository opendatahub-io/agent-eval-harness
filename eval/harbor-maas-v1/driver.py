"""harbor-maas-v1 reproducer: run /eval-anova over a subset of real
models-as-a-service PR tasks via the agent_eval.anova_runner bridge.

Harbor builds the repo into a container, so no local clone is needed.

Usage:
    python driver.py smoke                          # 1 task x sonnet (cheap gate)
    python driver.py matrix                         # full factorial + ANOVA
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
    "harbor_reward": {"type": "boolean", "gate": True},
    "solution_quality": {"type": "numeric", "weight": 1.0},
}


def _normalize(name, value, jtype):
    if value is None:
        return 0.0 if jtype != "check" else False
    if name == "solution_quality" and isinstance(value, (int, float)):
        return (value - 1) / 4.0  # 1..5 -> 0..1
    return value


def _detect_modules(oracle_path: Path) -> list[str]:
    """Parse oracle.diff to find affected Go modules (e.g., maas-api, maas-controller)."""
    modules = set()
    for line in oracle_path.read_text().splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            parts = line.split("/")
            if len(parts) >= 3:
                modules.add(parts[1])  # e.g., "maas-api" from "--- a/maas-api/..."
    return sorted(modules) or ["maas-api", "maas-controller"]


def _prepare_workspace(ws: Path, case_id: str, dataset_root: Path):
    """Create a harbor-compatible task directory layout.

    Harbor expects:
        workspace/
        ├── instruction.md
        ├── task.toml
        ├── environment/
        │   ├── Dockerfile
        │   └── oracle.diff
        └── tests/
            └── test.sh
    """
    oracle = dataset_root / case_id / "oracle.diff"
    instruction_src = dataset_root / case_id / "instruction.txt"

    if not oracle.exists() or oracle.stat().st_size == 0:
        print(f"    [prepare] {case_id}: no oracle.diff (skipping)", flush=True)
        return

    # instruction.md
    if instruction_src.exists():
        (ws / "instruction.md").write_text(instruction_src.read_text())
    else:
        (ws / "instruction.md").write_text(f"# Task {case_id}\n\nComplete the task.\n")

    # task.toml — cpus and memory_mb are required; harbor's OpenShift env
    # generates "None" / "NoneMi" as Kubernetes quantities if omitted.
    (ws / "task.toml").write_text(
        'schema_version = "1.3"\n'
        "\n"
        "[verifier]\n"
        "timeout_sec = 600.0\n"
        "\n"
        "[agent]\n"
        "timeout_sec = 900.0\n"
        "\n"
        "[environment]\n"
        "build_timeout_sec = 600.0\n"
        'network_mode = "public"\n'
        "cpus = 2\n"
        "memory_mb = 4096\n"
        "storage_mb = 10240\n"
    )

    # environment/
    env_dir = ws / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(oracle, env_dir / "oracle.diff")

    (env_dir / "Dockerfile").write_text(
        "FROM golang:latest\n"
        "RUN apt-get update && apt-get install -y git\n"
        "RUN git clone https://github.com/opendatahub-io/models-as-a-service /repo && \\\n"
        f"    cd /repo && git checkout {BASE_COMMIT}\n"
        "COPY oracle.diff /tmp/oracle.diff\n"
        "RUN cd /repo && git apply --reverse /tmp/oracle.diff && rm /tmp/oracle.diff\n"
        "WORKDIR /repo\n"
    )

    # tests/
    tests_dir = ws / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    modules = _detect_modules(oracle)
    test_lines = ["#!/bin/bash", "set -e"]
    for mod in modules:
        test_lines.append(f"cd /repo/{mod} && go test ./...")
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("\n".join(test_lines) + "\n")
    os.chmod(test_sh, 0o755)

    print(f"    [prepare] {case_id}: harbor task dir created "
          f"(modules: {', '.join(modules)})", flush=True)


def _bridge(run_dir: Path, log_prefix: str | None):
    config = EvalConfig.from_yaml(EVAL_YAML)
    return make_run_fn(
        config,
        runs_dir=run_dir,
        project_root=REPO,
        timeout_s=1800,
        max_budget_usd=10.0,
        log_prefix=log_prefix,
        normalize=_normalize,
        prepare_workspace=_prepare_workspace,
    )


def run_smoke():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / f"harbor-smoke-{ts}"
    run_fn = _bridge(run_dir, "smoke")
    cond = Condition(
        condition_id="smoke",
        levels={"model": "claude-sonnet-4-6", "context": "none"},
    )
    print(f"== SMOKE: sonnet x task-0031 -> {run_dir} ==", flush=True)
    res = run_cell(cond, "task-0031", 0, {}, JUDGE_CONFIGS, run_fn=run_fn)
    print(f"== judge_results={res.judge_results} composite={res.composite} ==")
    ok = bool(res.judge_results.get("harbor_reward")) and \
        res.judge_results.get("solution_quality") is not None
    print("SMOKE PASS" if ok else "SMOKE FAIL -- diagnose before matrix")
    return 0 if ok else 1


def run_matrix():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / f"anova-harbor-{ts}"
    run_fn = _bridge(run_dir, "anova")
    factors = {
        "model": ["claude-sonnet-4-6"],
        "context": ["cognee", "none"],
    }
    conditions = MatrixBuilder.expand_full_factorial(factors)
    print(f"== MATRIX: {len(conditions)} conditions x {len(TASKS)} tasks x 1 rep "
          f"= {len(conditions)*len(TASKS)} cells -> {run_dir} ==", flush=True)

    results, per_case = [], {}
    for cond in conditions:
        m = cond.levels["model"]
        ctx = cond.levels["context"]
        label = f"{m}/{ctx}"
        print(f"  condition: {label}", flush=True)
        for cid in TASKS:
            r = run_cell(cond, cid, 0, {}, JUDGE_CONFIGS, run_fn=run_fn)
            results.append(r)
            per_case.setdefault(label, {})[cid] = r.composite

    analysis = analyze_experiment(results, factors=["model", "context"])

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
         "context": r.condition.levels["context"],
         "composite": r.composite, "judge_results": r.judge_results}
        for r in results], indent=2))

    print("\n== CONDITION MEANS ==")
    for cs in sorted(analysis["condition_summaries"], key=lambda c: -c["mean"]):
        label = f"{cs.get('model', '?')}/{cs.get('context', '?')}"
        print(f"  {label:36s} mean={cs['mean']:.3f} std={cs['std']:.3f} n={cs['n']}")
    print("\n== ANOVA ==", json.dumps(ser(analysis["anova"]))[:300])
    print("run_dir:", run_dir)
    return run_dir


def main():
    parser = argparse.ArgumentParser(description="harbor-maas-v1 ANOVA reproducer")
    parser.add_argument("mode", choices=["smoke", "matrix"], default="smoke", nargs="?")
    args = parser.parse_args()

    if args.mode == "smoke":
        sys.exit(run_smoke())
    else:
        run_matrix()


if __name__ == "__main__":
    main()
