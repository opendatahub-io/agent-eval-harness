"""Podman BaseEnvironment for Harbor.

A compose-free Harbor environment that runs each trial in a single Podman
container via ``podman run``/``exec``/``cp``. Like the Kubernetes env it is a
generic exec/copy surface — Harbor drives the agent, oracle, and verifier,
preserving the agent zoo and ``reward.json`` contract.

Usage::

    harbor run -p <task> --agent claude-code -m <model> \\
      --environment-import-path agent_eval.harbor.podman:PodmanEnvironment

Only Linux containers are supported. The image is taken from
``[environment].docker_image`` (preferred) or built from ``environment/Dockerfile``.
"""

import asyncio
import re
import shlex
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.models.task.config import TaskOS

import os

_PODMAN = os.environ.get("PODMAN_BINARY", "podman")

# Env vars forwarded into the Podman trial container. Includes provider config
# AND API keys — the container runs on the host machine (no security boundary).
# On K8s, API keys come from a Secret (AGENT_EVAL_K8S_CREDENTIALS_SECRET).
_FORWARD_ENV = (
    # Provider config
    "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    "GOOGLE_CLOUD_PROJECT", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "AWS_REGION",
    # API keys
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
)

# Where a mounted GCP credentials file lands inside the container.
_CONTAINER_CREDS = "/var/creds/creds.json"


def _container_name(session_id: str) -> str:
    """Derive a valid container name from the trial session id."""
    name = session_id.lower()
    name = re.sub(r"[^a-z0-9_.-]", "-", name)
    return f"aeh-{name}"[:120]


class PodmanEnvironment(BaseEnvironment):
    """Single-container Harbor environment backed by the Podman CLI."""

    def __init__(self, *args, keep_containers: bool | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._container = _container_name(self.session_id)
        # Keep the container after the trial for `podman logs`/`podman exec` debugging.
        if keep_containers is None:
            keep_containers = os.environ.get("AGENT_EVAL_PODMAN_KEEP_RUN") == "1"
        self._keep_containers = keep_containers
        self._started = False

    # --- identity / capabilities -------------------------------------------

    @staticmethod
    def type() -> str:
        return "podman"

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            gpus=False,
            tpus=False,
            disable_internet=True,   # podman run --network none
            network_allowlist=False,
            windows=False,
            mounted=False,           # we copy in/out, no host bind for logs
            docker_compose=False,
        )

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # podman run --cpus / --memory apply hard ceilings.
        return EnvironmentResourceCapabilities(
            cpu_limit=True, cpu_request=False,
            memory_limit=True, memory_request=False,
        )

    @classmethod
    def preflight(cls) -> None:
        import shutil
        import sys
        if not shutil.which(_PODMAN):
            raise SystemExit(
                f"Podman ('{_PODMAN}') is not installed or not on PATH. "
                "Install Podman (and `podman machine start` on macOS) and retry."
            )

    def _validate_definition(self) -> None:
        if self.task_env_config.docker_image:
            return
        dockerfile = self.environment_dir / "Dockerfile"
        if not dockerfile.is_file():
            raise FileNotFoundError(
                f"Podman environment needs [environment].docker_image or "
                f"{dockerfile} — neither found."
            )

    # --- podman command helper ---------------------------------------------

    async def _podman(self, args: list[str], timeout_sec: int | None = None,
                      input_bytes: bytes | None = None) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            _PODMAN, *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_bytes), timeout=timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(stdout="", stderr=f"podman timed out after {timeout_sec}s",
                              return_code=124)
        return ExecResult(
            stdout=(stdout or b"").decode(errors="replace"),
            stderr=(stderr or b"").decode(errors="replace"),
            return_code=proc.returncode,
        )

    # --- lifecycle ----------------------------------------------------------

    async def start(self, force_build: bool) -> None:
        if self.os == TaskOS.WINDOWS:
            raise RuntimeError("PodmanEnvironment supports Linux containers only.")

        image = self.task_env_config.docker_image
        if not image or force_build:
            dockerfile = self.environment_dir / "Dockerfile"
            if dockerfile.is_file():
                image = f"aeh-{_container_name(self.session_id)}:latest"
                build = await self._podman(
                    ["build", "-t", image, str(self.environment_dir)], timeout_sec=1800)
                if build.return_code != 0:
                    raise RuntimeError(f"podman build failed:\n{build.stderr}")
        if not image:
            raise RuntimeError("No image to run (set [environment].docker_image)")

        # Remove any stale container with the same name.
        await self._podman(["rm", "-f", self._container])

        run_args = ["run", "-d", "--name", self._container, "--entrypoint", "sleep"]
        if self._effective_cpus:
            run_args += ["--cpus", str(self._effective_cpus)]
        if self._effective_memory_mb:
            run_args += ["--memory", f"{self._effective_memory_mb}m"]
        if self._network_disabled:
            run_args += ["--network", "none"]

        # Forward NON-SECRET provider config so claude-code knows how to auth.
        forwarded = {k: os.environ[k] for k in _FORWARD_ENV if os.environ.get(k)}

        # Credentials: only via an explicitly provided file, read-only mounted —
        # never the host's personal ADC. AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE should point at a
        # service-account key (or a Workload-Identity credential config).
        creds_file = os.environ.get("AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE")
        if creds_file and Path(creds_file).is_file():
            run_args += ["-v", f"{Path(creds_file).resolve()}:{_CONTAINER_CREDS}:ro"]
            forwarded["GOOGLE_APPLICATION_CREDENTIALS"] = _CONTAINER_CREDS

        # Project resources from a host directory (bind-mount, read-only).
        # Podman equivalent of AGENT_EVAL_K8S_PROJECT_CONFIGMAP — with this, no
        # project-specific image is needed.
        project_dir = os.environ.get("AGENT_EVAL_PODMAN_PROJECT_DIR")
        if project_dir and Path(project_dir).is_dir():
            project_mount = os.environ.get("AGENT_EVAL_PODMAN_PROJECT_MOUNT", "/opt/project")
            run_args += ["-v", f"{Path(project_dir).resolve()}:{project_mount}:ro"]
            forwarded["AGENT_EVAL_PROJECT_DIR"] = project_mount

        merged_env = {**forwarded, **(self._persistent_env or {})}
        for key, value in merged_env.items():
            run_args += ["-e", f"{key}={value}"]
        self._persistent_env = merged_env

        run_args += [image, "infinity"]

        result = await self._podman(run_args, timeout_sec=300)
        if result.return_code != 0:
            raise RuntimeError(f"podman run failed:\n{result.stderr}")
        self._started = True

        # Upload task environment/ for prebuilt-image tasks (base helper).
        await self._upload_environment_dir_after_start()

    async def stop(self, delete: bool) -> None:
        if not self._started:
            return
        if self._keep_containers:
            # Leave it running for `podman logs`/`podman exec` debugging.
            self.logger.info(
                "Keeping container %s (AGENT_EVAL_PODMAN_KEEP_RUN). Inspect with: "
                "podman logs %s | podman exec -it %s /bin/sh",
                self._container, self._container, self._container)
            return
        await self._podman(["stop", "-t", "5", self._container], timeout_sec=60)
        if delete:
            await self._podman(["rm", "-f", self._container], timeout_sec=60)

    # --- exec ---------------------------------------------------------------

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        user = self._resolve_user(user)
        env = self._merge_env(env)

        args = ["exec"]
        effective_cwd = cwd or self.task_env_config.workdir
        if effective_cwd:
            args += ["-w", effective_cwd]
        if env:
            for key, value in env.items():
                args += ["-e", f"{key}={value}"]
        if user is not None:
            args += ["-u", str(user)]
        args.append(self._container)
        # Run through a shell so pipelines / redirections in `command` work,
        # matching BaseInstalledAgent._exec which prefixes `set -o pipefail`.
        args += ["/bin/sh", "-c", command]

        return await self._podman(args, timeout_sec=timeout_sec)

    # --- file transfer (podman cp) -----------------------------------------

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        parent = str(Path(target_path).parent)
        if parent and parent != ".":
            await self.exec(f"mkdir -p {shlex.quote(parent)}", user="root")
        await self._cp(str(source_path), f"{self._container}:{target_path}")

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        # `podman cp <dir>/. <c>:<target>` copies contents into target.
        src = str(source_dir).rstrip("/")
        await self.exec(f"mkdir -p {shlex.quote(target_dir)}", user="root")
        await self._cp(f"{src}/.", f"{self._container}:{target_dir}")

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        await self._cp(f"{self._container}:{source_path}", str(target_path))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        await self._cp(f"{self._container}:{source_dir.rstrip('/')}/.", str(target_dir))

    async def _cp(self, src: str, dst: str) -> None:
        result = await self._podman(["cp", src, dst], timeout_sec=300)
        if result.return_code != 0:
            raise RuntimeError(f"podman cp {src} -> {dst} failed:\n{result.stderr}")
