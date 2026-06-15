"""Kubernetes/OpenShift BaseEnvironment for Harbor (Python client).

The OpenShift sibling of the Podman env: each trial runs in a single pod created
from a prebuilt task image. Like the Podman env it is a generic exec/copy surface
(Harbor drives the agent + oracle + verifier), so the agent zoo and `reward.json`
contract are preserved — unlike PR #78's k8s mode, which reimplemented the
oracle/verifier in bash and scraped rewards from pod logs.

Uses the **Kubernetes Python client**, not the `oc` CLI, because the real target
is in-cluster: the EvalHub/TrustyAI provider runs Harbor inside an OpenShift pod
where there's no `oc` binary or kubeconfig — `load_incluster_config()` uses the
pod's ServiceAccount token. (Locally it falls back to your kubeconfig.) It runs
under the restricted-v2 SCC (non-root, arbitrary assigned UID; the task image must
be UID-agnostic, i.e. group-0 writable).

Plug in without forking Harbor:

    PYTHONPATH=<repo> harbor run -p <task> --agent claude-code -m <model> \\
      --environment-import-path agent_eval.harbor.kubernetes:KubernetesEnvironment

Requires the `kubernetes` package in Harbor's environment
(``uv tool install harbor --with kubernetes``). Config via env:
AGENT_EVAL_K8S_NAMESPACE, AGENT_EVAL_K8S_SERVICE_ACCOUNT, AGENT_EVAL_K8S_CREDS_SECRET / _KEY / _MOUNT,
AGENT_EVAL_K8S_ENV_SECRET, AGENT_EVAL_K8S_CPU / _MEMORY, AGENT_EVAL_K8S_KEEP_RUN.
"""

import asyncio
import base64
import io
import json
import os
import re
import shlex
import tarfile
import threading
import time
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.models.task.config import TaskOS

# For K8s, env is managed via AGENT_EVAL_K8S_ENV_SECRET (envFrom secretRef).
# Only forward model-routing hints that are safe to inherit from the host.
# Deliberately excludes cloud-provider auth vars (CLAUDE_CODE_USE_VERTEX,
# ANTHROPIC_VERTEX_PROJECT_ID, CLAUDE_CODE_USE_BEDROCK, AWS_REGION, etc.)
# because those reflect the *developer's* local setup and would override the
# in-cluster LiteLLM gateway config baked into the K8s secret.
_FORWARD_ENV = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_BASE_URL",
)

try:
    from kubernetes import client as k8s_client, config as k8s_config
    from kubernetes.client.rest import ApiException
    from kubernetes.stream import stream as k8s_stream
    from kubernetes.stream.ws_client import ERROR_CHANNEL
    _K8S_AVAILABLE = True
except ImportError:
    _K8S_AVAILABLE = False
    ApiException = Exception  # type: ignore[assignment,misc]

_CREDS_MOUNT = os.environ.get("AGENT_EVAL_K8S_CREDS_MOUNT", "/var/creds")
_INCLUSTER_NS = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


def _pod_name(session_id: str) -> str:
    """RFC 1123-compliant pod name from a Harbor session id."""
    name = re.sub(r"[^a-z0-9-]", "-", session_id.lower())
    name = re.sub(r"-+", "-", name).strip("-")
    return f"aeh-{name}"[:62].strip("-")


def _load_kube_config() -> None:
    """In-cluster config when running in a pod; else local kubeconfig."""
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()


def _default_namespace() -> str:
    ns = os.environ.get("AGENT_EVAL_K8S_NAMESPACE")
    if ns:
        return ns
    if _INCLUSTER_NS.is_file():
        try:
            return _INCLUSTER_NS.read_text().strip() or "default"
        except OSError:
            pass
    try:
        _, active = k8s_config.list_kube_config_contexts()
        ns = (active or {}).get("context", {}).get("namespace")
        if ns:
            return ns
    except Exception:
        pass
    return "default"


def _returncode_from_status(err_channel: str | None) -> int:
    """Parse the exec ERROR_CHANNEL v1.Status JSON into an exit code."""
    if not err_channel:
        return 0
    try:
        status = json.loads(err_channel)
    except (json.JSONDecodeError, TypeError):
        return 0
    if status.get("status") == "Success":
        return 0
    for cause in (status.get("details") or {}).get("causes") or []:
        if cause.get("reason") == "ExitCode":
            try:
                return int(cause.get("message"))
            except (TypeError, ValueError):
                return 1
    return 1


class KubernetesEnvironment(BaseEnvironment):
    """Single-pod Harbor environment backed by the Kubernetes Python client."""

    # Patterns Harbor emits during agent setup / install that require root or
    # a network bootstrap.  Matched in exec() when
    # AGENT_EVAL_K8S_SKIP_PKG_INSTALLS=1 so pre-built images (which already
    # have every dependency baked in) can skip commands that would fail under
    # OpenShift's restricted-v2 SCC (no root, no internet egress).
    #
    # Covers all branches of claude_code.install() + BaseInstalledAgent.setup():
    #   1. Root pkg installs  – apk add / apt-get install / dnf install / yum install
    #   2. npm global install – npm install -g @anthropic-ai/claude-code
    #   3. Claude bootstrap   – curl -fsSL https://downloads.claude.ai/…  | bash
    _PKG_INSTALL_RE = re.compile(
        r"\b(apk\s+add"
        r"|apt-get\s+(?:update\s*&&\s*apt-get\s+)?install"
        r"|dnf\s+install"
        r"|yum\s+install"
        r"|npm\s+install\s+-g"
        r")\b"
        r"|curl\s+-fsSL\s+https://downloads\.claude\.ai"
    )

    def __init__(self, *args, keep_pods: bool | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if not _K8S_AVAILABLE:
            raise RuntimeError(
                "The 'kubernetes' package is required for KubernetesEnvironment. "
                "Install it in Harbor's environment: "
                "`uv tool install harbor --with kubernetes` (or pip install kubernetes).")
        self._pod = _pod_name(self.session_id)
        self._namespace = _default_namespace()
        if keep_pods is None:
            keep_pods = os.environ.get("AGENT_EVAL_K8S_KEEP_RUN") == "1"
        self._keep_pods = keep_pods
        # When the task image is pre-built with all required packages, set
        # AGENT_EVAL_K8S_SKIP_PKG_INSTALLS=1 to suppress install commands that
        # would fail under OpenShift's restricted-v2 SCC (no root access).
        self._skip_pkg_installs = os.environ.get("AGENT_EVAL_K8S_SKIP_PKG_INSTALLS") == "1"
        self._started = False
        _load_kube_config()
        self._core = k8s_client.CoreV1Api()

    @staticmethod
    def type() -> str:
        return "kubernetes"

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            gpus=False, tpus=False, disable_internet=False,
            network_allowlist=False, windows=False, mounted=False,
            docker_compose=False,
        )

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        return EnvironmentResourceCapabilities(
            cpu_limit=True, cpu_request=True,
            memory_limit=True, memory_request=True,
        )

    @classmethod
    def preflight(cls) -> None:
        if not _K8S_AVAILABLE:
            raise SystemExit(
                "The 'kubernetes' package is required. Install with "
                "`uv tool install harbor --with kubernetes`.")

    def _validate_definition(self) -> None:
        if not self.task_env_config.docker_image:
            raise FileNotFoundError(
                "Kubernetes environment requires [environment].docker_image "
                "(a registry image the cluster can pull); in-cluster build is "
                "not supported.")

    # --- pod manifest -------------------------------------------------------

    def _pod_manifest(self, image: str, env: dict) -> dict:
        """Restricted-v2-compliant single-container pod (sleep keepalive).

        No runAsUser → the SCC admission assigns a UID from the namespace range;
        the task image must be UID-agnostic (group-0 writable). Credentials come
        from the cluster (Secret mount / Workload Identity), never the host.
        """
        cpu = str(self._effective_cpus) if self._effective_cpus else \
            os.environ.get("AGENT_EVAL_K8S_CPU", "1")
        mem = f"{self._effective_memory_mb}Mi" if self._effective_memory_mb else \
            os.environ.get("AGENT_EVAL_K8S_MEMORY", "2Gi")
        requests = {"cpu": cpu, "memory": mem}
        resources = {"requests": requests, "limits": dict(requests)}

        labels = {"app.kubernetes.io/managed-by": "agent-eval-harness"}

        container: dict = {
            "name": "main",
            "image": image,
            "command": ["sleep", "infinity"],
            "env": [{"name": k, "value": v} for k, v in env.items()],
            "resources": resources,
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "runAsNonRoot": True,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        }
        pod_spec: dict = {"restartPolicy": "Never", "containers": [container]}

        # Credentials from the cluster — never copied from the host.
        sa = os.environ.get("AGENT_EVAL_K8S_SERVICE_ACCOUNT")
        if sa:
            pod_spec["serviceAccountName"] = sa
        creds_secret = os.environ.get("AGENT_EVAL_K8S_CREDS_SECRET")
        if creds_secret:
            container.setdefault("volumeMounts", []).append(
                {"name": "aeh-creds", "mountPath": _CREDS_MOUNT, "readOnly": True})
            pod_spec.setdefault("volumes", []).append(
                {"name": "aeh-creds", "secret": {"secretName": creds_secret}})
            key = os.environ.get("AGENT_EVAL_K8S_CREDS_KEY", "key.json")
            container["env"].append({
                "name": "GOOGLE_APPLICATION_CREDENTIALS",
                "value": f"{_CREDS_MOUNT}/{key}"})
        env_secret = os.environ.get("AGENT_EVAL_K8S_ENV_SECRET")
        if env_secret:
            container["envFrom"] = [{"secretRef": {"name": env_secret}}]

        # Project resources from a ConfigMap (skills, scripts, .context, CLAUDE.md).
        # Mounted read-only; the agent copies what it needs into /workspace at run
        # time. With this, no project-specific image is needed — the generic base
        # image + a ConfigMap covers any project.
        project_cm = os.environ.get("AGENT_EVAL_K8S_PROJECT_CONFIGMAP")
        if project_cm:
            project_mount = os.environ.get("AGENT_EVAL_K8S_PROJECT_MOUNT", "/opt/project")
            container.setdefault("volumeMounts", []).append(
                {"name": "aeh-project", "mountPath": project_mount, "readOnly": True})
            pod_spec.setdefault("volumes", []).append(
                {"name": "aeh-project", "configMap": {
                    "name": project_cm, "defaultMode": 0o755}})
            container["env"].append({
                "name": "AGENT_EVAL_PROJECT_DIR", "value": project_mount})

        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": self._pod, "namespace": self._namespace,
                         "labels": labels},
            "spec": pod_spec,
        }

    # --- lifecycle ----------------------------------------------------------

    async def start(self, force_build: bool) -> None:
        if self.os == TaskOS.WINDOWS:
            raise RuntimeError("KubernetesEnvironment supports Linux containers only.")
        image = self.task_env_config.docker_image

        forwarded = {k: os.environ[k] for k in _FORWARD_ENV if os.environ.get(k)}
        pod_env = {**forwarded, **(self._persistent_env or {})}
        self._persistent_env = pod_env
        manifest = self._pod_manifest(image, pod_env)

        await asyncio.to_thread(self._delete_pod_quiet)
        try:
            await asyncio.to_thread(
                self._core.create_namespaced_pod, self._namespace, manifest)
        except ApiException as exc:
            raise RuntimeError(f"pod create failed: {exc}") from exc

        await self._wait_ready(timeout_sec=300)
        self._started = True
        await self._upload_environment_dir_after_start()

    def _delete_pod_quiet(self) -> None:
        try:
            self._core.delete_namespaced_pod(
                self._pod, self._namespace, grace_period_seconds=0)
        except ApiException as exc:
            if getattr(exc, "status", None) != 404:
                self.logger.debug("delete stale pod: %s", exc)

    async def _wait_ready(self, timeout_sec: int) -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                pod = await asyncio.to_thread(
                    self._core.read_namespaced_pod, self._pod, self._namespace)
            except ApiException as exc:
                raise RuntimeError(f"read pod failed: {exc}") from exc
            status = pod.status
            phase = (status.phase or "") if status else ""
            conds = {c.type: c.status for c in (status.conditions or [])} \
                if status else {}
            if conds.get("Ready") == "True":
                return
            if phase in ("Failed", "Succeeded"):
                raise RuntimeError(f"pod {self._pod} entered phase {phase} before Ready")
            await asyncio.sleep(3)
        raise RuntimeError(f"pod {self._pod} not Ready after {timeout_sec}s")

    async def stop(self, delete: bool) -> None:
        if not self._started:
            return
        if self._keep_pods:
            self.logger.info(
                "Keeping pod %s in namespace %s (AGENT_EVAL_K8S_KEEP_RUN). Inspect with: "
                "kubectl logs %s -n %s | kubectl exec -it %s -n %s -- /bin/sh",
                self._pod, self._namespace, self._pod, self._namespace,
                self._pod, self._namespace)
            return
        if delete:
            await asyncio.to_thread(self._delete_pod_quiet)

    # --- exec (websocket) ---------------------------------------------------

    @staticmethod
    def _ws_keepalive(
        resp: object,
        interval: int,
        stop: threading.Event,
        lock: threading.Lock,
    ) -> None:
        """Send WebSocket ping frames every *interval* seconds.

        HAProxy resets its idle-connection timer on any wire-level frame,
        including WebSocket pings.  A ping every 30 s keeps long-running
        agent commands alive without the complexity of fire-and-poll.

        *lock* is shared with the read loop in ``_ws_exec`` so that ping
        and ``resp.update()`` never race on the underlying socket.
        Stops silently when *stop* is set or the underlying socket is gone.
        """
        while not stop.wait(interval):
            try:
                with lock:
                    resp.sock.ping()  # type: ignore[union-attr]
            except Exception:
                break

    def _ws_exec(self, command: str, timeout_sec: int | None) -> ExecResult:
        """WebSocket exec with a keepalive ping for HAProxy resilience.

        OpenShift's HAProxy router drops WebSocket connections idle for ≥60 s.
        Sending a ping frame every 30 s resets the idle timer and keeps the
        single long-lived connection alive for the full agent run.

        A daemon thread with a hard wall-clock deadline guards against the
        ``resp.close()`` / ``read_channel()`` deadlock that occurs when HAProxy
        silently tears down a connection without sending a TCP FIN or WebSocket
        close frame.
        """
        result_holder: list[ExecResult] = []
        exc_holder:    list[BaseException] = []
        stop_ping = threading.Event()
        ws_lock   = threading.Lock()

        def _run() -> None:
            try:
                resp = k8s_stream(
                    self._core.connect_get_namespaced_pod_exec,
                    self._pod, self._namespace, container="main",
                    command=["/bin/sh", "-c", command],
                    stderr=True, stdin=False, stdout=True, tty=False,
                    _preload_content=False,
                )
                threading.Thread(
                    target=self._ws_keepalive,
                    args=(resp, 30, stop_ping, ws_lock),
                    daemon=True,
                ).start()
                out: list[str] = []
                err: list[str] = []
                deadline = time.monotonic() + timeout_sec if timeout_sec else None
                while resp.is_open():
                    with ws_lock:
                        resp.update(timeout=1)
                    if resp.peek_stdout():
                        out.append(resp.read_stdout())
                    if resp.peek_stderr():
                        err.append(resp.read_stderr())
                    if deadline and time.monotonic() > deadline:
                        stop_ping.set()
                        with ws_lock:
                            try:
                                resp.close()
                            except Exception:
                                pass
                        result_holder.append(ExecResult(
                            stdout="".join(out),
                            stderr="".join(err) + f"\n[timed out after {timeout_sec}s]",
                            return_code=124))
                        return
                stop_ping.set()
                # Hold ws_lock for the final read_channel + close so we don't
                # race against a keepalive ping that passed the stop check just
                # before stop_ping was set.
                with ws_lock:
                    try:
                        err_channel = resp.read_channel(ERROR_CHANNEL)
                    except Exception:
                        err_channel = None
                    try:
                        resp.close()
                    except Exception:
                        pass
                result_holder.append(ExecResult(
                    stdout="".join(out), stderr="".join(err),
                    return_code=_returncode_from_status(err_channel)))
            except Exception as exc:  # noqa: BLE001
                stop_ping.set()
                exc_holder.append(exc)

        # Hard deadline guards against resp.close() / read_channel() deadlock.
        # When timeout_sec is set, the +30 s buffer ensures the soft deadline
        # inside _run fires before the hard limit. When timeout_sec is None
        # there is no soft deadline and the hard limit alone applies (1 h cap).
        hard_limit = (timeout_sec or 3600) + 30
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=hard_limit)
        if t.is_alive():
            stop_ping.set()
            return ExecResult(
                stdout="",
                stderr=f"[ws_exec hard timeout after {hard_limit}s — HAProxy connection dead]",
                return_code=124)
        if exc_holder:
            raise exc_holder[0]
        return result_holder[0] if result_holder else ExecResult(
            stdout="", stderr="[no result from exec thread]", return_code=1)

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        # `user` is ignored: the pod runs as its SCC-assigned UID and exec can't
        # switch users. cwd/env are folded into the shell command.
        if self._skip_pkg_installs and self._PKG_INSTALL_RE.search(command):
            self.logger.debug("skip pkg-install: AGENT_EVAL_K8S_SKIP_PKG_INSTALLS=1")
            return ExecResult(stdout="[skipped: pre-built image]", stderr="", return_code=0)
        prefix = ""
        effective_cwd = cwd or self.task_env_config.workdir
        if effective_cwd:
            prefix += f"cd {shlex.quote(effective_cwd)} && "
        if env:
            for key, value in env.items():
                prefix += f"export {key}={shlex.quote(str(value))}; "
        return await asyncio.to_thread(self._ws_exec, prefix + command, timeout_sec)

    # --- file transfer (tar + base64 over exec) -----------------------------
    #
    # cp goes through `exec` + base64 rather than the websocket stdin channel
    # (which has no clean half-close for tar's EOF). Uploads (env dir, tests)
    # are small; downloads stream a base64'd tar out via stdout.

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        b64 = base64.b64encode(Path(source_path).read_bytes()).decode()
        parent = shlex.quote(str(Path(target_path).parent))
        cmd = (f"mkdir -p {parent} && printf %s {shlex.quote(b64)} | base64 -d "
               f"> {shlex.quote(target_path)}")
        await self._checked_exec(cmd, f"upload_file -> {target_path}")

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for item in sorted(Path(source_dir).iterdir()):
                tf.add(item, arcname=item.name)
        b64 = base64.b64encode(buf.getvalue()).decode()
        tgt = shlex.quote(target_dir)
        cmd = (f"mkdir -p {tgt} && printf %s {shlex.quote(b64)} | base64 -d "
               f"| tar xmf - -C {tgt}")
        await self._checked_exec(cmd, f"upload_dir -> {target_dir}")

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        res = await self.exec(f"base64 -w0 {shlex.quote(source_path)}")
        if res.return_code != 0:
            raise RuntimeError(f"download_file {source_path}: {res.stderr}")
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        Path(target_path).write_bytes(base64.b64decode(res.stdout))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        res = await self.exec(
            f"tar cf - -C {shlex.quote(source_dir)} . | base64 -w0")
        if res.return_code != 0:
            raise RuntimeError(f"download_dir {source_dir}: {res.stderr}")
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        raw = base64.b64decode(res.stdout)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tf:
            tf.extractall(target_dir, filter="data")

    async def _checked_exec(self, command: str, what: str) -> None:
        res = await self.exec(command, timeout_sec=300)
        if res.return_code != 0:
            raise RuntimeError(f"{what} failed (rc={res.return_code}): {res.stderr}")
