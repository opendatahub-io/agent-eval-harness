"""OpenAI Codex CLI runner implementation."""

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .base import EvalRunner, RunResult

_print_lock = threading.Lock()


class CodexRunner(EvalRunner):
    """Run a skill or prompt with ``codex exec --json``."""

    _VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        plugin_dirs = [str(Path(d).resolve()) for d in config.runner.plugin_dirs]
        return cls(
            env=config.runner.env,
            log_prefix=log_prefix,
            config_overrides=config.runner.settings,
            plugin_dirs=plugin_dirs,
            system_prompt=config.runner.system_prompt,
            effort=overrides.get("effort", config.runner.effort),
        )

    def __init__(
        self,
        env: Optional[dict] = None,
        log_prefix: Optional[str] = None,
        config_overrides: Optional[dict] = None,
        plugin_dirs: Optional[list] = None,
        system_prompt: Optional[str] = None,
        effort: Optional[str] = None,
    ):
        self._env = env or {}
        self._log_prefix = log_prefix
        self._config_overrides = dict(config_overrides or {})
        self._plugin_dirs = [Path(p) for p in (plugin_dirs or [])]
        self._system_prompt = system_prompt

        # Keep compatibility with early Codex configs that put the effort in
        # runner.settings while making runner.effort the canonical field.
        settings_effort = self._config_overrides.get("model_reasoning_effort")
        self._effort = effort or settings_effort
        if self._effort and self._effort not in self._VALID_EFFORTS:
            raise ValueError(
                f"Invalid effort '{self._effort}'. "
                f"Must be one of: {sorted(self._VALID_EFFORTS)}")
        if self._effort:
            self._config_overrides["model_reasoning_effort"] = self._effort

    @property
    def name(self) -> str:
        return "codex"

    @property
    def version(self) -> str:
        try:
            result = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, timeout=5)
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

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
        del settings_path, max_budget_usd  # Codex has no equivalent CLI flags.
        workspace = workspace.resolve()
        self._stage_skills(workspace)

        if target:
            skill_name = target.rsplit(":", 1)[-1]
            prompt = f"Use the {skill_name} skill"
            if args:
                prompt += f" with arguments: {args}"
        else:
            prompt = args or ""

        effective_system_prompt = system_prompt or self._system_prompt
        if effective_system_prompt:
            prompt = f"{effective_system_prompt.rstrip()}\n\n{prompt}"

        cmd = [
            "codex", "exec", "--json", "--ephemeral",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check", "-C", str(workspace),
        ]
        if model:
            cmd.extend(["--model", model])
        for key, value in self._config_overrides.items():
            cmd.extend(["-c", f"{key}={json.dumps(value)}"])
        cmd.extend(["--", prompt])

        start = time.monotonic()
        events: list[dict] = []
        stdout_lines: list[str] = []
        try:
            popen_kwargs = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workspace),
                text=True,
                env=self._build_env(extra_env),
            )
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **popen_kwargs)
            try:
                stdout, stderr = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                stdout, stderr = proc.communicate()
                return RunResult(
                    exit_code=-1,
                    stdout=stdout or "",
                    stderr=f"Timed out after {timeout_s}s",
                    duration_s=time.monotonic() - start,
                    resolved_model=model or None,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            return RunResult(
                exit_code=-1, stdout="", stderr=str(exc),
                duration_s=time.monotonic() - start,
                resolved_model=model or None,
            )

        for line in (stdout or "").splitlines():
            stdout_lines.append(line)
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            events.append(event)
            if self._log_prefix:
                progress = self._extract_progress(event)
                if progress:
                    with _print_lock:
                        print(f"  {self._log_prefix} | {progress}", flush=True)

        usage = _extract_usage(events)
        return RunResult(
            exit_code=proc.returncode,
            stdout="\n".join(stdout_lines) + ("\n" if stdout_lines else ""),
            stderr=stderr or "",
            duration_s=time.monotonic() - start,
            token_usage=usage["token_usage"],
            num_turns=usage["num_turns"],
            resolved_model=model or None,
            models_used=[model] if model else None,
            raw_output={"events": events},
        )

    def _stage_skills(self, workspace: Path) -> None:
        """Expose every plugin skill, including transitive dependencies."""
        skills_dest = workspace / ".agents" / "skills"
        for plugin_dir in self._plugin_dirs:
            source_root = plugin_dir / "skills"
            if not source_root.is_dir():
                raise FileNotFoundError(
                    f"Codex plugin skill directory not found: {source_root}")
            skills_dest.mkdir(parents=True, exist_ok=True)
            for source in source_root.iterdir():
                if not source.is_dir() or not (source / "SKILL.md").is_file():
                    continue
                destination = skills_dest / source.name
                if destination.exists() or destination.is_symlink():
                    continue
                try:
                    destination.symlink_to(source.resolve(), target_is_directory=True)
                except OSError:
                    shutil.copytree(source, destination)

    def _build_env(self, extra_env: Optional[dict] = None) -> dict:
        env = os.environ.copy()
        for key, value in self._env.items():
            if isinstance(value, str) and value.startswith("$"):
                resolved = os.environ.get(value[1:])
                if resolved is not None:
                    env[key] = resolved
            else:
                env[key] = str(value)
        if extra_env:
            env.update({key: str(value) for key, value in extra_env.items()})
        return env

    @staticmethod
    def _extract_progress(event: dict) -> str:
        event_type = event.get("type")
        item = event.get("item") or {}
        if event_type in {"item.started", "item.completed"}:
            if item.get("type") == "command_execution":
                command = item.get("command", "")
                return f"Shell: {command[:100]}" if command else ""
            if event_type == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text", "").strip()
                return text[:100] if text else ""
        if event_type in {"turn.completed", "turn_completed"}:
            return "Turn complete"
        return ""


def _extract_usage(events: list[dict]) -> dict:
    """Aggregate Codex ``turn.completed`` usage records."""
    input_tokens = output_tokens = cache_read = turns = 0
    for event in events:
        if event.get("type") not in {"turn.completed", "turn_completed"}:
            continue
        usage = event.get("usage") or {}
        input_tokens += usage.get("input_tokens") or 0
        output_tokens += usage.get("output_tokens") or 0
        cache_read += usage.get("cached_input_tokens") or 0
        turns += 1
    return {
        "token_usage": {
            "input": input_tokens,
            "output": output_tokens,
            "cache_read": cache_read,
        } if turns else None,
        "num_turns": turns or None,
    }
