"""EvalHub FrameworkAdapter for Harbor benchmarks.

Runs Harbor benchmark tasks via the harbor CLI, parses trial results,
and maps them to EvalHub JobResults for MLflow persistence.

Supports three modes:
  - Live: runs `harbor run` and parses results (default)
  - Kubernetes: runs tasks as K8s Jobs via k8s_runner
  - Import: parses pre-existing results from a jobs directory
"""

import logging
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_eval.harbor.results_parser import parse_job

try:
    from evalhub.adapter import (
        EvaluationResult,
        FrameworkAdapter,
        JobCallbacks,
        JobResults,
        JobSpec,
        JobStatus,
        JobPhase,
        JobStatusUpdate,
    )
    from evalhub.adapter.models.job import MessageInfo
except ImportError:
    from agent_eval.evalhub.stubs import (  # type: ignore[assignment]
        EvaluationResult,
        FrameworkAdapter,
        JobCallbacks,
        JobResults,
        JobSpec,
        JobStatus,
        JobPhase,
        JobStatusUpdate,
        MessageInfo,
    )

log = logging.getLogger(__name__)


def _framework_adapter_init(adapter_instance):
    FrameworkAdapter.__init__(adapter_instance)


def _build_job_results(
    config: JobSpec,
    job_data: dict,
    agent_name: str,
    model_name: str,
    task_path: str,
    duration_s: float,
) -> JobResults:
    """Map parsed Harbor job data to EvalHub JobResults."""
    trials = job_data["trials"]
    all_metrics = []

    mean_reward = job_data["mean_reward"]
    if mean_reward is None:
        log.warning("mean_reward is None — no trials completed successfully")
        mean_reward = 0.0
    all_metrics.append(EvaluationResult(
        metric_name="mean_reward",
        metric_value=mean_reward,
        metric_type="benchmark",
    ))

    all_metrics.append(EvaluationResult(
        metric_name="num_trials",
        metric_value=len(trials),
        metric_type="count",
    ))

    all_metrics.append(EvaluationResult(
        metric_name="num_errored",
        metric_value=job_data["n_errored"],
        metric_type="count",
    ))

    for trial in trials:
        prefix = trial["task_name"].replace("/", "_")
        for metric in trial["metrics"]:
            all_metrics.append(EvaluationResult(
                metric_name=f"{prefix}/{metric.metric_name}",
                metric_value=metric.metric_value,
                metric_type=metric.metric_type,
            ))

    evaluation_metadata = {
        "harbor_job_id": job_data["job_id"],
        "agent": agent_name,
        "task_path": task_path,
        "n_completed": job_data["n_completed"],
        "n_errored": job_data["n_errored"],
    }
    if model_name:
        evaluation_metadata["model"] = model_name

    total_cost = sum(
        m.metric_value for t in trials for m in t["metrics"]
        if m.metric_name == "cost_usd" and m.metric_value is not None
    )
    if total_cost:
        all_metrics.append(EvaluationResult(
            metric_name="total_cost_usd",
            metric_value=total_cost,
            metric_type="cost",
        ))

    return JobResults(
        id=config.id,
        benchmark_id=config.benchmark_id,
        benchmark_index=config.benchmark_index,
        model_name=model_name or agent_name,
        results=all_metrics,
        overall_score=job_data["mean_reward"],
        num_examples_evaluated=len(trials),
        duration_seconds=duration_s,
        completed_at=datetime.now(timezone.utc),
        evaluation_metadata=evaluation_metadata,
    )


class HarborAdapter(FrameworkAdapter):
    """EvalHub adapter that runs Harbor benchmark tasks.

    Orchestrates: harbor run -> parse results -> map to JobResults.

    If jobs_dir is provided, skips harbor run and parses existing results.
    """

    def __init__(
        self,
        task_path: str | None = None,
        jobs_dir: str | None = None,
        execution_mode: str | None = None,
    ):
        _framework_adapter_init(self)
        self._task_path = task_path
        self._jobs_dir = jobs_dir
        self._execution_mode = execution_mode

    def run_benchmark_job(
        self, config: JobSpec, callbacks: JobCallbacks
    ) -> JobResults:
        start_time = time.monotonic()
        params = config.parameters or {}

        task_path = self._task_path or params.get("task_path", "")
        agent_name = params.get("agent", "oracle")
        model_name = params.get("model") or (config.model.name if config.model else "") or ""
        jobs_dir = self._jobs_dir or params.get("jobs_dir")
        execution_mode = self._execution_mode or params.get("execution_mode", "harbor")

        if jobs_dir:
            return self._import_results(
                config, callbacks, Path(jobs_dir),
                agent_name, model_name, task_path, start_time,
            )

        if execution_mode == "kubernetes":
            return self._run_k8s(
                config, callbacks,
                task_path, agent_name, model_name, params, start_time,
            )

        return self._run_harbor(
            config, callbacks,
            task_path, agent_name, model_name, params, start_time,
        )

    def _run_harbor(
        self,
        config: JobSpec,
        callbacks: JobCallbacks,
        task_path: str,
        agent_name: str,
        model_name: str,
        params: dict,
        start_time: float,
    ) -> JobResults:
        """Run harbor CLI and parse fresh results."""
        if not task_path:
            raise ValueError("task_path is required (via constructor or job parameters)")

        n_concurrent = int(params.get("n_concurrent", 1))
        timeout_multiplier = float(params.get("timeout_multiplier", 1.0))

        self._report_status(
            callbacks,
            status=JobStatus.RUNNING,
            phase=JobPhase.INITIALIZING,
            message=f"Preparing harbor run: {task_path} agent={agent_name}",
        )

        jobs_dir = tempfile.mkdtemp(prefix="harbor-evalhub-")
        job_name = f"evalhub-{config.id[:8]}" if config.id else "evalhub-run"

        self._report_status(
            callbacks,
            status=JobStatus.RUNNING,
            phase=JobPhase.RUNNING_EVALUATION,
            message=f"Running harbor: {task_path}",
        )

        cmd = [
            "harbor", "run",
            "-p", task_path,
            "-a", agent_name,
            "--jobs-dir", jobs_dir,
            "--job-name", job_name,
            "--n-concurrent", str(n_concurrent),
            "--timeout-multiplier", str(timeout_multiplier),
        ]
        if model_name:
            cmd.extend(["-m", model_name])

        log.info("Running: %s", " ".join(cmd))

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200,
        )

        if result.returncode != 0:
            log.error("harbor run failed (exit %d): %s", result.returncode, result.stderr)
            self._report_status(
                callbacks,
                status=JobStatus.FAILED,
                phase=JobPhase.RUNNING_EVALUATION,
                message=f"harbor run failed: {result.stderr[:200]}",
            )
            return JobResults(
                id=config.id,
                benchmark_id=config.benchmark_id,
                model_name=model_name or agent_name,
                results=[EvaluationResult(
                    metric_name="harbor_exit_code",
                    metric_value=result.returncode,
                    metric_type="status",
                )],
                overall_score=0.0,
                num_examples_evaluated=0,
                duration_seconds=time.monotonic() - start_time,
                completed_at=datetime.now(timezone.utc),
                evaluation_metadata={"stderr": result.stderr[:1000]},
            )

        job_dir = Path(jobs_dir) / job_name
        return self._parse_and_map(
            config, callbacks, job_dir,
            agent_name, model_name, task_path, start_time,
        )

    def _run_k8s(
        self,
        config: JobSpec,
        callbacks: JobCallbacks,
        task_path: str,
        agent_name: str,
        model_name: str,
        params: dict,
        start_time: float,
    ) -> JobResults:
        """Run Harbor task as a Kubernetes Job."""
        from agent_eval.harbor.k8s_runner import run_task_job

        task_image = params.get("task_image", "")
        namespace = params.get("namespace", "evalhub")
        timeout = int(params.get("timeout_sec", 600))
        cpu = params.get("cpu", "2")
        memory = params.get("memory", "4Gi")
        run_as_user = int(params.get("run_as_user", 1001))
        env_from_secrets = params.get("env_from_secrets", [])
        if isinstance(env_from_secrets, str):
            env_from_secrets = [env_from_secrets]
        env_from_configmaps = params.get("env_from_configmaps", [])
        if isinstance(env_from_configmaps, str):
            env_from_configmaps = [env_from_configmaps]
        secret_volumes = params.get("secret_volumes", [])

        if not task_image:
            raise ValueError("task_image is required for kubernetes execution mode")

        task_name = task_path.replace("/", "-").replace("tasks-", "")

        self._report_status(
            callbacks,
            status=JobStatus.RUNNING,
            phase=JobPhase.RUNNING_EVALUATION,
            message=f"Running K8s Job: {task_name} (agent={agent_name})",
        )

        result = run_task_job(
            task_name=task_name,
            task_image=task_image,
            namespace=namespace,
            timeout_sec=timeout,
            cpu=cpu,
            memory=memory,
            run_as_user=run_as_user,
            env_from_secrets=env_from_secrets,
            env_from_configmaps=env_from_configmaps,
            agent=agent_name,
            model=model_name,
            secret_volumes=secret_volumes,
        )

        all_metrics = [
            EvaluationResult(
                metric_name="reward",
                metric_value=result["reward"],
                metric_type="benchmark",
            ),
            EvaluationResult(
                metric_name="mean_reward",
                metric_value=result["reward"],
                metric_type="benchmark",
            ),
            EvaluationResult(
                metric_name="duration_seconds",
                metric_value=result["duration_s"],
                metric_type="performance",
            ),
            EvaluationResult(
                metric_name="num_trials",
                metric_value=1,
                metric_type="count",
            ),
        ]

        job_results = JobResults(
            id=config.id,
            benchmark_id=config.benchmark_id,
            benchmark_index=config.benchmark_index,
            model_name=model_name or agent_name,
            results=all_metrics,
            overall_score=result["reward"],
            num_examples_evaluated=1,
            duration_seconds=time.monotonic() - start_time,
            completed_at=datetime.now(timezone.utc),
            evaluation_metadata={
                "agent": agent_name,
                "task_path": task_path,
                "task_image": task_image,
                "execution_mode": "kubernetes",
                "exit_code": result["exit_code"],
                "test_output": result["stdout"][-50000:] if result.get("stdout") else "",
            },
        )

        status = JobStatus.COMPLETED if result["exit_code"] == 0 else JobStatus.FAILED
        self._report_status(
            callbacks,
            status=status,
            phase=JobPhase.COMPLETED,
            message=f"K8s Job complete: reward={result['reward']}",
            progress=1.0,
        )

        return job_results

    def _import_results(
        self,
        config: JobSpec,
        callbacks: JobCallbacks,
        jobs_dir: Path,
        agent_name: str,
        model_name: str,
        task_path: str,
        start_time: float,
    ) -> JobResults:
        """Parse pre-existing Harbor results without running harbor."""
        self._report_status(
            callbacks,
            status=JobStatus.RUNNING,
            phase=JobPhase.LOADING_DATA,
            message=f"Importing results from {jobs_dir}",
        )

        if (jobs_dir / "result.json").exists():
            job_dir = jobs_dir
        else:
            subdirs = [
                d for d in sorted(jobs_dir.iterdir())
                if d.is_dir() and (d / "result.json").exists()
            ]
            if not subdirs:
                raise FileNotFoundError(f"No Harbor result.json found in {jobs_dir}")
            job_dir = subdirs[0]

        return self._parse_and_map(
            config, callbacks, job_dir,
            agent_name, model_name, task_path, start_time,
        )

    def _parse_and_map(
        self,
        config: JobSpec,
        callbacks: JobCallbacks,
        job_dir: Path,
        agent_name: str,
        model_name: str,
        task_path: str,
        start_time: float,
    ) -> JobResults:
        """Parse a Harbor job directory and map to JobResults."""
        self._report_status(
            callbacks,
            status=JobStatus.RUNNING,
            phase=JobPhase.POST_PROCESSING,
            message="Parsing harbor results",
        )

        job_data = parse_job(job_dir)

        job_results = _build_job_results(
            config, job_data,
            agent_name, model_name, task_path,
            duration_s=time.monotonic() - start_time,
        )

        self._report_status(
            callbacks,
            status=JobStatus.COMPLETED,
            phase=JobPhase.COMPLETED,
            message=f"Harbor benchmark complete: {len(job_data['trials'])} trials, mean_reward={job_data['mean_reward']}",
            progress=1.0,
        )

        return job_results

    @staticmethod
    def _report_status(
        callbacks: JobCallbacks,
        status: str,
        phase: str,
        message: str,
        progress: float | None = None,
        total_steps: int | None = None,
        completed_steps: int | None = None,
    ) -> None:
        try:
            update = JobStatusUpdate(
                status=status,
                phase=phase,
                progress=progress,
                message=MessageInfo(message=message, message_code="info"),
                total_steps=total_steps,
                completed_steps=completed_steps,
                timestamp=datetime.now(timezone.utc),
            )
            callbacks.report_status(update)
        except Exception as exc:
            log.warning("Failed to report status: %s", exc)
