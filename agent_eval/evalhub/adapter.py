"""EvalHub FrameworkAdapter for agent-eval-harness.

Orchestrates the full evaluation loop inside the EvalHub Job pod:
download dataset → run agent per case → score with judges → map to JobResults.

The adapter runs IN-PROCESS (matching EvalHub's architecture where adapter pods
are execution-only). It uses the runner registry (ClaudeCodeRunner, CliRunner,
ResponsesAPIRunner) based on ``runner.type`` in eval.yaml — no Harbor, no
sub-pods.

Resources (eval.yaml, dataset, project) can come from:
- The container filesystem (baked into the image or mounted)
- S3 (EvalHub's standard dataset delivery)
- Kubernetes ConfigMaps (passed as job parameters — no image rebuild needed;
  created programmatically via ``agent_eval.harbor.k8s_resources``)

Uses conditional imports so the module works without eval-hub-sdk
installed (stubs are provided for testing/CI).
"""

import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

try:
    import boto3
except ImportError:
    boto3 = None  # type: ignore[assignment]

from agent_eval.agent import RUNNERS
from agent_eval.agent.base import RunResult
from agent_eval.config import EvalConfig, resolve_arguments
from agent_eval.evalhub.results_mapper import map_to_job_results
from agent_eval.evalhub.s3_dataset import DatasetInfo, download_dataset

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
    from evalhub.adapter.models.job import MessageInfo, ModelConfig

    EVALHUB_AVAILABLE = True
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
        ModelConfig,
    )

    EVALHUB_AVAILABLE = False


_score_module = None
_score_module_loaded = False


def _get_score_module():
    """Load scoring module from eval-run scripts once, cache the result."""
    global _score_module, _score_module_loaded
    if _score_module_loaded:
        return _score_module
    _score_module_loaded = True
    try:
        import importlib.util
        score_path = Path(__file__).parent.parent.parent / "skills" / "eval-run" / "scripts" / "score.py"
        spec = importlib.util.spec_from_file_location("score", score_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "load_judges") and hasattr(mod, "score_cases"):
            _score_module = mod
    except Exception as exc:
        log.warning("Judge scoring unavailable: %s", exc)
    return _score_module


def _load_judges_and_score(eval_config, case_dirs):
    """Try to score using eval-run scripts. Returns aggregated dict on success."""
    mod = _get_score_module()
    if mod is None:
        return {}
    try:
        judges = mod.load_judges(eval_config)
        result = mod.score_cases(judges, case_dirs, eval_config)
        return result.get("aggregated", {})
    except Exception as exc:
        log.warning("Judge scoring failed: %s", exc)
        return {}


def _framework_adapter_init(adapter_instance):
    """Call FrameworkAdapter.__init__. Extracted for testability."""
    FrameworkAdapter.__init__(adapter_instance)


def _read_configmap(name: str, namespace: str) -> dict[str, str]:
    """Read a ConfigMap's data via the Kubernetes API.

    Works in-cluster (ServiceAccount token) and locally (kubeconfig).
    Returns the ConfigMap's ``data`` dict, or raises on error.
    """
    from kubernetes import client as k8s_client, config as k8s_config
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    core = k8s_client.CoreV1Api()
    cm = core.read_namespaced_config_map(name, namespace)
    return cm.data or {}


def _configmap_to_dir(cm_data: dict[str, str], dest: Path) -> None:
    """Write ConfigMap data to a directory, restoring ``--`` path separators.

    ConfigMap keys use ``--`` instead of ``/`` (created by
    ``k8s_resources._collect_files``). This reverses that encoding so the
    directory structure matches the original project layout.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for key, content in cm_data.items():
        rel_path = key.replace("--", "/")
        file_path = dest / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)


def _get_namespace() -> str:
    """Get the current K8s namespace (in-cluster or from kubeconfig)."""
    ns_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if ns_path.is_file():
        return ns_path.read_text().strip() or "default"
    ns = os.environ.get("AGENT_EVAL_K8S_NAMESPACE")
    if ns:
        return ns
    return "default"


class AgentEvalAdapter(FrameworkAdapter):
    """EvalHub adapter that runs agent evaluations in-process.

    Dispatches to the runner specified by ``runner.type`` in eval.yaml
    (claude-code, cli, responses-api). Runs all cases in the Job pod.

    Supports three resource delivery modes (checked in order):
    1. **ConfigMap** — job parameters ``eval_configmap``, ``dataset_configmap``,
       ``project_configmap`` name ConfigMaps to read via the K8s API. No image
       rebuild needed; created programmatically via ``k8s_resources``.
    2. **Filesystem** — eval.yaml + cases baked into the image or volume-mounted.
    3. **S3** — ``s3_bucket`` + ``s3_prefix`` parameters (EvalHub's standard).
    """

    def __init__(self, eval_config_path: str = "eval.yaml"):
        _framework_adapter_init(self)
        self._eval_config_path = eval_config_path

    def run_benchmark_job(self, config: JobSpec, callbacks: JobCallbacks) -> JobResults:
        start_time = time.monotonic()
        params = config.parameters or {}
        log.info("run_benchmark_job starting: params=%s", list(params.keys()))

        # Temp dir for materializing ConfigMap content
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmp_dir.name)
        namespace = params.get("namespace") or _get_namespace()

        # 1. Load eval.yaml (ConfigMap > filesystem)
        self._report_status(callbacks, JobStatus.RUNNING, JobPhase.INITIALIZING,
                            "Loading evaluation configuration")
        eval_config_path = self._resolve_eval_config(params, tmp_root, namespace)
        eval_config = EvalConfig.from_yaml(eval_config_path)
        log.info("Eval config loaded: skill=%s, dataset=%s, %d judges",
                 eval_config.skill or "(prompt mode)", eval_config.dataset.path,
                 len(eval_config.judges))

        # Mount project resources from ConfigMap if specified
        if params.get("project_configmap"):
            self._materialize_project(params["project_configmap"], namespace, tmp_root)

        # 2. Load dataset (ConfigMap > filesystem > S3)
        dataset_info = self._load_dataset(config, callbacks, eval_config,
                                          tmp_root, namespace)
        model_name = config.model.name

        # 3. Build runner from eval.yaml runner.type
        runner_type = eval_config.runner.type
        if runner_type not in RUNNERS:
            raise ValueError(
                f"Unknown runner type '{runner_type}' in eval.yaml. "
                f"Available: {list(RUNNERS.keys())}")
        runner_cls = RUNNERS[runner_type]
        runner = runner_cls.from_config(eval_config, log_prefix="evalhub")
        log.info("Runner: %s (%s)", runner.name, runner_type)

        # 4. Execute per case
        self._report_status(callbacks, JobStatus.RUNNING, JobPhase.RUNNING_EVALUATION,
                            f"Running {dataset_info.num_cases} test cases",
                            total_steps=dataset_info.num_cases, completed_steps=0)

        case_results = []
        for i, case_id in enumerate(dataset_info.case_ids):
            case_dir = dataset_info.dest / case_id
            input_path = case_dir / "input.yaml"
            input_data = {}
            if input_path.exists():
                with open(input_path, encoding="utf-8") as f:
                    input_data = yaml.safe_load(f) or {}

            args = resolve_arguments(eval_config.execution.arguments, input_data) \
                if eval_config.execution.arguments else ""
            timeout = eval_config.execution.timeout or 600
            budget = eval_config.execution.max_budget_usd or 5.0

            result = runner.run_skill(
                skill_name=eval_config.skill or "",
                args=args,
                workspace=case_dir,
                model=model_name,
                max_budget_usd=budget,
                timeout_s=timeout,
            )

            cost_str = f"{result.cost_usd:.4f}" if result.cost_usd is not None else "n/a"
            log.info("Case %s: exit=%s cost=%s %.1fs",
                     case_id, result.exit_code, cost_str, result.duration_s)
            case_results.append({"case_id": case_id, "run_result": result})

            self._report_status(callbacks, JobStatus.RUNNING, JobPhase.RUNNING_EVALUATION,
                                f"Completed case {case_id}",
                                total_steps=dataset_info.num_cases,
                                completed_steps=i + 1,
                                progress=(i + 1) / dataset_info.num_cases)

        # 5. Score with judges
        self._report_status(callbacks, JobStatus.RUNNING, JobPhase.POST_PROCESSING,
                            "Scoring results with judges")
        case_dirs = [dataset_info.dest / cr["case_id"] for cr in case_results]
        judge_scores = _load_judges_and_score(eval_config, case_dirs)

        # 6. Aggregate + map to JobResults
        aggregate = self._aggregate(case_results, start_time)
        log.info("Mapping results: %d cases, exit_code=%d",
                 len(case_results), aggregate.exit_code)
        job_results = map_to_job_results(
            job_id=config.id,
            benchmark_id=config.benchmark_id,
            model_name=model_name,
            run_result=aggregate,
            judge_scores=judge_scores,
            num_cases=dataset_info.num_cases,
            benchmark_index=config.benchmark_index,
        )

        self._report_status(callbacks, JobStatus.COMPLETED, JobPhase.COMPLETED,
                            "Evaluation complete", progress=1.0)
        return job_results

    # --- resource resolution -------------------------------------------------

    def _resolve_eval_config(self, params: dict, tmp_root: Path,
                             namespace: str) -> Path:
        """Resolve eval.yaml: ConfigMap parameter > filesystem path."""
        cm_name = params.get("eval_configmap")
        if cm_name:
            log.info("Reading eval config from ConfigMap %s/%s", namespace, cm_name)
            cm_data = _read_configmap(cm_name, namespace)
            config_dir = tmp_root / "eval-config"
            _configmap_to_dir(cm_data, config_dir)
            return config_dir / "eval.yaml"
        return Path(self._eval_config_path)

    def _materialize_project(self, cm_name: str, namespace: str,
                             tmp_root: Path) -> None:
        """Read project resources from a ConfigMap into a temp directory."""
        log.info("Reading project from ConfigMap %s/%s", namespace, cm_name)
        cm_data = _read_configmap(cm_name, namespace)
        project_dir = tmp_root / "project"
        _configmap_to_dir(cm_data, project_dir)
        os.environ["AGENT_EVAL_PROJECT_DIR"] = str(project_dir)

    def _load_dataset(self, config: JobSpec, callbacks: JobCallbacks,
                      eval_config: EvalConfig, tmp_root: Path,
                      namespace: str) -> DatasetInfo:
        """Load dataset: ConfigMap parameter > local path > S3."""
        params = config.parameters or {}

        # ConfigMap dataset
        cm_name = params.get("dataset_configmap")
        if cm_name:
            self._report_status(callbacks, JobStatus.RUNNING, JobPhase.LOADING_DATA,
                                f"Reading dataset from ConfigMap {cm_name}")
            cm_data = _read_configmap(cm_name, namespace)
            dest = tmp_root / "cases"
            _configmap_to_dir(cm_data, dest)
            case_ids = sorted(d.name for d in dest.iterdir() if d.is_dir())
            log.info("Dataset from ConfigMap: %d cases", len(case_ids))
            return DatasetInfo(num_cases=len(case_ids), case_ids=case_ids, dest=dest)

        # Local filesystem
        eval_config_dir = Path(self._eval_config_path).parent
        local_dataset = eval_config_dir / eval_config.dataset.path
        if local_dataset.is_dir() and any(local_dataset.iterdir()):
            self._report_status(callbacks, JobStatus.RUNNING, JobPhase.LOADING_DATA,
                                f"Using local dataset at {local_dataset}")
            dest = tmp_root / "cases"
            shutil.copytree(local_dataset, dest)
            case_ids = sorted(d.name for d in dest.iterdir() if d.is_dir())
            log.info("Copied local dataset → %s (%d cases)", dest, len(case_ids))
            return DatasetInfo(num_cases=len(case_ids), case_ids=case_ids, dest=dest)

        # S3
        self._report_status(callbacks, JobStatus.RUNNING, JobPhase.LOADING_DATA,
                            "Downloading test cases from S3")
        if not boto3:
            raise RuntimeError(
                "boto3 is required for S3 dataset download. "
                "Install with: pip install agent-eval-harness[evalhub]")
        dest = tmp_root / "cases"
        dest.mkdir(parents=True, exist_ok=True)
        return download_dataset(
            boto3.client("s3"), params.get("s3_bucket", ""),
            params.get("s3_prefix", ""), dest)

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _aggregate(case_results: list, start_time: float) -> RunResult:
        if not case_results:
            return RunResult(exit_code=-1, stdout="", stderr="No cases executed",
                             duration_s=time.monotonic() - start_time)
        runs = [cr["run_result"] for cr in case_results]
        failed = sum(1 for r in runs if r.exit_code != 0)
        return RunResult(
            exit_code=max((r.exit_code for r in runs), key=abs),
            stdout="",
            stderr=f"{failed}/{len(runs)} cases failed" if failed else "",
            duration_s=time.monotonic() - start_time,
            cost_usd=sum(r.cost_usd or 0 for r in runs) or None,
            num_turns=sum(r.num_turns or 0 for r in runs) or None,
            resolved_model=runs[0].resolved_model,
        )

    @staticmethod
    def _report_status(
        callbacks: JobCallbacks, status: str, phase: str, message: str,
        progress: float | None = None, total_steps: int | None = None,
        completed_steps: int | None = None,
    ) -> None:
        try:
            callbacks.report_status(JobStatusUpdate(
                status=status, phase=phase, progress=progress,
                message=MessageInfo(message=message, message_code="info"),
                total_steps=total_steps, completed_steps=completed_steps,
                timestamp=datetime.now(timezone.utc),
            ))
        except Exception as exc:
            log.warning("Failed to report status: %s", exc)
