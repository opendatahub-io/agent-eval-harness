"""Responses API runner — agentic skill execution via OpenAI Responses API.

Uses the Skills API and Shell tool to execute eval harness skills in
hosted containers. Provides apples-to-apples comparison with
ClaudeCodeRunner: same skill, same test cases, different model/runtime.

"""

import os
import time
import threading
import uuid
from pathlib import Path
from typing import Optional

from .base import EvalRunner, RunResult

_print_lock = threading.Lock()
_global_skill_cache: dict[tuple[str, str], str] = {}
_global_skill_lock = threading.Lock()


def _sanitize_prompt(prompt: str, max_preview: int = 50) -> str:
    """Sanitize prompt for logging to prevent PII/credential exposure.

    Returns a safe string showing only length and a masked preview.

    Args:
        prompt: The raw prompt text
        max_preview: Maximum characters to show in preview (default 50)

    Returns:
        Sanitized string like: "<prompt: 245 chars, preview: 'Create a new...'"
    """
    if not prompt:
        return "<empty prompt>"

    length = len(prompt)
    preview = prompt[:max_preview].replace('\n', ' ').replace('\r', ' ')
    if length > max_preview:
        preview += "..."

    return f"<prompt: {length} chars, preview: '{preview}'>"


class ResponsesAPIRunner(EvalRunner):
    """Runs skills via OpenAI Responses API with Shell tool + Skills API.

    Lifecycle per case:
      1. Upload skill directory → skill_id (cached across cases)
      2. Create container
      3. Upload workspace files to container
      4. Execute via POST /v1/responses with shell tool
      5. Download new/modified files from container
      6. Delete container
      7. Return RunResult

    Note: ``settings_path`` and ``max_budget_usd`` are accepted for ABC
    compatibility but not wired — the Responses API does not expose
    equivalent knobs.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        network_policy: Optional[dict] = None,
        memory_limit_mb: int = 512,
        log_prefix: Optional[str] = None,
        **kwargs,
    ):
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL", "")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._default_model = default_model or os.environ.get("OPENAI_MODEL", "")
        self._network_policy = network_policy
        self._memory_limit_mb = memory_limit_mb
        self._log_prefix = log_prefix

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        """Construct from EvalConfig.

        Runner-specific settings (``base_url``, ``api_key``,
        ``default_model``, ``network_policy``, ``memory_limit_mb``)
        are read from ``runner.settings`` in eval.yaml and can be
        overridden by keyword arguments.
        """
        settings = dict(config.runner.settings or {})
        settings.update(overrides)
        return cls(
            base_url=settings.get("base_url"),
            api_key=settings.get("api_key"),
            default_model=settings.get("default_model"),
            network_policy=settings.get("network_policy"),
            memory_limit_mb=settings.get("memory_limit_mb", 512),
            log_prefix=log_prefix,
        )

    @property
    def name(self) -> str:
        return "responses-api"

    def _get_client(self):
        """Lazy-init the OpenAI client."""
        if not hasattr(self, "_client"):
            try:
                from openai import OpenAI
            except ImportError:
                raise ImportError(
                    "openai package required for responses-api runner. "
                    "Install with: pip install agent-eval-harness[openai]"
                )
            kwargs = {}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = OpenAI(**kwargs)
        return self._client

    def _upload_skill(self, client, skill_dir: Path, skill_name: str) -> str:
        """Upload a skill directory and return its skill_id.

        The cache is process-global so parallel workers (each with their
        own ``ResponsesAPIRunner`` instance) share a single upload.
        The lock is held across the check-and-create to prevent redundant
        uploads when multiple threads see a cache miss simultaneously.

        Symlinks are resolved and included when the target stays under the
        skill root (typical for symlinked skill trees from the harness).
        """
        with _global_skill_lock:
            skill_root = skill_dir.resolve()
            cache_key = (skill_name, str(skill_root))
            if cache_key in _global_skill_cache:
                return _global_skill_cache[cache_key]
            files = []
            for f in skill_root.rglob("*"):
                try:
                    resolved = f.resolve()
                    resolved.relative_to(skill_root)
                except (ValueError, OSError):
                    continue
                if not resolved.is_file():
                    continue
                rel = f.relative_to(skill_root).as_posix()
                files.append((rel, resolved.read_bytes()))
            skill = client.skills.create(files=files)
            _global_skill_cache[cache_key] = skill.id
            return skill.id

    _MEMORY_TIERS = [
        (1024, "1g"), (4096, "4g"), (16384, "16g"), (65536, "64g"),
    ]

    def _pick_memory_limit(self, mb: int) -> str:
        """Map an MB value to the nearest valid API tier."""
        for threshold, label in self._MEMORY_TIERS:
            if mb <= threshold:
                return label
        return "64g"

    def _create_container(self, client, skill_id: Optional[str]) -> str:
        """Create an isolated container for one eval case.

        If skill_id is provided (case/batch mode), attach the skill.
        If None (prompt mode), create container without skills.
        """
        kwargs = {
            "name": f"eval-{uuid.uuid4().hex[:12]}",
            "memory_limit": self._pick_memory_limit(self._memory_limit_mb),
        }
        if skill_id:
            kwargs["skills"] = [{"type": "skill_reference", "skill_id": skill_id}]
        if self._network_policy:
            kwargs["network_policy"] = self._network_policy
        container = client.containers.create(**kwargs)
        return container.id

    def _upload_workspace(self, client, container_id: str,
                          workspace: Path) -> set[str]:
        """Upload all workspace files to the container. Returns set of paths.

        Symlinked project files are common (e.g. symlinked skills under
        ``skills/``). Each path is resolved and must stay under the
        resolved workspace root (CWE-59); targets outside are skipped.
        """
        uploaded = set()
        workspace_root = workspace.resolve()
        for f in workspace_root.rglob("*"):
            try:
                resolved = f.resolve()
                resolved.relative_to(workspace_root)
            except (ValueError, OSError):
                continue
            if not resolved.is_file():
                continue
            rel = f.relative_to(workspace_root)
            container_path = f"/workspace/{rel.as_posix()}"
            content = resolved.read_bytes()
            client.containers.files.create(
                container_id,
                file=(container_path, content),
            )
            uploaded.add(container_path)
        return uploaded

    def _download_results(self, client, container_id: str,
                          workspace: Path) -> None:
        """Download new and modified files from container back to workspace.

        All files under ``/workspace/`` are synced back — both newly
        created files *and* files that existed before execution (which
        may have been edited in-place by the agent).
        """
        after_files = client.containers.files.list(container_id)
        workspace_root = workspace.resolve()
        for f in after_files:
            if not f.path.startswith("/workspace/"):
                continue
            rel = Path(f.path[len("/workspace/"):])
            if rel.is_absolute() or ".." in rel.parts:
                self._log(f"Skipping unsafe container path: {f.path}")
                continue
            local_path = (workspace_root / rel).resolve()
            try:
                local_path.relative_to(workspace_root)
            except ValueError:
                self._log(f"Skipping escaped container path: {f.path}")
                continue
            local_path.parent.mkdir(parents=True, exist_ok=True)
            resp = client.containers.files.content.retrieve(
                file_id=f.id, container_id=container_id,
            )
            local_path.write_bytes(resp.content)

    def _delete_container(self, client, container_id: str) -> None:
        """Delete a container (best-effort cleanup)."""
        try:
            client.containers.delete(container_id)
        except Exception as exc:
            self._log(f"Warning: container cleanup failed ({container_id}): {exc}")

    def _find_skill_dir(self, workspace: Path,
                        skill_name: str) -> Path:
        """Locate the skill directory relative to workspace."""
        candidates = [
            workspace / ".skills" / skill_name,
            workspace / "skills" / skill_name,
            workspace.parent / ".skills" / skill_name,
        ]
        for c in candidates:
            if c.exists() and (c / "SKILL.md").exists():
                return c.resolve()
        raise FileNotFoundError(
            f"Skill directory not found for '{skill_name}'. "
            f"Searched: {[str(c) for c in candidates]}")

    def _extract_output_text(self, response) -> str:
        """Extract text content from response output messages."""
        parts = []
        for item in (response.output or []):
            if getattr(item, "type", None) == "message":
                for block in (item.content or []):
                    if getattr(block, "type", None) == "output_text":
                        parts.append(block.text)
        return "\n".join(parts)

    def _count_turns(self, response) -> int:
        """Count the number of assistant turns in the response."""
        count = 0
        for item in (response.output or []):
            if getattr(item, "type", None) == "message":
                count += 1
        return count or 1

    def _log(self, msg: str) -> None:
        """Print a progress message if log_prefix is set."""
        if self._log_prefix:
            with _print_lock:
                print(f"  {self._log_prefix} | {msg}", flush=True)

    def execute(
        self,
        target: Optional[str],
        args: str,
        workspace: Path,
        model: str,
        settings_path: Optional[Path] = None,
        system_prompt: Optional[str] = None,
        max_budget_usd: float = 5.0,
        timeout_s: int = 600,
        extra_env: Optional[dict] = None,
    ) -> RunResult:
        client = None
        effective_model = model or self._default_model
        if not effective_model:
            raise ValueError(
                "No model specified. Set the 'model' argument, "
                "'default_model' in runner settings, or OPENAI_MODEL env var.")
        start = time.monotonic()
        container_id = None
        stdout_parts = []

        try:
            client = self._get_client()

            # Skill upload only for case/batch mode (target is skill name)
            # For prompt mode (target is None), skip skill upload
            skill_id = None
            if target:
                skill_dir = self._find_skill_dir(workspace, target)
                skill_id = self._upload_skill(client, skill_dir, target)
                self._log(f"Skill uploaded: {target} -> {skill_id}")

            container_id = self._create_container(client, skill_id)
            self._log(f"Container created: {container_id}")

            uploaded = self._upload_workspace(
                client, container_id, workspace)
            self._log(f"Uploaded {len(uploaded)} files to container")

            # Build prompt: /{skill} {args} for case/batch, or {args} for prompt mode
            if target:
                prompt = f"/{target}"
                if args:
                    prompt += f" {args}"
            else:
                prompt = args or ""

            input_messages = []
            if system_prompt:
                input_messages.append({
                    "role": "developer",
                    "content": system_prompt,
                })
            input_messages.append({
                "role": "user",
                "content": prompt,
            })

            self._log(f"Executing: {_sanitize_prompt(prompt)}")
            response = client.responses.create(
                model=effective_model,
                input=input_messages,
                tools=[{
                    "type": "shell",
                    "environment": {
                        "type": "container_reference",
                        "container_id": container_id,
                    },
                }],
                timeout=timeout_s,
            )

            output_text = self._extract_output_text(response)
            stdout_parts.append(output_text)
            self._log(f"Execution complete: {response.status}")

            self._download_results(client, container_id, workspace)

            token_usage = None
            cost_usd = None
            if response.usage:
                token_usage = {
                    "input": getattr(response.usage, "input_tokens", None)
                            or getattr(response.usage, "prompt_tokens", None),
                    "output": getattr(response.usage, "output_tokens", None)
                             or getattr(response.usage, "completion_tokens", None),
                }

            duration = time.monotonic() - start
            exit_code = 0 if response.status == "completed" else 1

            return RunResult(
                exit_code=exit_code,
                stdout="\n".join(stdout_parts),
                stderr="",
                duration_s=duration,
                token_usage=token_usage,
                cost_usd=cost_usd,
                num_turns=self._count_turns(response),
                resolved_model=getattr(response, "model", effective_model),
                raw_output={"response_id": response.id,
                            "status": response.status},
            )

        except Exception as e:
            duration = time.monotonic() - start
            return RunResult(
                exit_code=1,
                stdout="\n".join(stdout_parts),
                stderr=str(e),
                duration_s=duration,
            )
        finally:
            if container_id and client is not None:
                self._delete_container(client, container_id)
