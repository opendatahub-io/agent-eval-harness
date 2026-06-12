"""EvalHub FrameworkAdapter for agent-eval-harness.

Orchestrates the full evaluation loop: download dataset, execute agent
against each case, score with judges, and map results to JobResults.

Uses conditional imports so the module works without eval-hub-sdk
installed (stubs are provided for testing/CI).
"""

import logging
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

try:
    import boto3
except ImportError:
    boto3 = None  # type: ignore[assignment]

from agent_eval.agent.base import RunResult
from agent_eval.agent.claude_code import ClaudeCodeRunner
from agent_eval.config import EvalConfig
from agent_eval.evalhub.results_mapper import map_to_job_results
from agent_eval.evalhub.s3_dataset import DatasetInfo, download_dataset

# Conditional imports for evalhub SDK types
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


def _resolve_arguments(template: str, input_data: dict) -> str:
    """Resolve {field} and {field?} placeholders from input.yaml data.

    {field} — required, raises KeyError if missing
    {field?} — optional, silently omitted if missing
    """
    def _replacer(match):
        field = match.group(1)
        optional = field.endswith("?")
        if optional:
            field = field[:-1]
        value = input_data.get(field)
        if value is None:
            if optional:
                return ""
            raise KeyError(f"Required field '{field}' not found in input.yaml")
        return str(value)

    result = re.sub(r"\{([^}]+)\}", _replacer, template)
    # Clean up runs of spaces from omitted optional fields, preserve newlines
    return re.sub(r"[ \t]+", " ", result).strip()


def _create_anthropic_client():
    """Create Anthropic client based on environment (Vertex AI or direct)."""
    import os
    use_vertex = os.environ.get("CLAUDE_CODE_USE_VERTEX", "").strip() == "1"
    if use_vertex:
        from anthropic import AnthropicVertex
        project_id = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
        region = os.environ.get("CLOUD_ML_REGION", "us-east5")
        return AnthropicVertex(project_id=project_id, region=region)
    from anthropic import Anthropic
    return Anthropic()


def _call_llm(client, rubric: str, rfe_content: str, model_name: str) -> str:
    """Call Anthropic API to score an RFE against the rubric."""
    prompt = f"{rubric}\n\n---\n\nScore the following RFE:\n\n{rfe_content}"
    response = client.messages.create(
        model=model_name,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    if not response.content:
        raise ValueError(f"Empty response from model {model_name}")
    return response.content[0].text


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
    """Call FrameworkAdapter.__init__. Extracted for testability.

    The real FrameworkAdapter.__init__ loads meta/job.json from disk,
    which isn't available in unit tests. Tests patch this function.
    """
    FrameworkAdapter.__init__(adapter_instance)


class AgentEvalAdapter(FrameworkAdapter):
    """EvalHub adapter that runs agent evaluations.

    Orchestrates: dataset download -> agent execution -> scoring -> results mapping.
    """

    def __init__(self, eval_config_path: str = "eval.yaml"):
        _framework_adapter_init(self)
        self._eval_config_path = eval_config_path

    def run_benchmark_job(self, config: JobSpec, callbacks: JobCallbacks) -> JobResults:
        """Run a full evaluation job.

        Args:
            config: Job specification from EvalHub (model, parameters, benchmark info)
            callbacks: Callback interface for status reporting

        Returns:
            JobResults with metrics and scores
        """
        start_time = time.monotonic()
        log.info("run_benchmark_job starting: eval_config=%s", self._eval_config_path)

        # 1. Load eval.yaml
        self._report_status(
            callbacks,
            status=JobStatus.RUNNING,
            phase=JobPhase.INITIALIZING,
            message="Loading evaluation configuration",
        )
        log.info("Loading eval config from %s", self._eval_config_path)
        eval_config = EvalConfig.from_yaml(self._eval_config_path)
        log.info("Eval config loaded: skill=%s, dataset_path=%s, %d judges",
                 eval_config.skill, eval_config.dataset.path, len(eval_config.judges))

        # 2. Load dataset — use local path if it exists, otherwise download from S3
        #    Always copy to a writable temp dir (baked-in container paths are read-only)
        params = config.parameters or {}
        eval_config_dir = Path(self._eval_config_path).parent
        local_dataset = eval_config_dir / eval_config.dataset.path
        _tmp_dir = tempfile.TemporaryDirectory()
        tmp_root = Path(_tmp_dir.name)
        if local_dataset.is_dir() and any(local_dataset.iterdir()):
            self._report_status(
                callbacks,
                status=JobStatus.RUNNING,
                phase=JobPhase.LOADING_DATA,
                message=f"Using local dataset at {local_dataset}",
            )
            dest = tmp_root / "cases"
            shutil.copytree(local_dataset, dest)
            case_ids = sorted(
                d.name for d in dest.iterdir() if d.is_dir()
            )
            log.info("Copied local dataset %s → %s (%d cases: %s)", local_dataset, dest, len(case_ids), case_ids)
            dataset_info = DatasetInfo(
                num_cases=len(case_ids), case_ids=case_ids, dest=dest
            )
        else:
            self._report_status(
                callbacks,
                status=JobStatus.RUNNING,
                phase=JobPhase.LOADING_DATA,
                message="Downloading test cases from S3",
            )
            s3_bucket = params.get("s3_bucket", "")
            s3_prefix = params.get("s3_prefix", "")
            dest = tmp_root / "cases"
            dest.mkdir(parents=True, exist_ok=True)
            if not boto3:
                raise RuntimeError(
                    "boto3 is required for S3 dataset download. "
                    "Install with: pip install agent-eval-harness[evalhub]"
                )
            s3_client = boto3.client("s3")
            dataset_info = download_dataset(s3_client, s3_bucket, s3_prefix, dest)

        model_name = config.model.name

        # 3-4. Run evaluation — skill mode or direct LLM mode
        self._report_status(
            callbacks,
            status=JobStatus.RUNNING,
            phase=JobPhase.RUNNING_EVALUATION,
            message=f"Running {dataset_info.num_cases} test cases",
            total_steps=dataset_info.num_cases,
            completed_steps=0,
        )

        case_results = []

        if eval_config.skill:
            # Skill mode: use ClaudeCodeRunner
            log.info("Creating ClaudeCodeRunner: model=%s effort=%s", model_name, eval_config.runner.effort)
            runner = ClaudeCodeRunner(
                permissions=eval_config.permissions,
                system_prompt=eval_config.runner.system_prompt,
                plugin_dirs=eval_config.runner.plugin_dirs,
                env=eval_config.runner.env,
                effort=eval_config.runner.effort,
            )

            for i, case_id in enumerate(dataset_info.case_ids):
                case_dir = dataset_info.dest / case_id
                input_path = case_dir / "input.yaml"
                input_data = {}
                if input_path.exists():
                    with open(input_path, encoding="utf-8") as f:
                        input_data = yaml.safe_load(f) or {}

                args = _resolve_arguments(eval_config.execution.arguments, input_data)
                timeout = eval_config.execution.timeout or 600
                budget = eval_config.execution.max_budget_usd or 5.0

                result = runner.execute(
                    target=eval_config.skill,
                    args=args,
                    workspace=case_dir,
                    model=model_name,
                    max_budget_usd=budget,
                    timeout_s=timeout,
                )

                cost_str = f"{result.cost_usd:.4f}" if result.cost_usd is not None else "n/a"
                log.info("Case %s: exit_code=%s cost=%s duration=%.1fs",
                         case_id, result.exit_code, cost_str, result.duration_s)
                case_results.append({
                    "case_id": case_id,
                    "run_result": result,
                    })

                self._report_status(
                    callbacks,
                    status=JobStatus.RUNNING,
                    phase=JobPhase.RUNNING_EVALUATION,
                    message=f"Completed case {case_id}",
                    total_steps=dataset_info.num_cases,
                    completed_steps=i + 1,
                    progress=(i + 1) / dataset_info.num_cases,
                )
        else:
            # Direct LLM mode: call Anthropic API with rubric + input
            log.info("Direct LLM mode (no skill): model=%s", model_name)
            client = _create_anthropic_client()
            rubric_path = eval_config_dir / "rubric.md"
            rubric = rubric_path.read_text(encoding="utf-8") if rubric_path.exists() else ""
            if not rubric:
                log.warning("No rubric.md found at %s", rubric_path)

            for i, case_id in enumerate(dataset_info.case_ids):
                case_dir = dataset_info.dest / case_id
                input_path = case_dir / "input.yaml"
                input_data = {}
                if input_path.exists():
                    with open(input_path, encoding="utf-8") as f:
                        input_data = yaml.safe_load(f) or {}

                rfe_content = f"# {input_data.get('rfe_id', case_id)}: {input_data.get('title', '')}\n\n{input_data.get('description', '')}"
                log.info("Case %s: scoring RFE %s", case_id, input_data.get("rfe_id", case_id))

                case_start = time.monotonic()
                try:
                    assessment = _call_llm(client, rubric, rfe_content, model_name)
                    results_dir = case_dir / "results"
                    results_dir.mkdir(parents=True, exist_ok=True)
                    result_path = results_dir / "result.md"
                    result_path.write_text(assessment, encoding="utf-8")
                    log.info("Case %s: wrote result.md (%d chars)", case_id, len(assessment))
                    result = RunResult(
                        exit_code=0,
                        stdout=assessment,
                        stderr="",
                        duration_s=time.monotonic() - case_start,
                    )
                except Exception as exc:
                    log.error("Case %s: LLM call failed: %s", case_id, exc)
                    result = RunResult(
                        exit_code=1,
                        stdout="",
                        stderr=str(exc),
                        duration_s=time.monotonic() - case_start,
                    )

                case_results.append({
                    "case_id": case_id,
                    "run_result": result,
                    })

                self._report_status(
                    callbacks,
                    status=JobStatus.RUNNING,
                    phase=JobPhase.RUNNING_EVALUATION,
                    message=f"Completed case {case_id}",
                    total_steps=dataset_info.num_cases,
                    completed_steps=i + 1,
                    progress=(i + 1) / dataset_info.num_cases,
                )

        # 5. Score with judges
        self._report_status(
            callbacks,
            status=JobStatus.RUNNING,
            phase=JobPhase.POST_PROCESSING,
            message="Scoring results with judges",
        )
        case_dirs = [dest / cr["case_id"] for cr in case_results]
        judge_scores = _load_judges_and_score(eval_config, case_dirs)

        # Aggregate across all cases
        if not case_results:
            aggregate = RunResult(
                exit_code=-1, stdout="", stderr="No cases executed",
                duration_s=time.monotonic() - start_time,
            )
        else:
            runs = [cr["run_result"] for cr in case_results]
            failed_count = sum(1 for r in runs if r.exit_code != 0)
            aggregate = RunResult(
                exit_code=max((r.exit_code for r in runs), key=abs),
                stdout="",
                stderr=f"{failed_count}/{len(runs)} cases failed" if failed_count else "",
                duration_s=time.monotonic() - start_time,
                cost_usd=sum(r.cost_usd or 0 for r in runs) or None,
                num_turns=sum(r.num_turns or 0 for r in runs) or None,
                resolved_model=runs[0].resolved_model,
            )

        # 6. Map to JobResults
        log.info("Mapping results: %d cases, aggregate exit_code=%d", len(case_results), aggregate.exit_code)
        job_results = map_to_job_results(
            job_id=config.id,
            benchmark_id=config.benchmark_id,
            model_name=model_name,
            run_result=aggregate,
            judge_scores=judge_scores,
            num_cases=dataset_info.num_cases,
            benchmark_index=config.benchmark_index,
        )

        # 7. Report completed
        self._report_status(
            callbacks,
            status=JobStatus.COMPLETED,
            phase=JobPhase.COMPLETED,
            message="Evaluation complete",
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
        """Report status update via callbacks."""
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
