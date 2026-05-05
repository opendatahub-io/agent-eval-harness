#!/usr/bin/env python3
import logging
import os
import sys
import traceback

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("entrypoint")

from evalhub.adapter import JobSpec, DefaultCallbacks
from agent_eval.evalhub.adapter import AgentEvalAdapter


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

    eval_config_path = os.environ.get("EVAL_CONFIG_PATH", "/app/eval-config/eval.yaml")
    log.info("Eval config path: %s (exists=%s)", eval_config_path, os.path.exists(eval_config_path))

    adapter = AgentEvalAdapter(eval_config_path=eval_config_path)
    callbacks = DefaultCallbacks(job_id=spec.id, benchmark_id=spec.benchmark_id)

    log.info("Starting run_benchmark_job...")
    try:
        results = adapter.run_benchmark_job(spec, callbacks)
    except Exception:
        log.error("run_benchmark_job failed:\n%s", traceback.format_exc())
        sys.exit(1)

    if results is None:
        log.error("run_benchmark_job returned None — adapter did not produce results")
        sys.exit(1)

    # Log to MLflow (SDK prescribed pattern) — optional, don't fail the job
    try:
        rid = callbacks.mlflow.save(results, spec)
        if rid:
            results.mlflow_run_id = rid
            log.info("MLflow run: %s", rid)
    except Exception as exc:
        log.warning("MLflow save failed (non-fatal): %s", exc)

    # Report final results to sidecar → EvalHub API
    callbacks.report_results(results)

    log.info("Completed: %d examples, overall_score=%s", results.num_examples_evaluated, results.overall_score)


if __name__ == "__main__":
    main()
