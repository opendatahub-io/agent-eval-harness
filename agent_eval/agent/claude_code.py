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
from agent_eval.config import resolve_plugin_dir, resolve_plugin_skill_roots

_print_lock = threading.Lock()

# Conventional directories plugin discovery reads besides the manifest and
# the manifest-declared skill roots. Copied only when present.
# scripts/ is included because skills commonly invoke
# ${CLAUDE_PLUGIN_ROOT}/scripts/... or ${CLAUDE_SKILL_DIR}/../../scripts/...
# at runtime; a staged copy without it would break those plugins.
_PLUGIN_OPTIONAL_DIRS = ("commands", "agents", "hooks", "scripts")

# Bulk plugin discovery never reads — keeps the staged copy small.
_PLUGIN_IGNORE = shutil.ignore_patterns(".git", "node_modules", "__pycache__")


def _plugin_ignore(plugin: Path):
    """copytree ignore callback: bulk dirs, plus any symlink whose resolved
    target escapes the plugin. ``symlinks=False`` MATERIALIZES link targets,
    so a third-party plugin could otherwise plant a link to a host file
    (credentials, source data) and have staging copy it into the workspace
    where the agent can read it (CWE-59 -> CWE-200).
    """
    def ignore(src, names):
        ignored = set(_PLUGIN_IGNORE(src, names))
        for name in names:
            if name in ignored:
                continue
            entry = Path(src) / name
            if entry.is_symlink():
                try:
                    resolved = entry.resolve(strict=True)
                except (OSError, RuntimeError):
                    # Dangling or looping (loops raise RuntimeError on
                    # Python 3.11/3.12, OSError after) — not stageable.
                    ignored.add(name)
                    continue
                if not resolved.is_relative_to(plugin):
                    ignored.add(name)
        return ignored
    return ignore


def stage_plugin_dir(plugin_dir: Path, workspace: Path) -> Path:
    """Copy one plugin's discoverable content into the case workspace.

    WHY: ``--plugin-dir <path>`` lands verbatim in the session's system
    context — the stream-json init event registers the plugin under that
    path. When the configured path points outside the workspace (typically
    at the project repo under evaluation), the agent can follow it and
    read or write the real project: ``additionalDirectories`` gates the
    file tools, but Bash is not path-scoped, so a disclosed path is a
    standing escape vector out of the isolated workspace. Staging the
    plugin inside the throwaway workspace and passing THAT path keeps the
    real location out of the session entirely.

    Copies only what plugin discovery and execution need: the
    ``.claude-plugin/`` manifest, every skill root declared by the
    manifest (via ``resolve_plugin_skill_roots``, which validates
    containment), and the conventional ``_PLUGIN_OPTIONAL_DIRS`` when they
    exist at the plugin root. Symlinks are not reproduced — in-plugin
    targets are copied as content, dangling ones are skipped, and links
    escaping the plugin are refused — so the staged tree cannot point back
    outside the workspace. Idempotent per workspace: an existing
    destination is reused; a partial copy never becomes the destination
    (copy into a temp sibling, then rename).
    """
    plugin = Path(plugin_dir).resolve()
    dest = workspace / ".staged-plugins" / plugin.name
    if dest.exists():
        return dest

    sources = [plugin / ".claude-plugin"]
    # A Claude plugin may legitimately export no skills (commands, agents or
    # hooks only). Tolerate exactly that layout — no 'skills' declaration in
    # the manifest AND no conventional skills/ directory — and let every
    # other error from resolve_plugin_skill_roots propagate: a malformed
    # manifest or a declared-but-missing root staged "successfully" would
    # only resurface later as an undiscoverable slash command, the silent
    # failure mode this staging exists to prevent.
    declares_skills = False
    manifest_path = plugin / ".claude-plugin" / "plugin.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # Malformed manifest: let the authoritative resolver raise its
            # own, clearer error below.
            manifest = None
            declares_skills = True
        if isinstance(manifest, dict) and manifest.get("skills") is not None:
            declares_skills = True
    if declares_skills or (plugin / "skills").is_dir():
        sources.extend(resolve_plugin_skill_roots(plugin))
    sources.extend(plugin / name for name in _PLUGIN_OPTIONAL_DIRS)

    staging = dest.parent / f".{plugin.name}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for source in sources:
            if not source.is_dir():
                continue
            # copytree with symlinks=False follows a source dir that is
            # ITSELF a symlink, and the ignore callback only sees entries
            # inside walked directories — an escaping link at the copy root
            # (e.g. scripts -> ~/.secrets) would be materialized wholesale.
            # Apply the same containment rule to the roots.
            try:
                resolved = source.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved.is_relative_to(plugin):
                continue
            shutil.copytree(
                source, staging / source.relative_to(plugin),
                symlinks=False, ignore=_plugin_ignore(plugin),
                ignore_dangling_symlinks=True, dirs_exist_ok=True)
        try:
            staging.replace(dest)
        except OSError:
            # Another stager won the rename race; its complete copy stands.
            if dest.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
                return dest
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


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
            workspace_mode=config.runner.workspace_mode,
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
        workspace_mode: Optional[str] = None,
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
        self._workspace_mode = workspace_mode
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

        plugin_dirs = self._plugin_dirs
        if plugin_dirs:
            # Always pass a workspace-local copy so the real plugin path never
            # enters the session context (see stage_plugin_dir). A plugin that
            # already lives inside the workspace is passed through unchanged,
            # and workspace_mode: repo skips staging entirely — the workspace
            # IS the project there, so there is nothing to isolate and staging
            # would write junk into the user's repo.
            try:
                plugin_dirs = self._staged_plugin_dirs(workspace)
            except (OSError, ValueError, FileNotFoundError) as e:
                return RunResult(
                    exit_code=-1, stdout="",
                    stderr=f"Plugin staging failed: {e}", duration_s=0.0,
                )
        for plugin_dir in plugin_dirs:
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

    def _staged_plugin_dirs(self, workspace: Path) -> list:
        """Stage every configured plugin into the workspace; return the copies.

        The staged path is keyed by the plugin's directory name, so two
        different plugins sharing a basename would silently collapse into
        one copy — fail loud instead.
        """
        # workspace_mode: repo runs in the user's real repository: there is
        # no isolation boundary for staging to defend (the session already
        # has the project), and staging an external plugin would write
        # .staged-plugins/ into the repo — polluting it and reading back as
        # a spurious repo modification. Pass every configured path through.
        if self._workspace_mode == "repo":
            return [str(Path(p).resolve()) for p in self._plugin_dirs]
        staged = []
        seen: dict = {}
        ws = Path(workspace).resolve()
        for configured in self._plugin_dirs:
            plugin = Path(configured).resolve()
            # A plugin already inside the workspace is passed through: its
            # path discloses nothing outside the sandbox, and re-staging it
            # would be pointless.
            if plugin == ws or plugin.is_relative_to(ws):
                staged.append(str(plugin))
                continue
            previous = seen.setdefault(plugin.name, plugin)
            if previous != plugin:
                raise ValueError(
                    "plugin staging cannot stage two different plugins with "
                    f"the same directory name: {previous} and {plugin}")
            staged.append(str(stage_plugin_dir(plugin, workspace)))
        return staged

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


_UNKNOWN_COMMAND_RE = re.compile(r"^Unknown command:\s*(/\S+)")


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
