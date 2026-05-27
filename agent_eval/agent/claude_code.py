"""Claude Code CLI runner implementation."""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .base import EvalRunner, RunResult
from .stream_capture import (
    make_prompt_event, inject_timestamp, extract_usage,
    count_subagent_turns, count_subagent_turns_by_model, setup_subagent_hook,
)

_print_lock = threading.Lock()


def _per_model_turns(subagent_dir, stream_ids_by_model):
    """Combine stream-level per-model turn IDs with subagent transcripts.

    Returns ``{model: turn_count}`` summing stream IDs and any new IDs found
    in subagent transcripts (deduplicated by message ID). Returns None if no
    per-model data is available, so the field stays absent rather than {}."""
    by_model = {m: set(ids) for m, ids in (stream_ids_by_model or {}).items()}
    new_per_model = count_subagent_turns_by_model(subagent_dir, by_model) or {}
    counts = {m: len(ids) for m, ids in by_model.items()}
    for m, n in new_per_model.items():
        counts[m] = counts.get(m, 0) + n
    return counts or None


class ClaudeCodeRunner(EvalRunner):
    """Runs skills using the Claude Code CLI in non-interactive mode."""

    _VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        plugin_dirs = config.runner.plugin_dirs or []
        resolved_plugin_dirs = [
            str(Path(d).resolve()) for d in plugin_dirs]
        return cls(
            permissions=config.permissions,
            plugin_dirs=resolved_plugin_dirs,
            env=config.runner.env,
            system_prompt=config.runner.system_prompt,
            subagent_model=overrides.get("subagent_model"),
            mlflow_experiment=overrides.get("mlflow_experiment"),
            mlflow_tracking_uri=overrides.get("mlflow_tracking_uri"),
            effort=overrides.get("effort", config.runner.effort),
            log_prefix=log_prefix,
        )

    def __init__(
        self,
        permissions: Optional[dict] = None,
        subagent_model: Optional[str] = None,
        plugin_dirs: Optional[list] = None,
        env: Optional[dict] = None,
        system_prompt: Optional[str] = None,
        mlflow_experiment: Optional[str] = None,
        mlflow_tracking_uri: Optional[str] = None,
        log_prefix: Optional[str] = None,
        effort: Optional[str] = None,
    ):
        self._permissions = permissions or {}
        self._subagent_model = subagent_model
        self._plugin_dirs = plugin_dirs or []
        self._env = env or {}
        self._system_prompt = system_prompt
        self._mlflow_experiment = mlflow_experiment
        self._mlflow_tracking_uri = mlflow_tracking_uri
        self._log_prefix = log_prefix
        if effort and effort not in self._VALID_EFFORTS:
            raise ValueError(
                f"Invalid effort '{effort}'. "
                f"Must be one of: {sorted(self._VALID_EFFORTS)}")
        self._effort = effort

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def version(self) -> str:
        """Get the Claude Code CLI version."""
        try:
            result = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=5)
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
        cmd = [
            "claude",
            "--print",
            "--model", model,
            "--output-format", "stream-json" if self._log_prefix else "json",
            "--max-budget-usd", str(max_budget_usd),
            # Session persistence must stay ON so subagent transcript files
            # survive long enough for the SubagentStop hook to copy them.
            # The session directory is cleaned up post-run (see below).
        ]
        if self._log_prefix:
            cmd.append("--verbose")

        if self._effort:
            cmd.extend(["--effort", self._effort])

        for plugin_dir in self._plugin_dirs:
            cmd.extend(["--plugin-dir", str(plugin_dir)])

        effective_prompt = system_prompt or self._system_prompt
        if effective_prompt:
            cmd.extend(["--append-system-prompt", effective_prompt])

        # Permissions: handle both simple and path-based formats
        # If path-based (list of dicts), create a temporary settings file
        deny = self._permissions.get("deny", [])
        allow = self._permissions.get("allow", [])

        temp_settings_file = None
        # Check if ANY element is path-based (dict), not just the first
        has_path_based = (
            any(isinstance(item, dict) for item in deny) if deny else False
        ) or (
            any(isinstance(item, dict) for item in allow) if allow else False
        )

        if has_path_based:
            # Generate temporary settings file with path-based permissions.
            # Write to the same directory as settings_path (case workspace) to avoid
            # modifying the repo when workspace is the repo root (in-repo mode).
            if settings_path and Path(settings_path).exists():
                # Write next to settings file in case workspace (case_ws/.claude/)
                temp_settings_file = Path(settings_path).parent / ".eval-permissions.json"
            else:
                # No settings file - use workspace (safe when workspace != repo root)
                temp_settings_file = workspace / ".eval-permissions.json"
            settings_config = {}

            # If there's an existing settings file, load it first
            if settings_path and Path(settings_path).exists():
                try:
                    with open(settings_path) as f:
                        settings_config = json.load(f)
                except Exception:
                    settings_config = {}

            # Ensure permissions section exists
            if "permissions" not in settings_config:
                settings_config["permissions"] = {}

            # Convert path-based deny rules to Claude Code permission patterns
            # Format: "Tool(path/pattern*)"
            # Handle mixed lists: ["Read", {"path": "eval/", "tools": ["Grep"]}]
            if deny:
                # Start with existing deny rules (preserve repo-protection rules from workspace settings)
                deny_patterns = settings_config["permissions"].get("deny", []).copy() if isinstance(settings_config["permissions"].get("deny"), list) else []
                for rule in deny:
                    if isinstance(rule, dict):
                        # Path-based rule
                        path_pattern = rule.get("path", "")
                        tools = rule.get("tools", [])
                        for tool in tools:
                            # Convert path pattern to Claude Code format
                            # "eval/" → "Tool(eval/*)"
                            # "eval.yaml" → "Tool(eval.yaml)"
                            if path_pattern.endswith("/"):
                                pattern = f"{tool}({path_pattern}*)"
                            else:
                                pattern = f"{tool}({path_pattern})"
                            deny_patterns.append(pattern)
                    else:
                        # Simple string rule - tool name only
                        deny_patterns.append(rule)
                settings_config["permissions"]["deny"] = deny_patterns

            # Convert path-based allow rules similarly
            # Handle mixed lists: ["Read", {"path": "docs/", "tools": ["Grep"]}]
            if allow:
                # Start with existing allow rules (preserve case-workspace permissions from workspace settings)
                allow_patterns = settings_config["permissions"].get("allow", []).copy() if isinstance(settings_config["permissions"].get("allow"), list) else []
                for rule in allow:
                    if isinstance(rule, dict):
                        # Path-based rule
                        path_pattern = rule.get("path", "")
                        tools = rule.get("tools", [])
                        for tool in tools:
                            if path_pattern.endswith("/"):
                                pattern = f"{tool}({path_pattern}*)"
                            else:
                                pattern = f"{tool}({path_pattern})"
                            allow_patterns.append(pattern)
                    else:
                        # Simple string rule - tool name only
                        allow_patterns.append(rule)
                settings_config["permissions"]["allow"] = allow_patterns

            # Write temporary settings file
            temp_settings_file.write_text(json.dumps(settings_config, indent=2))

            # Override with temporary settings file to ensure path-based rules apply
            settings_path = temp_settings_file
        else:
            # Simple format - use CLI flags directly
            if deny:
                cmd.extend(["--disallowed-tools", ",".join(deny)])
            if allow:
                cmd.extend(["--allowed-tools", ",".join(allow)])

        # Add --settings flag after all permission mutations
        if settings_path:
            cmd.extend(["--settings", str(settings_path)])

        # Build the prompt (passed via stdin)
        # For case/batch mode: /{skill} {args}
        # For prompt mode: {args} (direct prompt, no skill wrapper)
        if target:
            prompt = f"/{target}"
            if args:
                prompt += f" {args}"
        else:
            prompt = args or ""

        start = time.monotonic()
        stdout_lines = []
        deadline = start + timeout_s
        timed_out = False

        # Track temp settings file for cleanup
        cleanup_settings = temp_settings_file if has_path_based and temp_settings_file else None

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workspace),
                text=True,
                env=self._build_env(extra_env=extra_env),
            )

            proc.stdin.write(prompt)
            proc.stdin.close()

            # Watchdog thread: kill the process when the deadline passes.
            # The stdout readline loop blocks during extended thinking, so
            # an in-loop check never fires.  Killing the process closes
            # stdout, which unblocks the for-loop.
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

            # Inject synthetic user event for the prompt
            if self._log_prefix:
                stdout_lines.append(make_prompt_event(prompt))

            result_obj = None
            resolved_model = None
            permission_denials = 0

            for line in proc.stdout:
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout_s)
                line = line.rstrip("\n")
                if not line.strip():
                    stdout_lines.append(line)
                    continue
                if self._log_prefix:
                    try:
                        line = inject_timestamp(line)
                        obj = json.loads(line)
                        if (not resolved_model
                                and obj.get("type") == "system"
                                and obj.get("subtype") == "init"):
                            resolved_model = obj.get("model")
                        msg = _extract_progress(obj)
                        if msg:
                            if msg.startswith("PERMISSION DENIED"):
                                permission_denials += 1
                            with _print_lock:
                                print(f"  {self._log_prefix} | {msg}", flush=True)
                        if obj.get("type") == "result":
                            result_obj = obj
                    except json.JSONDecodeError:
                        pass
                stdout_lines.append(line)

            stderr = proc.stderr.read()
            proc.wait(timeout=5)
            if timed_out:
                raise subprocess.TimeoutExpired(cmd, timeout_s)

        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            duration = time.monotonic() - start
            (token_usage, cost_usd, num_turns, stream_ids, models_seen,
             per_model_usage, stream_ids_by_model) = extract_usage(stdout_lines)
            # Add subagent turns from captured transcripts, deduplicating
            # against IDs already seen in the stream
            subagent_turns = count_subagent_turns(workspace / "subagents", already_seen=stream_ids)
            if subagent_turns:
                num_turns = (num_turns or 0) + subagent_turns
            per_model_turns = _per_model_turns(
                workspace / "subagents", stream_ids_by_model)
            timeout_stderr = f"Timed out after {timeout_s}s"
            denial_list = _extract_denial_list(result_obj, permission_denials)
            if denial_list:
                timeout_stderr += (f"\nWARNING: {len(denial_list)} permission "
                                   f"denial(s) detected during execution")

            # Clean up temporary settings file if created
            if cleanup_settings and cleanup_settings.exists():
                try:
                    cleanup_settings.unlink()
                except Exception:
                    pass  # Best effort cleanup

            return RunResult(
                exit_code=-1,
                stdout="\n".join(stdout_lines),
                stderr=timeout_stderr,
                duration_s=duration,
                token_usage=token_usage,
                cost_usd=cost_usd,
                num_turns=num_turns,
                resolved_model=resolved_model,
                models_used=sorted(models_seen) if models_seen else None,
                per_model_usage=per_model_usage,
                per_model_turns=per_model_turns,
                permission_denials=denial_list,
            )
        except Exception as e:
            duration = time.monotonic() - start

            # Clean up temporary settings file if created
            if cleanup_settings and cleanup_settings.exists():
                try:
                    cleanup_settings.unlink()
                except Exception:
                    pass  # Best effort cleanup

            return RunResult(
                exit_code=-1, stdout="", stderr=str(e), duration_s=duration,
            )

        duration = time.monotonic() - start
        stdout_text = "\n".join(stdout_lines)

        # Clean up session directory now that SubagentStop hooks have fired
        # and copied transcripts.  Without this, session files accumulate
        # in ~/.claude/projects/ for every eval run.
        self._cleanup_session(workspace)

        # Extract usage from collected stream-json lines
        raw_output = result_obj
        if not result_obj and stdout_text.strip():
            try:
                result_obj = json.loads(stdout_text)
                raw_output = result_obj
            except json.JSONDecodeError:
                pass

        (token_usage, cost_usd, num_turns, stream_ids, models_seen,
         per_model_usage, stream_ids_by_model) = extract_usage(stdout_lines)
        if not cost_usd and isinstance(result_obj, dict):
            cost_usd = result_obj.get("total_cost_usd")

        # Add subagent turns from captured transcripts, deduplicating
        # against IDs already seen in the stream (Claude Code >= 2.1.108
        # streams subagent messages in stdout too)
        subagent_turns = count_subagent_turns(workspace / "subagents", already_seen=stream_ids)
        if subagent_turns:
            num_turns = (num_turns or 0) + subagent_turns
        per_model_turns = _per_model_turns(
            workspace / "subagents", stream_ids_by_model)

        denial_list = _extract_denial_list(result_obj, permission_denials)
        if denial_list:
            denial_msg = (f"\nWARNING: {len(denial_list)} permission "
                          f"denial(s) detected during execution")
            stderr = (stderr or "") + denial_msg

        # Clean up temporary settings file if created
        if cleanup_settings and cleanup_settings.exists():
            try:
                cleanup_settings.unlink()
            except Exception:
                pass  # Best effort cleanup

        return RunResult(
            exit_code=proc.returncode,
            stdout=stdout_text,
            stderr=stderr or "",
            duration_s=duration,
            token_usage=token_usage,
            cost_usd=cost_usd,
            num_turns=num_turns,
            resolved_model=resolved_model,
            models_used=sorted(models_seen) if models_seen else None,
            per_model_usage=per_model_usage,
            per_model_turns=per_model_turns,
            permission_denials=denial_list,
            raw_output=raw_output,
        )

    @staticmethod
    def _cleanup_session(workspace: Path) -> None:
        """Remove the Claude Code session directory for a workspace.

        Claude Code stores sessions under ~/.claude/projects/<encoded-path>/.
        The path encoding replaces '/' with '-' and prepends '-'.
        """
        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.exists():
            return
        encoded = "-" + str(workspace).replace("/", "-")
        session_dir = projects_dir / encoded
        if session_dir.exists() and session_dir.is_dir():
            shutil.rmtree(session_dir, ignore_errors=True)

    # Environment keys safe to forward to evaluated skills
    _SAFE_ENV_KEYS = {
        "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID",
        "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLOUD_ML_REGION", "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_SUBAGENT_MODEL",
        "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
        "CLOUDSDK_CONFIG", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
        "MLFLOW_TRACKING_URI", "MLFLOW_EXPERIMENT_NAME",
        "AGENT_EVAL_RUNS_DIR",
    }

    def _build_env(self, extra_env=None):
        """Build subprocess environment with allowlisted keys only."""
        env = {k: v for k, v in os.environ.items() if k in self._SAFE_ENV_KEYS}
        for k, v in self._env.items():
            if v is None:
                continue
            if isinstance(v, str) and v.startswith("$"):
                resolved = os.environ.get(v[1:])
                if resolved is not None:
                    env[k] = resolved
            else:
                env[k] = str(v)
        if extra_env:
            for k, v in extra_env.items():
                env[k] = str(v)
        if self._subagent_model:
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = self._subagent_model
        if self._mlflow_experiment:
            env["MLFLOW_EXPERIMENT_NAME"] = self._mlflow_experiment
        if self._mlflow_tracking_uri:
            env["MLFLOW_TRACKING_URI"] = self._mlflow_tracking_uri
        return env


def _extract_denial_list(result_obj, streaming_count):
    """Build the permission_denials list for RunResult.

    Prefers the structured ``permission_denials`` array from the CLI
    ``result`` event (available since Claude Code 2.x).  Falls back to
    a synthetic list derived from the streaming keyword counter when the
    result event is absent (e.g. timeout before result is emitted).
    """
    if isinstance(result_obj, dict):
        denials = result_obj.get("permission_denials")
        if isinstance(denials, list) and denials:
            return denials
    if streaming_count:
        return [{"tool_name": "unknown"}] * streaming_count
    return None


def _sanitize_for_log(text: str, max_len: int = 80) -> str:
    """Strip newlines and non-printable characters from text for safe logging."""
    text = text.replace("\r", " ").replace("\n", " ")
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:max_len]


def _is_permission_denial(text: str) -> bool:
    """Check if a tool_result error text indicates a permission denial."""
    lower = text.lower()
    return any(phrase in lower for phrase in (
        "permission denied", "not allowed", "disallowed",
        "not permitted", "user denied",
    ))


def _extract_progress(obj: dict) -> str:
    """Extract a human-readable progress message from a stream-json event."""
    t = obj.get("type")

    if t == "user":
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    c = block.get("content", "")
                    if isinstance(c, str):
                        text = c
                    elif isinstance(c, list):
                        text = " ".join(
                            x.get("text", "") for x in c if isinstance(x, dict))
                    else:
                        text = ""
                    if text and _is_permission_denial(text):
                        return f"PERMISSION DENIED: {_sanitize_for_log(text)}"
        return ""

    elif t == "assistant":
        # Skip foreground subagent messages to avoid duplicate progress lines
        if obj.get("parent_tool_use_id"):
            return ""
        msg = obj.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "tool_use":
                tool = block.get("name", "")
                inp = block.get("input", {})
                if tool == "Skill":
                    return f"Invoking /{inp.get('skill', '?')}"
                elif tool == "Bash":
                    cmd = inp.get("command", "")[:60]
                    return f"Running: {cmd}"
                elif tool in ("Write", "Edit"):
                    path = inp.get("file_path", "")
                    return f"{tool}: {path.split('/')[-1] if path else '?'}"
                elif tool == "Read":
                    path = inp.get("file_path", "")
                    return f"Reading: {path.split('/')[-1] if path else '?'}"
                else:
                    return f"Tool: {tool}"
            elif block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and len(text) < 100:
                    return text
    elif t == "result":
        cost = obj.get("total_cost_usd", 0)
        turns = obj.get("num_turns", 0)
        return f"Done ({turns} turns, ${cost:.2f})"

    return ""
