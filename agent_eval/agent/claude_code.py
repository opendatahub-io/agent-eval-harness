"""Claude Code CLI runner implementation."""

import json
import os
import re
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
from agent_eval.tools.permissions import compile_permission_rules
from agent_eval.config import resolve_plugin_dir

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


# Public effort vocabulary — importable by Harbor orchestration without
# reaching into a protected class attribute (mirrors CODEX_EFFORTS).
CLAUDE_CODE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


class ClaudeCodeRunner(EvalRunner):
    """Runs skills using the Claude Code CLI in non-interactive mode."""

    _VALID_EFFORTS = CLAUDE_CODE_EFFORTS
    _VALID_PERMISSION_MODES = {
        "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions",
    }

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        resolved_plugin_dirs = [
            str(resolve_plugin_dir(config, configured))
            for configured in config.runner.plugin_dirs
        ]
        return cls(
            permissions=overrides.get("permissions", config.permissions),
            plugin_dirs=resolved_plugin_dirs,
            env=config.runner.env,
            system_prompt=config.runner.system_prompt,
            subagent_model=overrides.get("subagent_model"),
            mlflow_experiment=overrides.get("mlflow_experiment"),
            mlflow_tracking_uri=overrides.get("mlflow_tracking_uri"),
            effort=overrides.get("effort", config.runner.effort),
            permission_mode=overrides.get(
                "permission_mode", config.runner.permission_mode),
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
        permission_mode: Optional[str] = None,
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
        if permission_mode is not None and (
            not isinstance(permission_mode, str)
            or permission_mode not in self._VALID_PERMISSION_MODES
        ):
            raise ValueError(
                f"Invalid permission_mode '{permission_mode}'. "
                f"Must be one of: {sorted(self._VALID_PERMISSION_MODES)}")
        self._permission_mode = permission_mode

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

        if self._permission_mode:
            cmd.extend(["--permission-mode", self._permission_mode])

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

            # Compile eval.yaml deny/allow rules into Claude Code patterns via the
            # shared compiler (gitignore-recursive paths; Bash skipped as a no-op),
            # merging with any rules already in the workspace settings (e.g. the
            # repo-write protection). See agent_eval/tools/permissions.py.
            if deny:
                existing = settings_config["permissions"].get("deny")
                merged = list(existing) if isinstance(existing, list) else []
                for pattern in compile_permission_rules(deny, harden_bash=True):
                    if pattern not in merged:
                        merged.append(pattern)
                settings_config["permissions"]["deny"] = merged

            if allow:
                existing = settings_config["permissions"].get("allow")
                merged = list(existing) if isinstance(existing, list) else []
                for pattern in compile_permission_rules(allow):
                    if pattern not in merged:
                        merged.append(pattern)
                settings_config["permissions"]["allow"] = merged

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
            result_denials = []

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
                            # A session the CLI resumes (e.g. after background
                            # task notifications) emits one result event PER
                            # segment, each carrying only that segment's
                            # denials. Keeping just the last event drops every
                            # earlier segment's list — a real run lost 7
                            # denials that way, hiding a workspace escape from
                            # run_result.json.
                            seg = obj.get("permission_denials")
                            if isinstance(seg, list):
                                result_denials.extend(seg)
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
            # An evaluator timeout can land after the CLI already emitted
            # usage data — same under-reporting risk as the bg-kill path.
            cost_usd = _billed_cost(cost_usd, per_model_usage)
            timeout_stderr = f"Timed out after {timeout_s}s"
            denial_list = _extract_denial_list(result_obj, permission_denials, result_denials)
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

        cost_usd = _billed_cost(cost_usd, per_model_usage)

        # Add subagent turns from captured transcripts, deduplicating
        # against IDs already seen in the stream (Claude Code >= 2.1.108
        # streams subagent messages in stdout too)
        subagent_turns = count_subagent_turns(workspace / "subagents", already_seen=stream_ids)
        if subagent_turns:
            num_turns = (num_turns or 0) + subagent_turns
        per_model_turns = _per_model_turns(
            workspace / "subagents", stream_ids_by_model)

        denial_list = _extract_denial_list(result_obj, permission_denials, result_denials)
        if denial_list:
            denial_msg = (f"\nWARNING: {len(denial_list)} permission "
                          f"denial(s) detected during execution")
            stderr = (stderr or "") + denial_msg

        # An unrecognised slash command is reported by the CLI as a successful
        # run. Fail the case instead of letting a never-started skill look like
        # a skill that produced nothing.
        exit_code = proc.returncode
        # The CLI kills background tasks that outlive the final turn by the
        # bg-wait ceiling (default 600s) and still exits 0 with a "success"
        # result event. For pipeline skills whose real work happens in
        # background agents, that is a dead case wearing an OK label: on a
        # real run the killed agent left half-written artifacts and the case
        # was published as "OK | exit 0". Fail it honestly.
        if _BG_KILL_RE.search(stderr or "") and exit_code == 0:
            exit_code = 1
            stderr = (stderr or "") + (
                "\nERROR: the CLI terminated still-running background tasks at "
                "the bg-wait ceiling — their work is incomplete and artifacts "
                "may be half-written. Raise CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS "
                "(0 = wait indefinitely) for long-running pipeline skills."
            )
        unknown_command = _detect_unknown_command(result_obj)
        if unknown_command and exit_code == 0:
            exit_code = 1
            stderr = (stderr or "") + (
                f"\nERROR: the agent did not recognise '{unknown_command}' "
                f"(0 turns, no work performed). The skill is not discoverable "
                f"at runtime — check the skill name, and set runner.plugin_dirs "
                f"if it is packaged as a plugin rather than living in "
                f".claude/skills."
            )

        # Clean up temporary settings file if created
        if cleanup_settings and cleanup_settings.exists():
            try:
                cleanup_settings.unlink()
            except Exception:
                pass  # Best effort cleanup

        return RunResult(
            exit_code=exit_code,
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


def _billed_cost(cost_usd, per_model_usage):
    """The larger of conversation cost and per-model billed cost.

    total_cost_usd covers the CONVERSATION; modelUsage covers every token
    billed, including a background agent killed after the final turn (or
    still running at an evaluator timeout). Normally they agree to the cent —
    when modelUsage is higher, the difference is real spend the conversation
    never saw (a real case published $0.30 while burning $1.47).
    """
    per_model_total = sum(
        (v or {}).get("cost_usd") or 0
        for v in (per_model_usage or {}).values())
    if per_model_total and per_model_total > (cost_usd or 0) + 0.01:
        return per_model_total
    return cost_usd


_UNKNOWN_COMMAND_RE = re.compile(r"^Unknown command:\s*(/\S+)")

# Emitted on stderr when the CLI gives up waiting for background tasks
# (message text as of Claude Code 2.1.x; keep the match loose).
_BG_KILL_RE = re.compile(r"Background tasks still running after .*terminating", re.S)


def _detect_unknown_command(result_obj) -> Optional[str]:
    """Return the slash command the CLI did not recognise, if that is what happened.

    Claude Code answers an unrecognised slash command with plain text and still
    reports success::

        {"type": "result", "subtype": "success", "is_error": false,
         "num_turns": 0, "total_cost_usd": 0, "result": "Unknown command: /x"}

    The process exits 0, so an eval whose skill never resolves (a plugin-packaged
    skill with no runner.plugin_dirs, a typo, a plugin that failed to load) finishes
    in seconds with every case marked OK and no artifacts — indistinguishable from a
    skill that ran and produced nothing.

    ``num_turns`` guards the match: a real run that merely quotes the phrase has
    turns, an unrecognised command never does.

    The guard suppresses only on *positive evidence of work*. A missing, null or
    non-numeric count is deliberately not read as "zero turns", but neither does
    it suppress — the leading-phrase match below is the actual signal. Demanding
    a literal integer ``0`` would trade a far-fetched false positive (a partial
    payload whose result text also begins with "Unknown command: /") for a false
    negative that silently restores the original green-but-broken run if the
    payload shape ever changes.
    """
    if not isinstance(result_obj, dict):
        return None
    turns = result_obj.get("num_turns")
    if isinstance(turns, (int, float)) and turns > 0:
        return None
    text = result_obj.get("result")
    if not isinstance(text, str):
        return None
    match = _UNKNOWN_COMMAND_RE.match(text.strip())
    return match.group(1) if match else None


def _extract_denial_list(result_obj, streaming_count, collected=None):
    """Build the permission_denials list for RunResult.

    Prefers the structured ``permission_denials`` arrays from the CLI
    ``result`` events (available since Claude Code 2.x). ``collected`` is the
    union across ALL result events of the session — a resumed session emits
    one result event per segment, each with only that segment's denials, so
    reading only the final event under-reports. Deduplicated by tool_use_id
    where present, in case a CLI version ever reports cumulatively. Falls back
    to a synthetic list derived from the streaming keyword counter when no
    result event carried denials (e.g. timeout before a result is emitted).
    """
    denials = list(collected) if collected else []
    if not denials and isinstance(result_obj, dict):
        final = result_obj.get("permission_denials")
        if isinstance(final, list):
            denials = list(final)
    if denials:
        seen = set()
        unique = []
        for d in denials:
            key = d.get("tool_use_id") if isinstance(d, dict) else None
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            unique.append(d)
        return unique
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
