"""Run Harbor benchmark tasks as Kubernetes Jobs.

Creates a K8s Job from a pre-built task image (which includes solution
and test files baked in), runs the oracle and verifier, collects the
reward from pod logs.
"""

import logging
import shlex
import time
from typing import Any

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    _K8S_AVAILABLE = True
except ImportError:
    _K8S_AVAILABLE = False
    client = None  # type: ignore[assignment]
    config = None  # type: ignore[assignment]
    ApiException = Exception  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)


def _require_k8s():
    if not _K8S_AVAILABLE:
        raise RuntimeError(
            "kubernetes package is required for K8s execution. "
            "Install with: pip install 'agent-eval-harness[evalhub]'"
        )


def _load_k8s_config():
    _require_k8s()
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _oracle_script() -> str:
    return """#!/bin/bash
set -o pipefail
cd /app
mkdir -p /logs/verifier
bash /solution/solve.sh
bash /tests/test.sh
echo "HARBOR_REWARD=$(cat /logs/verifier/reward.txt 2>/dev/null || echo 0)"
"""


def _agent_script(model: str = "") -> str:
    model_flag = f"--model {shlex.quote(model)}" if model else ""
    return f"""#!/bin/bash
set -o pipefail
cd /app
mkdir -p /logs/verifier

# Set up MLflow tracing hook if mlflow is available
if python3 -c "import mlflow" 2>/dev/null; then
  mkdir -p $HOME/.claude
  cat > $HOME/.claude/settings.json << 'SETTINGS'
{{"hooks":{{"Stop":[{{"hooks":[{{"type":"command","command":"python3 -c \\"from mlflow.claude_code.hooks import stop_hook_handler; stop_hook_handler()\\"" }}]}}]}}}}
SETTINGS
fi

claude -p --dangerously-skip-permissions {model_flag} \
  "Read /app/instruction.md and implement the solution in this codebase. You may run go test on the affected packages to verify your work and iterate until tests pass. Do not modify files under /tests/."

bash /tests/test.sh
echo "HARBOR_REWARD=$(cat /logs/verifier/reward.txt 2>/dev/null || echo 0)"
"""


def _build_volumes(
    secret_volumes: list[dict] | None,
) -> tuple[list | None, list | None]:
    if not secret_volumes:
        return None, None
    volumes = []
    volume_mounts = []
    for sv in secret_volumes:
        vol_name = f"secret-{sv['secret_name']}"
        items = None
        if sv.get("items"):
            items = [
                client.V1KeyToPath(key=item["key"], path=item["path"])
                for item in sv["items"]
            ]
        volumes.append(
            client.V1Volume(
                name=vol_name,
                secret=client.V1SecretVolumeSource(
                    secret_name=sv["secret_name"],
                    items=items,
                ),
            )
        )
        volume_mounts.append(
            client.V1VolumeMount(
                name=vol_name,
                mount_path=sv["mount_path"],
                read_only=True,
            )
        )
    return volumes, volume_mounts


def run_task_job(
    task_name: str,
    task_image: str,
    namespace: str,
    timeout_sec: int = 600,
    cpu: str = "2",
    memory: str = "4Gi",
    run_as_user: int = 1001,
    env_from_secrets: list[str] | None = None,
    env_from_configmaps: list[str] | None = None,
    agent: str = "oracle",
    model: str = "",
    secret_volumes: list[dict] | None = None,
) -> dict[str, Any]:
    """Run a Harbor task as a K8s Job and return the result.

    Returns dict with keys: reward, stdout, duration_s, exit_code
    """
    _load_k8s_config()
    batch_v1 = client.BatchV1Api()
    core_v1 = client.CoreV1Api()

    job_name = f"harbor-{task_name}-{int(time.monotonic()) % 100000}"

    if agent == "oracle":
        script = _oracle_script()
    elif agent in ("claude-code", "agent"):
        script = _agent_script(model=model)
    else:
        raise ValueError(f"Unknown agent: {agent!r}. Must be 'oracle', 'claude-code', or 'agent'.")

    volumes, volume_mounts = _build_volumes(secret_volumes)

    job = client.V1Job(
        metadata=client.V1ObjectMeta(name=job_name, namespace=namespace),
        spec=client.V1JobSpec(
            backoff_limit=0,
            active_deadline_seconds=timeout_sec,
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    security_context=client.V1PodSecurityContext(
                        run_as_user=run_as_user,
                    ),
                    volumes=volumes,
                    containers=[
                        client.V1Container(
                            name="task",
                            image=task_image,
                            image_pull_policy="Always",
                            command=["/bin/bash", "-c", script],
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": cpu, "memory": memory},
                                limits={"cpu": cpu, "memory": memory},
                            ),
                            env_from=[
                                *[
                                    client.V1EnvFromSource(
                                        secret_ref=client.V1SecretEnvSource(name=s)
                                    )
                                    for s in (env_from_secrets or [])
                                ],
                                *[
                                    client.V1EnvFromSource(
                                        config_map_ref=client.V1ConfigMapEnvSource(name=c)
                                    )
                                    for c in (env_from_configmaps or [])
                                ],
                            ] or None,
                            volume_mounts=volume_mounts,
                        )
                    ],
                ),
            ),
        ),
    )

    start_time = time.monotonic()
    log.info("Creating K8s Job %s in %s", job_name, namespace)
    batch_v1.create_namespaced_job(namespace, job)

    reward = 0.0
    stdout = ""
    exit_code = -1

    try:
        while time.monotonic() - start_time < timeout_sec + 30:
            job_status = batch_v1.read_namespaced_job_status(job_name, namespace)

            if job_status.status.succeeded:
                exit_code = 0
                break
            if job_status.status.failed:
                exit_code = 1
                break

            time.sleep(5)

        pods = core_v1.list_namespaced_pod(
            namespace, label_selector=f"job-name={job_name}"
        )
        if pods.items:
            pod_name = pods.items[0].metadata.name
            try:
                stdout = core_v1.read_namespaced_pod_log(pod_name, namespace)
                for line in stdout.splitlines():
                    if line.startswith("HARBOR_REWARD="):
                        try:
                            reward = float(line.split("=", 1)[1].strip())
                        except (ValueError, TypeError):
                            log.warning("Malformed reward line: %s", line)
            except ApiException as e:
                log.warning("Failed to read pod logs: %s", e)

    finally:
        try:
            batch_v1.delete_namespaced_job(
                job_name, namespace, propagation_policy="Background",
            )
        except ApiException:
            pass

    duration = time.monotonic() - start_time
    return {
        "reward": reward,
        "stdout": stdout,
        "duration_s": round(duration, 1),
        "exit_code": exit_code,
    }
