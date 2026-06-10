"""OpenCode CLI runner implementation.

Runs skills using the OpenCode CLI (anomalyco/opencode) in non-interactive
mode via `opencode run --format json`.

OTel trace capture is supported but requires an upstream fix in OpenCode
(process.exit() kills spans before flush). Until then, usage and events
are extracted from OpenCode's JSON stdout events.
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .base import EvalRunner, RunResult

_print_lock = threading.Lock()


class OpenCodeRunner(EvalRunner):
    """Runs skills using the OpenCode CLI in non-interactive mode."""

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        return cls(
            permissions=config.permissions,
            env=config.runner.env,
            system_prompt=config.runner.system_prompt,
            otel_config=config.runner.otel,
            log_prefix=log_prefix,
            effort=overrides.get("effort", config.runner.effort),
        )

    def __init__(
        self,
        permissions: Optional[dict] = None,
        env: Optional[dict] = None,
        system_prompt: Optional[str] = None,
        otel_config=None,
        log_prefix: Optional[str] = None,
        effort: Optional[str] = None,
    ):
        self._permissions = permissions or {}
        self._env = env or {}
        self._system_prompt = system_prompt
        self._otel_config = otel_config
        self._log_prefix = log_prefix
        self._effort = effort

    @property
    def name(self) -> str:
        return "opencode"

    def setup_workspace(self, workspace, config, *, project_root=None,
                        interceptor_src=None):
        """Write opencode.json with permissions and optional OTel config."""
        config_data = {"$schema": "https://opencode.ai/config.json"}

        config_data["permission"] = {"task": "deny"}

        deny = self._permissions.get("deny", [])
        for pattern in deny:
            config_data["permission"][pattern.lower()] = "deny"

        if self._otel_config and self._otel_config.enabled:
            config_data.setdefault("experimental", {})
            config_data["experimental"]["openTelemetry"] = True

        (workspace / "opencode.json").write_text(
            json.dumps(config_data, indent=2))

    def run_skill(
        self,
        skill_name: str,
        args: str,
        workspace: Path,
        model: str,
        settings_path: Optional[Path] = None,
        system_prompt: Optional[str] = None,
        max_budget_usd: float = 5.0,
        timeout_s: int = 600,
        extra_env: Optional[dict] = None,
        output_dir: Optional[Path] = None,
    ) -> RunResult:
        receiver = None
        otel_port = None
        if self._otel_config and self._otel_config.enabled:
            from agent_eval.otel.receiver import OTLPReceiver
            otel_output = output_dir or workspace
            receiver = OTLPReceiver(output_dir=otel_output)
            otel_port = receiver.start()

        try:
            return self._run_inner(
                skill_name, args, workspace, model,
                system_prompt, max_budget_usd, timeout_s,
                otel_port,
            )
        finally:
            if receiver:
                receiver.stop(flush_timeout_s=5)

    def _run_inner(
        self,
        skill_name: str,
        args: str,
        workspace: Path,
        model: str,
        system_prompt: Optional[str],
        max_budget_usd: float,
        timeout_s: int,
        otel_port: Optional[int],
    ) -> RunResult:
        if skill_name:
            prompt = f"Use the {skill_name} skill"
            if args:
                prompt += f" with arguments: {args}"
        else:
            prompt = args or ""

        # --dangerously-skip-permissions: auto-approve tool calls that
        # aren't explicitly denied in opencode.json. Workaround for
        # OpenCode's ruleset ordering bug (#13851) where "allow" rules
        # in config get overridden by session presets for external paths.
        cmd = ["opencode", "run", "--format", "json",
               "--dangerously-skip-permissions"]

        if model:
            cmd.extend(["--model", model])

        if self._effort:
            cmd.extend(["--variant", self._effort])

        cmd.extend(["--dir", str(workspace)])
        cmd.append(prompt)

        env = self._build_env(otel_port, workspace)
        start = time.monotonic()
        deadline = start + timeout_s
        timed_out = False

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workspace),
                text=True,
                env=env,
            )

            def _watchdog():
                nonlocal timed_out
                remaining = max(0, deadline - time.monotonic())
                try:
                    proc.wait(timeout=remaining if remaining > 0 else 0.1)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    proc.kill()

            watchdog = threading.Thread(target=_watchdog, daemon=True)
            watchdog.start()

            stdout_lines = []
            for line in proc.stdout:
                line = line.rstrip("\n")
                stdout_lines.append(line)
                if self._log_prefix:
                    msg = self._extract_progress(line)
                    if msg:
                        with _print_lock:
                            print(f"  {self._log_prefix} | {msg}", flush=True)

            stderr = proc.stderr.read()
            proc.wait(timeout=5)
            if timed_out:
                raise subprocess.TimeoutExpired(cmd, timeout_s)

        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            duration = time.monotonic() - start
            return RunResult(
                exit_code=-1,
                stdout="\n".join(stdout_lines),
                stderr=f"Timed out after {timeout_s}s",
                duration_s=duration,
            )
        except Exception as e:
            duration = time.monotonic() - start
            return RunResult(
                exit_code=-1, stdout="", stderr=str(e), duration_s=duration,
            )

        duration = time.monotonic() - start
        usage = _extract_usage_from_events(stdout_lines)

        return RunResult(
            exit_code=proc.returncode,
            stdout="\n".join(stdout_lines),
            stderr=stderr or "",
            duration_s=duration,
            token_usage=usage.get("token_usage"),
            cost_usd=usage.get("cost_usd"),
            num_turns=usage.get("num_turns"),
            resolved_model=usage.get("resolved_model"),
            models_used=usage.get("models_used"),
        )

    def _build_env(self, otel_port: Optional[int], workspace: Path) -> dict:
        """Build subprocess environment: inherit env, apply runner.env, add OTel."""
        env = os.environ.copy()
        for k, v in self._env.items():
            if isinstance(v, str) and v.startswith("$"):
                resolved = os.environ.get(v[1:])
                if resolved is not None:
                    env[k] = resolved
            else:
                env[k] = str(v)

        if otel_port and self._otel_config:
            cfg = self._otel_config
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{otel_port}"
            env["OTEL_EXPORTER_OTLP_PROTOCOL"] = cfg.protocol
            env["OTEL_BSP_SCHEDULE_DELAY"] = "0"
            if cfg.resource_attributes:
                attrs = ",".join(f"{k}={v}" for k, v in cfg.resource_attributes.items())
                env["OTEL_RESOURCE_ATTRIBUTES"] = attrs

        return env

    @staticmethod
    def _extract_progress(line: str) -> str:
        """Extract progress message from OpenCode JSON output."""
        try:
            obj = json.loads(line)
            event_type = obj.get("type", "")
            if event_type == "text":
                text = obj.get("part", {}).get("text", "").strip()
                if text and len(text) < 100:
                    return text
            elif event_type == "tool_call":
                name = obj.get("name", "")
                return f"Tool: {name}" if name else ""
            elif event_type == "step_finish":
                part = obj.get("part", {})
                cost = part.get("cost") or 0
                return f"Step done (${cost:.4f})"
        except (json.JSONDecodeError, ValueError):
            pass
        return ""


def _extract_usage_from_events(stdout_lines: list) -> dict:
    """Extract token usage and cost from OpenCode JSON stdout events.

    OpenCode emits step_finish events with cost and token breakdowns::

        {"type": "step_finish", "part": {
            "cost": 0.0054,
            "tokens": {"total": 14227, "input": 2, "output": 5,
                       "reasoning": 0, "cache": {"read": 13896, "write": 324}}
        }}
    """
    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    num_turns = 0

    for line in stdout_lines:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if obj.get("type") == "step_finish":
            part = obj.get("part", {})
            cost = part.get("cost") or 0
            if cost:
                total_cost += cost

            tokens = part.get("tokens") or {}
            total_input += tokens.get("input") or 0
            total_output += tokens.get("output") or 0
            cache = tokens.get("cache") or {}
            total_cache_read += cache.get("read") or 0
            total_cache_write += cache.get("write") or 0
            num_turns += 1

    return {
        "token_usage": {
            "input": total_input,
            "output": total_output,
            "cache_read": total_cache_read,
            "cache_create": total_cache_write,
        } if num_turns else None,
        "cost_usd": total_cost or None,
        "num_turns": num_turns or None,
        "resolved_model": None,
        "models_used": None,
    }
