"""EvalHub runner for ``/eval-run --runner evalhub``.

Submits an evaluation job to an EvalHub server, polls for completion, and maps
the results back into the harness-native ``summary.yaml`` + ``report.html``.
From the user's perspective it's the same as ``--runner local`` or ``--runner
harbor`` — one command, different substrate.

The flow:
1. Create K8s ConfigMaps (project + eval config) via ``k8s_resources``
2. Submit a job to EvalHub (via the SDK client)
3. Poll for completion (``wait_for_completion``)
4. Map ``BenchmarkResult.metrics`` → ``summary.yaml``
5. Generate the HTML report + regression check

Requires: ``pip install eval-hub-sdk`` and an EvalHub server (local or on
OpenShift). The server creates the Job pod; the adapter runs in-process inside
it (no sub-pods, no Harbor).
"""

import argparse
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_report_module():
    path = _REPO_ROOT / "skills" / "eval-run" / "scripts" / "report.py"
    spec = importlib.util.spec_from_file_location("agent_eval_report", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_score_module():
    path = _REPO_ROOT / "skills" / "eval-run" / "scripts" / "score.py"
    spec = importlib.util.spec_from_file_location("agent_eval_score", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _metrics_to_summary(metrics: dict, config: EvalConfig) -> dict:
    """Map EvalHub BenchmarkResult.metrics → harness summary shape.

    EvalHub metrics come from ``results_mapper.py``: each judge name maps to
    its mean score (float), plus built-in metrics (exit_code, duration_seconds,
    cost_usd, num_turns, num_examples_evaluated).
    """
    judge_names = {jc.name for jc in config.judges if jc.name != "pairwise"}
    judges: dict = {}
    for name in judge_names:
        value = metrics.get(name)
        if value is not None:
            if isinstance(value, bool) or (isinstance(value, float) and value in (0.0, 1.0)):
                judges[name] = {"mean": value, "pass_rate": value}
            else:
                judges[name] = {"mean": value, "pass_rate": None}
    return {"judges": judges, "per_case": {}}


def run_eval_on_evalhub(
    config_path: Path,
    *,
    model: str,
    output_dir: Path,
    evalhub_url: str | None = None,
    evalhub_token: str | None = None,
    namespace: str | None = None,
    provider_id: str = "agent-eval",
    benchmark_id: str | None = None,
    project_dir: str | None = None,
    timeout: float | None = None,
    poll_interval: float = 10.0,
) -> int:
    """Submit an eval to EvalHub, wait, map results. Returns exit code."""
    try:
        from evalhub import (
            EvalHubClient, JobSubmissionRequest, ModelConfig, BenchmarkConfig)
    except ImportError:
        print("ERROR: eval-hub-sdk is required for --runner evalhub. "
              "Install with: pip install eval-hub-sdk", file=sys.stderr)
        return 1

    config = EvalConfig.from_yaml(config_path)

    # Resolve EvalHub connection
    url = evalhub_url or os.environ.get("EVALHUB_URL", "http://localhost:8080")
    token = evalhub_token or os.environ.get("EVALHUB_TOKEN")
    ns = namespace or os.environ.get("AGENT_EVAL_K8S_NAMESPACE", "default")

    # 1. Create K8s ConfigMaps for eval config + project (if on K8s)
    params: dict = {"namespace": ns}
    try:
        from agent_eval.harbor.k8s_resources import (
            create_eval_configmap, create_project_configmap)

        cm_name = f"agent-eval-{config.name or 'eval'}"
        create_eval_configmap(config_path, cm_name, ns)
        params["eval_configmap"] = cm_name
        print(f"Created ConfigMap {cm_name} in {ns}", file=sys.stderr)

        if project_dir:
            proj_cm = f"agent-eval-project-{config.name or 'eval'}"
            create_project_configmap(Path(project_dir), proj_cm, ns)
            params["project_configmap"] = proj_cm
            print(f"Created ConfigMap {proj_cm} in {ns}", file=sys.stderr)
    except Exception as exc:
        log.warning("ConfigMap creation skipped: %s", exc)

    # 2. Submit job to EvalHub
    client = EvalHubClient(base_url=url, auth_token=token)
    try:
        return _run_with_client(client, config, config_path, ns, provider_id,
                                benchmark_id, model, params, output_dir,
                                timeout, poll_interval)
    finally:
        client.close()


def _run_with_client(client, config, config_path, ns, provider_id,
                     benchmark_id, model, params, output_dir,
                     timeout, poll_interval):
    """Inner function so the client is always closed."""
    bench_id = benchmark_id or config.name or "skill-eval"

    request = JobSubmissionRequest(
        name=f"eval-{config.name or 'run'}-{int(time.time())}",
        description=config.description or f"Eval for {config.skill or 'agent'}",
        model=ModelConfig(name=model),
        benchmarks=[BenchmarkConfig(
            id=bench_id,
            provider_id=provider_id,
            parameters=params,
        )],
    )

    print(f"Submitting to EvalHub at {url} (provider={provider_id}, "
          f"benchmark={bench_id}, model={model})", file=sys.stderr)
    job = client.jobs.submit(request)
    job_id = job.resource.id
    print(f"Job submitted: {job_id}", file=sys.stderr)

    # 3. Wait for completion
    print(f"Waiting for completion (poll every {poll_interval}s)...",
          file=sys.stderr)
    job = client.jobs.wait_for_completion(
        job_id, timeout=timeout, poll_interval=poll_interval)

    status = job.status.state.value if job.status else "unknown"
    print(f"Job {job_id}: {status}", file=sys.stderr)

    if status not in ("completed", "partially_failed"):
        msg = job.status.message.message if job.status and job.status.message else ""
        print(f"ERROR: job {status}: {msg}", file=sys.stderr)
        return 1

    # 4. Map results → summary.yaml
    output_dir.mkdir(parents=True, exist_ok=True)
    if not job.results or not job.results.benchmarks:
        print("ERROR: no benchmark results returned", file=sys.stderr)
        return 1

    bench_result = job.results.benchmarks[0]
    metrics = bench_result.metrics or {}
    summary = _metrics_to_summary(metrics, config)

    run_meta = {
        "exit_code": int(metrics.get("exit_code", 0)),
        "execution_mode": "evalhub",
        "agent": f"evalhub:{provider_id}",
        "model": model,
        "job_id": job_id,
        "cost_usd": metrics.get("cost_usd"),
        "num_cases": int(metrics.get("num_examples_evaluated", 0)),
        "duration_seconds": metrics.get("duration_seconds"),
        "mlflow_run_id": bench_result.mlflow_run_id,
        "mlflow_experiment_url": job.results.mlflow_experiment_url,
    }

    (output_dir / "run_result.json").write_text(
        json.dumps(run_meta, indent=2) + "\n")
    (output_dir / "summary.yaml").write_text(
        yaml.safe_dump({"run_id": output_dir.name, **summary},
                       sort_keys=False, allow_unicode=True))

    # 5. Report
    try:
        raw_cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        report = _load_report_module()
        html = report.generate_report(
            config=raw_cfg, summary=summary, run_result=run_meta,
            run_dir=output_dir, review=None, baseline_dir=None,
            baseline_summary=None, baseline_result=None,
        )
        (output_dir / "report.html").write_text(html)
        print(f"report: {output_dir}/report.html")
    except Exception as exc:
        print(f"WARNING: report generation failed: {exc}", file=sys.stderr)

    # 6. Regression check
    try:
        score = _load_score_module()
        regressions = score.detect_regressions(summary["judges"], config.thresholds)
        if regressions:
            print(f"REGRESSIONS: {len(regressions)} detected", file=sys.stderr)
            for r in regressions:
                print(f"  [{r.judge_name}] {r.metric}: {r.baseline_value} -> "
                      f"{r.current_value}", file=sys.stderr)
            return 1
    except Exception as exc:
        log.warning("Regression check skipped: %s", exc)

    print(f"Mapped → {output_dir}/summary.yaml; REGRESSIONS: 0")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--evalhub-url", default=None)
    p.add_argument("--evalhub-token", default=None)
    p.add_argument("--namespace", default=None)
    p.add_argument("--provider-id", default="agent-eval")
    p.add_argument("--benchmark-id", default=None)
    p.add_argument("--project-dir", default=None)
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--poll-interval", type=float, default=10.0)
    args = p.parse_args()

    code = run_eval_on_evalhub(
        Path(args.config), model=args.model,
        output_dir=Path(args.output),
        evalhub_url=args.evalhub_url, evalhub_token=args.evalhub_token,
        namespace=args.namespace, provider_id=args.provider_id,
        benchmark_id=args.benchmark_id, project_dir=args.project_dir,
        timeout=args.timeout, poll_interval=args.poll_interval,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
