"""Kubernetes BaseEnvironment for Harbor (Python client).

Each trial runs in a single pod created from a prebuilt task image. Like the
Podman env it is a generic exec/copy surface — Harbor drives the agent, oracle,
and verifier, preserving the agent zoo and ``reward.json`` contract.

Uses the Kubernetes Python client (``load_incluster_config()`` when running
inside a pod, falling back to local kubeconfig). Compatible with OpenShift's
restricted-v2 SCC (non-root, arbitrary assigned UID; the task image must be
UID-agnostic, i.e. group-0 writable).

Usage::

    harbor run -p <task> --agent claude-code -m <model> \\
      --environment-import-path agent_eval.harbor.kubernetes:KubernetesEnvironment

Requires ``kubernetes`` in Harbor's environment
(``/eval-setup --harbor`` or ``pip install harbor kubernetes``).
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

# For K8s, env is managed via AGENT_EVAL_K8S_CREDENTIALS_SECRET (envFrom secretRef).
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

_CREDS_MOUNT = "/var/creds"
_INCLUSTER_NS = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


def _pod_name(session_id: str) -> str:
    """RFC 1123-compliant pod name from a Harbor session id."""
    name = re.sub(r"[^a-z0-9-]", "-", session_id.lower())
    name = re.sub(r"-+", "-", name).strip("-")
    return f"aeh-{name}"[:62].strip("-")


def _load_kube_config() -> None:
    """In-cluster config when running in a pod; else local kubeconfig."""
    from kubernetes.config.config_exception import ConfigException
    try:
        k8s_config.load_incluster_config()
    except ConfigException:
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
        return 1
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
    # a network bootstrap.  Skipped by default for pre-built images; opt out
    # with AGENT_EVAL_K8S_INSTALL_PACKAGES=1 if using a bare image.
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
        r"|curl\s+-fsSL\s+https://(?:downloads\.)?claude\.ai"
    )

    def __init__(self, *args, keep_pods: bool | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if not _K8S_AVAILABLE:
            raise RuntimeError(
                "The 'kubernetes' package is required for KubernetesEnvironment. "
                "Run `/eval-setup --harbor` or `pip install harbor kubernetes`.")
        self._pod = _pod_name(self.session_id)
        self._namespace = _default_namespace()
        if keep_pods is None:
            keep_pods = os.environ.get("AGENT_EVAL_K8S_KEEP_RUN") == "1"
        self._keep_pods = keep_pods
        # Pre-built images skip install by default (the norm for K8s/OpenShift).
        # Opt out with AGENT_EVAL_K8S_INSTALL_PACKAGES=1 if using a bare image.
        self._skip_pkg_installs = os.environ.get("AGENT_EVAL_K8S_INSTALL_PACKAGES") != "1"
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
                "The 'kubernetes' package is required. "
                "Run `/eval-setup --harbor` or `pip install harbor kubernetes`.")

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
            "command": ["sh", "-c",
                        'while true; do '
                        'for f in $(find /logs \\( -name "*.log" -o -name "*.txt" \\) 2>/dev/null); do '
                        'if ! echo "$TAILED" | grep -qF "$f"; then '
                        'TAILED="$TAILED $f"; '
                        'tail -F "$f" & '
                        'fi; done; sleep 5; done'],
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
        creds_secret = os.environ.get("AGENT_EVAL_K8S_GCP_CREDENTIALS_SECRET")
        if creds_secret:
            container.setdefault("volumeMounts", []).append(
                {"name": "aeh-creds", "mountPath": _CREDS_MOUNT, "readOnly": True})
            pod_spec.setdefault("volumes", []).append(
                {"name": "aeh-creds", "secret": {"secretName": creds_secret}})
            key = os.environ.get("AGENT_EVAL_K8S_GCP_CREDENTIALS_KEY", "key.json")
            container["env"].append({
                "name": "GOOGLE_APPLICATION_CREDENTIALS",
                "value": f"{_CREDS_MOUNT}/{key}"})
        env_secret = os.environ.get("AGENT_EVAL_K8S_CREDENTIALS_SECRET")
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
        await self._restore_project_from_configmap()

    async def _restore_project_from_configmap(self) -> None:
        """Reconstruct project tree from flat ConfigMap after environment upload.

        ConfigMap keys use ``--`` instead of ``/``. This copies them into
        /workspace with the original directory structure so Claude finds
        skills at ``.claude/skills/``, scripts at ``scripts/``, etc.
        """
        project_mount = os.environ.get("AGENT_EVAL_K8S_PROJECT_MOUNT",
                                       "/opt/project") \
            if os.environ.get("AGENT_EVAL_K8S_PROJECT_CONFIGMAP") else None
        if not project_mount:
            return
        cmd = (
            f'for f in {project_mount}/* {project_mount}/.*; do '
            '[ -f "$f" ] || continue; '
            'n=$(basename "$f"); '
            't=$(echo "$n" | sed "s/--/\\//g"); '
            'mkdir -p "$(dirname "/workspace/$t")"; '
            'cp "$f" "/workspace/$t"; '
            'done'
        )
        await self._checked_exec(cmd, "restore project from configmap")

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

    # Retry only failures that happen BEFORE the command starts executing
    # (the WebSocket never established). At that point nothing ran in the
    # container, so a retry is safe even for non-idempotent commands like the
    # agent run. Post-establishment drops/timeouts are NEVER retried here —
    # re-running a command that may have already executed (the agent!) would be
    # unsafe; long connections are protected by the keepalive instead.
    _EXEC_ESTABLISH_RETRIES = 2      # total attempts = retries + 1
    _EXEC_RETRY_BACKOFF_SEC = 0.5    # 0.5s, 1.0s, ...

    def _ws_exec_once(
        self, command: str, timeout_sec: int | None
    ) -> tuple[ExecResult, bool, BaseException | None]:
        """Single WebSocket exec attempt.

        Returns ``(result, established, exc)`` where *established* is True once
        ``k8s_stream`` has returned a live connection (i.e. the command has been
        handed to the container). When *established* is False the command
        provably never ran, so the caller may safely retry.

        A keepalive ping every 30 s resets HAProxy's ≥60 s idle timer so a long
        agent run survives on a single connection. A daemon thread with a hard
        wall-clock deadline guards against the ``resp.close()`` /
        ``read_channel()`` deadlock that occurs when HAProxy silently tears down
        a connection without a TCP FIN or WebSocket close frame.
        """
        result_holder: list[ExecResult] = []
        exc_holder:    list[BaseException] = []
        established:   list[bool] = []
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
                established.append(True)  # connection up — command is now running
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
            # The worker is still alive (likely wedged in k8s_stream). It may yet
            # establish the connection and run the command after we return, so we
            # must NOT report "never established" — that would let the caller
            # retry and double-execute a non-idempotent command. Force
            # established=True so this attempt is treated as non-retryable.
            return ExecResult(
                stdout="",
                stderr=f"[ws_exec hard timeout after {hard_limit}s — HAProxy connection dead]",
                return_code=124), True, None
        is_established = bool(established)
        if exc_holder:
            return ExecResult(
                stdout="", stderr=f"[exec error: {exc_holder[0]}]",
                return_code=1), is_established, exc_holder[0]
        result = result_holder[0] if result_holder else ExecResult(
            stdout="", stderr="[no result from exec thread]", return_code=1)
        return result, is_established, None

    def _log_exec(self, command: str, result: ExecResult, established: bool,
                  exc: BaseException | None, duration: float, attempts: int) -> None:
        """Record the outcome of an exec so failures are diagnosable.

        Harbor calls ``exec`` for everything (agent, verifier, file transfer) and
        only surfaces a generic "step failed" on error, so without this the
        return code / stderr / establishment state of a failed exec is lost.
        Successful execs log at debug; failures log at warning with the fields
        that explain *why* (rc, established vs post-establishment drop, duration,
        retry count). ``cmd`` is the first line truncated so a large upload chunk
        doesn't flood the log; ``detail`` is escaped (untrusted container output,
        CWE-117) and bounded (CWE-532).
        """
        rc = getattr(result, "return_code", None)
        cmd = (command.strip().splitlines() or [""])[0][:160]
        if exc is None and rc in (None, 0):
            self.logger.debug("exec ok: rc=0 dur=%.2fs attempts=%d cmd=%r",
                              duration, attempts, cmd)
            return
        detail = (f"{type(exc).__name__}: {exc}" if exc is not None
                  else (result.stderr or ""))
        detail = detail.encode("unicode_escape").decode("ascii")[:300]
        self.logger.warning(
            "exec FAILED: rc=%s established=%s dur=%.2fs attempts=%d cmd=%r detail=%r",
            rc, established, duration, attempts, cmd, detail)

    def _ws_exec(self, command: str, timeout_sec: int | None) -> ExecResult:
        """WebSocket exec, retrying only pre-execution (establishment) failures.

        Wraps :meth:`_ws_exec_once`. A failure where the connection never
        established (transient HAProxy refusal/drop during ``k8s_stream``) is
        retried with backoff because the command provably never ran. Any failure
        after the command started — timeout, mid-stream drop, non-zero exit — is
        returned/raised as-is so a possibly-executed command is never re-run.
        """
        attempts = self._EXEC_ESTABLISH_RETRIES + 1
        start = time.monotonic()
        for attempt in range(1, attempts + 1):
            result, established, exc = self._ws_exec_once(command, timeout_sec)
            if established:
                self._log_exec(command, result, established, exc,
                               time.monotonic() - start, attempt)
                if exc is not None:
                    raise exc  # post-establishment failure — preserve old contract
                return result
            # Not established: the command never started — safe to retry.
            if attempt < attempts:
                backoff = self._EXEC_RETRY_BACKOFF_SEC * (2 ** (attempt - 1))
                self.logger.warning(
                    "exec failed to establish (attempt %d/%d); retrying in %.1fs: %s",
                    attempt, attempts, backoff, (result.stderr or "")[:160])
                time.sleep(backoff)
                continue
            # Exhausted retries with no establishment.
            self._log_exec(command, result, established, exc,
                           time.monotonic() - start, attempt)
            if exc is not None:
                raise exc
            return result

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
            self.logger.debug("skip pkg-install (pre-built image, default for K8s)")
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
    # (which has no clean half-close for tar's EOF). The base64 blob is written
    # to a temp file in chunks (see _write_b64_chunked) rather than passed as a
    # single argument: Linux caps one argv entry at MAX_ARG_STRLEN (128 KiB)
    # regardless of the larger total ARG_MAX, so a big blob as one `printf` arg
    # fails with E2BIG (this silently broke "upload agent logs back to
    # environment" for every multi-step trial). Downloads stream out via stdout,
    # which has no such limit.

    # Stay well under Linux's 128 KiB single-argument cap (MAX_ARG_STRLEN), with
    # headroom for the rest of the command line.
    _B64_CHUNK = 100_000

    async def _write_b64_chunked(self, b64: str, remote_path: str, what: str) -> None:
        """Write a base64 string to *remote_path* in sub-arg-limit chunks.

        Each chunk is one exec (`printf %s <chunk> >> file`); the first truncates,
        the rest append. Transient establishment failures are retried by _ws_exec.
        """
        q = shlex.quote(remote_path)
        if not b64:
            await self._checked_exec(f": > {q}", f"{what}: create empty")
            return
        for offset in range(0, len(b64), self._B64_CHUNK):
            chunk = b64[offset:offset + self._B64_CHUNK]
            redir = ">" if offset == 0 else ">>"
            await self._checked_exec(
                f"printf %s {shlex.quote(chunk)} {redir} {q}",
                f"{what}: chunk @{offset}")

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        b64 = base64.b64encode(Path(source_path).read_bytes()).decode()
        parent = shlex.quote(str(Path(target_path).parent))
        tmp = f"{target_path}.aeh-b64.tmp"
        qt, qtmp = shlex.quote(target_path), shlex.quote(tmp)
        await self._checked_exec(f"mkdir -p {parent}", f"upload_file mkdir -> {target_path}")
        await self._write_b64_chunked(b64, tmp, f"upload_file -> {target_path}")
        await self._checked_exec(
            f"base64 -d {qtmp} > {qt} && rm -f {qtmp}",
            f"upload_file decode -> {target_path}")

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        def _reset_perms(info):
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = 0o755 if info.isdir() else 0o644
            return info

        # gzip the tar — agent-log dirs are text and compress ~5-10x, which keeps
        # the chunk count (and thus exec count) low.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for item in sorted(Path(source_dir).iterdir()):
                tf.add(item, arcname=item.name, filter=_reset_perms)
        b64 = base64.b64encode(buf.getvalue()).decode()
        tgt = shlex.quote(target_dir)
        tmp = "/tmp/aeh-upload-dir.b64"
        qtmp = shlex.quote(tmp)
        await self._checked_exec(f"mkdir -p {tgt}", f"upload_dir mkdir -> {target_dir}")
        await self._write_b64_chunked(b64, tmp, f"upload_dir -> {target_dir}")
        await self._checked_exec(
            f"base64 -d {qtmp} | tar xzmf - --no-same-owner --no-same-permissions "
            f"-C {tgt} 2>/dev/null; rm -f {qtmp}; true",
            f"upload_dir extract -> {target_dir}")

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
