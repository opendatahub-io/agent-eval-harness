#!/usr/bin/env python3
import logging
import os
import sys
import tempfile
import traceback

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("entrypoint")

from evalhub.adapter import JobSpec, DefaultCallbacks
from agent_eval.harbor.adapter import HarborAdapter


def _log_test_artifact(run_id: str, results) -> None:
    """Log test output as an MLflow artifact if mlflow is available."""
    test_output = (results.evaluation_metadata or {}).get("test_output", "")
    if not test_output:
        return
    try:
        import mlflow

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", prefix="test_output_", delete=False) as f:
            f.write(test_output)
            tmp_path = f.name
        try:
            mlflow.log_artifact(tmp_path, run_id=run_id)
            log.info("Logged test_output artifact (%d bytes)", len(test_output))
        finally:
            os.unlink(tmp_path)
    except ImportError:
        log.debug("mlflow not installed, skipping artifact logging")
    except Exception as exc:
        log.warning("Failed to log test artifact (non-fatal): %s", exc)


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/meta/job.json"
    log.info("Loading job spec from %s", config_path)
    try:
        spec = JobSpec.from_file(config_path)
    except FileNotFoundError:
        sys.exit(f"Job spec not found: {config_path}")
    except Exception as e:
        sys.exit(f"Failed to load job spec from {config_path}: {e}")
    log.info("Job: id=%s provider=%s benchmark=%s", spec.id, spec.provider_id, spec.benchmark_id)
    log.info("Model: %s / %s", spec.model.url, spec.model.name)
    log.info("Parameters: keys=%s", list(spec.parameters.keys()) if spec.parameters else "none")

    if not getattr(spec, 'experiment_name', None):
        log.warning(
            "No experiment name in job spec — MLflow results will NOT be saved. "
            "Set experiment: {name: ...} in the job config YAML."
        )

    task_path = os.environ.get("HARBOR_TASK_PATH", "")
    adapter = HarborAdapter(task_path=task_path or None)
    callbacks = DefaultCallbacks(job_id=spec.id, benchmark_id=spec.benchmark_id)

    log.info("Starting run_benchmark_job...")
    try:
        results = adapter.run_benchmark_job(spec, callbacks)
    except Exception:
        log.error("run_benchmark_job failed:\n%s", traceback.format_exc())
        sys.exit(1)

    if results is None:
        log.error("run_benchmark_job returned None")
        sys.exit(1)

    try:
        rid = callbacks.mlflow.save(results, spec)
        if rid:
            results.mlflow_run_id = rid
            log.info("MLflow run: %s", rid)
            _log_test_artifact(rid, results)
    except Exception as exc:
        log.warning("MLflow save failed (non-fatal): %s", exc)

    callbacks.report_results(results)
    log.info("Completed: %d examples, overall_score=%s", results.num_examples_evaluated, results.overall_score)


if __name__ == "__main__":
    main()
