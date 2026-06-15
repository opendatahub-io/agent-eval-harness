"""Run an eval on Harbor and map results into the harness run-dir layout.

This is the `/eval-run --runner harbor` path: instead of staging workspaces and
calling a runner per case, it generates Harbor task packages, invokes
`harbor run` (Podman locally / Kubernetes on OpenShift), then maps the Harbor
job's per-case verifier output into the SAME `run_result.json` + `summary.yaml`
shape the local scorer writes — so `report.py`, regression detection, and the
MLflow logger consume Harbor runs unchanged.

Per-case judging happens in-container (the reward bridge as the Harbor verifier),
so this step does not re-run judges; it aggregates their results. Pairwise stays
a suite-level step on top (run separately over two run dirs).
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig
from agent_eval.harbor import results as results_mod
from agent_eval.harbor import tasks as tasks_mod
from agent_eval.harbor.reward import _load_score_module

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_dotenv() -> None:
    """Load .env from cwd or any ancestor, if present.

    Uses os.environ.setdefault so explicit exports always win over .env values.
    Supports only simple ``KEY=VALUE``, ``KEY="VALUE"``, and ``KEY='VALUE'`` forms.
    Does NOT handle: ``export KEY=VALUE``, inline comments (``KEY=val # comment``),
    multiline values, or escaped quotes inside values.
    """
    for p in [Path.cwd(), *Path.cwd().parents]:
        env_file = p / ".env"
        if env_file.is_file():
            try:
                for raw in env_file.read_text().splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k:
                        os.environ.setdefault(k, v)
            except OSError:
                pass
            break
# Mapping from eval.yaml runner.type to Harbor agent name.
# runner.type is an agent-eval-harness concept; Harbor --agent is Harbor's.
# Names that match directly (claude-code) need no mapping.
_RUNNER_TO_HARBOR_AGENT = {
    "claude-code": "claude-code",
    "cli": None,            # CLI runner is generic — user must pass --agent explicitly
    "responses-api": None,  # no Harbor equivalent
}
_DEFAULT_AGENT = "claude-code"
_DEFAULT_ENV_IMPORT = "agent_eval.harbor.podman:PodmanEnvironment"


def _judge_types(config: EvalConfig) -> dict:
    """Map judge name -> type, mirroring score.load_judges' discrimination."""
    types = {}
    for jc in config.judges:
        if jc.name == "pairwise":
            continue
        if jc.check:
            t = "check"
        elif jc.prompt or jc.prompt_file:
            t = "llm"
        elif jc.module and jc.function:
            t = "code"
        elif jc.builtin:
            t = "builtin"
        else:
            t = "check"
        types[jc.name] = t
    return types


def build_summary(parsed_job: dict, config: EvalConfig) -> dict:
    """Map a parsed Harbor job into the harness summary shape.

    Returns ``{"judges": {name: {mean, pass_rate}}, "per_case": {case_id:
    {judge: {value, rationale, judge_type}}}}`` — identical to what
    ``score.py``'s ``cmd_judges`` writes, so downstream code is agnostic to
    whether judging ran locally or in a Harbor verifier.
    """
    types = _judge_types(config)
    per_case: dict = {}
    agg_values: dict = {}

    for trial in parsed_job["trials"]:
        case_judges = {}
        for name, rec in trial.get("per_judge", {}).items():
            value = rec.get("value")
            case_judges[name] = {
                "value": value,
                "rationale": rec.get("rationale", "") or rec.get("error", ""),
                "judge_type": types.get(name, "check"),
            }
            if value is not None:
                agg_values.setdefault(name, []).append(value)
        per_case[trial["case_id"]] = case_judges

    judges: dict = {}
    for name, vals in agg_values.items():
        if vals and all(isinstance(v, bool) for v in vals):
            rate = sum(vals) / len(vals)
            judges[name] = {"mean": rate, "pass_rate": rate}
        elif vals and all(isinstance(v, (int, float)) for v in vals):
            judges[name] = {"mean": sum(vals) / len(vals), "pass_rate": None}
        else:
            judges[name] = {"mean": None, "pass_rate": None}

    return {"judges": judges, "per_case": per_case}


def _count_task_packages(tasks_dir: Path) -> int:
    """Count Harbor task packages (subdirs with a task.toml) under tasks_dir."""
    if not tasks_dir.is_dir():
        return 0
    return sum(1 for d in tasks_dir.iterdir()
               if d.is_dir() and (d / "task.toml").is_file())


def _load_report_module():
    """Load report.py from the eval-run skill (by path)."""
    path = _REPO_ROOT / "skills" / "eval-run" / "scripts" / "report.py"
    spec = importlib.util.spec_from_file_location("agent_eval_report", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _copy_case_artifacts(parsed: dict, output_dir: Path) -> None:
    """Copy each trial's downloaded artifacts into the run dir's cases/<id>/.

    The task's verifier (test.sh) copies the agent's produced artifacts to
    /logs/verifier/artifacts, which Harbor downloads to
    <trial>/verifier/artifacts. Mirror them under cases/<case_id>/ (preserving
    the artifacts/ tree) so report.py renders per-case Output files.
    """
    import shutil
    for trial in parsed["trials"]:
        src = Path(trial.get("trial_path", "")) / "verifier" / "artifacts"
        if not src.is_dir():
            continue
        dst = output_dir / "cases" / trial["case_id"] / "artifacts"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)


def _write_report(config_path: Path, output_dir: Path, summary: dict,
                  run_meta: dict) -> None:
    """Render report.html with the same generator the local path uses."""
    try:
        raw_cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        # Resolve dataset.path to absolute (report renders case inputs from it).
        ds = raw_cfg.get("dataset")
        if isinstance(ds, dict) and ds.get("path") and not Path(ds["path"]).is_absolute():
            ds["path"] = str((Path(config_path).resolve().parent / ds["path"]).resolve())
        report = _load_report_module()
        html = report.generate_report(
            config=raw_cfg, summary=summary, run_result=run_meta,
            run_dir=output_dir, review=None, baseline_dir=None,
            baseline_summary=None, baseline_result=None,
        )
        (output_dir / "report.html").write_text(html)
        print(f"report: {output_dir}/report.html")
    except Exception as exc:  # report is best-effort; don't fail the run
        print(f"WARNING: report generation failed: {exc}", file=sys.stderr)


def run_eval_on_harbor(
    config_path: Path,
    *,
    image: str | None = None,
    model: str,
    output_dir: Path,
    tasks_dir: Path,
    jobs_dir: Path,
    arguments: str | None = None,
    skill: str | None = None,
    judge_model: str | None = None,
    cases: list[str] | None = None,
    n_concurrent: int = 1,
    workdir: str = "/workspace",
    agent_name: str | None = None,
    env_import_path: str | None = _DEFAULT_ENV_IMPORT,
    harbor_bin: str = "harbor",
    regenerate: bool = False,
) -> int:
    """Run an eval on Harbor and map results. Returns an exit code
    (non-zero if regression thresholds are violated).

    Tasks: if ``tasks_dir`` already holds Harbor task packages (e.g. emitted by
    ``/eval-dataset``), they are used as-is; otherwise they're generated now
    (one-shot convenience). ``regenerate=True`` forces regeneration.
    """
    config = EvalConfig.from_yaml(config_path)

    # Resolve Harbor agent from eval.yaml runner.type if not explicitly passed.
    if not agent_name:
        mapped = _RUNNER_TO_HARBOR_AGENT.get(config.runner.type)
        if mapped:
            agent_name = mapped
        elif config.runner.type in _RUNNER_TO_HARBOR_AGENT:
            raise ValueError(
                f"runner.type '{config.runner.type}' in eval.yaml has no Harbor "
                f"agent equivalent. Pass --agent explicitly (e.g. --agent opencode).")
        else:
            agent_name = config.runner.type

    # 1. Use pre-generated task packages if present; else generate them.
    existing = _count_task_packages(tasks_dir)
    if existing and not regenerate:
        print(f"Using {existing} pre-generated task package(s) in {tasks_dir} "
              f"(skipping generation; --regenerate to force)", file=sys.stderr)
    else:
        if not image:
            raise ValueError(
                "No tasks in --tasks-dir and no --image to generate them. "
                "Either pre-generate with /eval-dataset (scripts/harbor.py) "
                "or pass --image.")
        tasks_mod.generate_tasks(
            config, Path(config_path), tasks_dir, image,
            arguments=arguments, skill=skill, workdir=workdir, cases=cases,
            judge_model=judge_model,
        )

    # 2. Run on Harbor (one job over the tasks dir).
    cmd = [
        harbor_bin, "run", "-p", str(tasks_dir),
        "-a", agent_name, "-m", model,
        "-n", str(n_concurrent), "-o", str(jobs_dir),
    ]
    if env_import_path:
        cmd += ["--environment-import-path", env_import_path]
    print(f"harbor: {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        print(f"harbor run exited {proc.returncode}", file=sys.stderr)
        return proc.returncode

    # 3. Locate the job dir Harbor just wrote (newest under jobs_dir).
    job_dirs = sorted((d for d in jobs_dir.iterdir() if d.is_dir()),
                      key=lambda d: d.stat().st_mtime)
    if not job_dirs:
        print(f"No Harbor job dir under {jobs_dir}", file=sys.stderr)
        return 1
    parsed = results_mod.parse_job(job_dirs[-1])

    # 4. Map into the harness run-dir layout.
    summary = build_summary(parsed, config)
    output_dir.mkdir(parents=True, exist_ok=True)
    _copy_case_artifacts(parsed, output_dir)
    run_meta = {
        "exit_code": 0 if parsed["n_errored"] == 0 else 1,
        "execution_mode": "harbor",
        "agent": f"harbor:{agent_name}",
        "model": model,
        "num_cases": parsed["n_completed"],
        "mean_reward": parsed["mean_reward"],
        "cost_usd": parsed.get("cost_usd"),
        "token_usage": parsed.get("token_usage"),
        "harbor_job_dir": parsed["job_dir"],
    }
    (output_dir / "run_result.json").write_text(json.dumps(run_meta, indent=2) + "\n")
    (output_dir / "summary.yaml").write_text(
        yaml.safe_dump({"run_id": output_dir.name, **summary},
                       sort_keys=False, allow_unicode=True))

    # 4b. Generate the HTML report (same renderer as the local path).
    _write_report(config_path, output_dir, summary, run_meta)

    # 5. Regression detection (suite-level), mirroring score.py regression.
    score = _load_score_module()
    regressions = score.detect_regressions(summary["judges"], config.thresholds)
    if regressions:
        print(f"REGRESSIONS: {len(regressions)} detected", file=sys.stderr)
        for r in regressions:
            print(f"  [{r.judge_name}] {r.metric}: {r.baseline_value} -> {r.current_value}",
                  file=sys.stderr)
        return 1
    print(f"Mapped {parsed['n_completed']} case(s) → {output_dir}/summary.yaml "
          f"(mean_reward={parsed['mean_reward']}); REGRESSIONS: 0")
    return 0


def main() -> None:
    _load_dotenv()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True)
    p.add_argument("--image", default=None,
                   help="Task image (required only when generating tasks; "
                        "pre-generated tasks already reference their image)")
    p.add_argument("--model", required=True)
    p.add_argument("--output", required=True, help="Harness run dir to write")
    p.add_argument("--tasks-dir", required=True)
    p.add_argument("--jobs-dir", required=True)
    p.add_argument("--arguments", default=None)
    p.add_argument("--skill", default=None)
    p.add_argument("--judge-model", default=None)
    p.add_argument("--cases", nargs="*", default=None)
    p.add_argument("--n-concurrent", type=int, default=1)
    p.add_argument("--workdir", default="/workspace")
    p.add_argument("--agent", default=None,
                   help="Harbor agent name (default: from runner.type in eval.yaml; "
                        "e.g. claude-code, opencode)")
    p.add_argument("--environment-import-path", default=_DEFAULT_ENV_IMPORT,
                   help="Custom Harbor environment import path (default: Podman; "
                        "omit to use Harbor's built-in docker env)")
    p.add_argument("--regenerate", action="store_true",
                   help="Regenerate task packages even if --tasks-dir already has them "
                        "(default: reuse pre-generated tasks, e.g. from /eval-dataset)")
    args = p.parse_args()

    code = run_eval_on_harbor(
        Path(args.config), image=args.image, model=args.model,
        output_dir=Path(args.output), tasks_dir=Path(args.tasks_dir),
        jobs_dir=Path(args.jobs_dir), arguments=args.arguments, skill=args.skill,
        judge_model=args.judge_model, cases=args.cases,
        n_concurrent=args.n_concurrent, workdir=args.workdir,
        agent_name=args.agent,
        env_import_path=args.environment_import_path,
        regenerate=args.regenerate,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
