#!/usr/bin/env python3
"""Execute a skill headlessly via the configured agent runner.

Delegates to the agent_eval.agent abstraction so the same script works
with Claude Code, OpenCode, Agent SDK, or any other registered runner.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/execute.py \\
        --workspace /tmp/agent-eval/test-001 \\
        --skill rfe.speedrun \\
        --skill-args "--input batch.yaml --headless --dry-run" \\
        --model opus \\
        --output eval/runs/test-001 \\
        [--agent claude-code] \\
        [--subagent-model sonnet] \\
        [--max-budget 100] \\
        [--timeout 3600] \\
        [--mlflow-experiment my-eval]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from agent_eval.agent import RUNNERS
from agent_eval.hooks import (
    HookError, build_hook_env, collect_hook_outputs,
    run_hooks, run_hooks_safe, save_hook_data,
)

GIT_BIN = shutil.which("git")

_HARNESS_SYSTEM_PROMPT = (
    "You are running inside an evaluation harness. Tool interception hooks "
    "and permission controls are in place for safety. You MUST NOT: "
    "rename, copy, or move scripts to bypass tool filters; "
    "modify .claude/settings.json or hook scripts; "
    "disable or work around any harness controls; "
    "use alternative paths or commands to avoid blocked operations. "
    "If a tool call is blocked, report it and continue with the remaining "
    "pipeline steps. Do not attempt workarounds."
)


class _Tee:
    """Mirror writes to a real stream and a log file, flushing both.

    Installed on ``sys.stdout``/``sys.stderr`` so execute.py populates the
    background-command output stream (the real stdout/stderr that Claude
    Code's viewer displays) AND a stable ``<output_dir>/console.log`` for
    tailing. This removes any reason to redirect with ``>`` (which would
    divert the real stream and leave the background viewer blank).
    """

    def __init__(self, stream, logfile):
        self._stream = stream
        self._logfile = logfile

    def write(self, data):
        n = self._stream.write(data)
        try:
            self._stream.flush()
        except Exception:
            pass
        try:
            self._logfile.write(data)
            self._logfile.flush()
        except Exception:
            pass  # logging is best-effort; never break the run
        return n

    def flush(self):
        for s in (self._stream, self._logfile):
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._stream.fileno()

    def __getattr__(self, name):
        # Delegate everything else (encoding, writable, buffer, …) to the
        # real stream so the Tee is a faithful stand-in.
        return getattr(self._stream, name)


def _fd_path(fd):
    """Best-effort absolute path backing a file descriptor, or None."""
    try:
        if sys.platform == "darwin":
            import fcntl
            F_GETPATH = 50  # macOS fcntl command: resolve an fd to its path
            raw = fcntl.fcntl(fd, F_GETPATH, b"\0" * 1024)
            return os.fsdecode(raw.split(b"\0", 1)[0])
        return os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, ValueError, ImportError):
        return None


def _setup_console_log(output_dir):
    """Mirror console output to ``<output_dir>/console.log`` and warn on a
    stdout redirect into the run directory.

    Claude Code's background-command viewer displays the launched process's
    own stdout/stderr stream. Redirecting that stream to a file
    (``python3 execute.py … > run/execute.log 2>&1``) leaves the viewer
    blank. Teeing here gives a stable, tailable log without any redirect;
    the guard flags the specific footgun of redirecting into the run dir
    (the harness's own capture file lives in a separate temp tasks dir, so
    this never false-fires on a correct background launch).

    Returns the console.log Path, or None if it could not be opened.
    """
    console_log = output_dir / "console.log"
    try:
        fh = open(console_log, "w", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return None
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    import atexit
    atexit.register(fh.flush)

    fd1 = _fd_path(1)
    if fd1:
        try:
            p = Path(fd1).resolve()
            out = output_dir.resolve()
            if out == p or out in p.parents:
                print(
                    f"WARNING: execute.py stdout is redirected to {p}, inside the run "
                    f"directory. Claude Code's background-command viewer shows the "
                    f"process's own stdout stream and will appear BLANK. Re-launch "
                    f"WITHOUT a '>' redirect (see eval-run SKILL.md Step 4); the live "
                    f"console is already mirrored to {console_log}.",
                    file=sys.stderr,
                )
        except OSError:
            pass
    return console_log


def _resolve_permissions(config):
    """Return the permission rules to pass to the runner.

    User-authored ``permissions`` (allow AND deny) are honored in every
    execution mode. Deny rules meant to protect an in-repo checkout simply
    do not match isolated /tmp workspaces, so there is no need to strip them
    — silently dropping deny rules disabled protections the eval author had
    explicitly configured (e.g. ``deny: ["WebFetch"]`` in batch/case mode).

    Args:
        config: EvalConfig with permissions

    Returns:
        Permissions dict safe to pass to the runner (a shallow copy)
    """
    filtered = dict(config.permissions) if config.permissions else {}

    return filtered


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--skill", default=None,
                        help="Skill name for case/batch mode (required for case/batch, omit for prompt mode)")
    parser.add_argument("--skill-args", default=None,
                        help="Skill arguments (default: from eval.yaml execution.arguments)")
    parser.add_argument("--model", default=None,
                        help="Skill model (default: from eval.yaml models.skill)")
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", required=True,
                        help="Path to eval.yaml")
    parser.add_argument("--agent", default=None,
                        help="Agent runner override (default: from runner.type)")
    parser.add_argument("--subagent-model", default=None)
    parser.add_argument("--max-budget", type=float, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--mlflow-experiment", default=None)
    parser.add_argument("--effort", default=None,
                        choices=["minimal", "low", "medium", "high", "xhigh", "max"],
                        help="Agent reasoning effort (validated for the selected runner; "
                             "default: eval.yaml runner.effort)")
    parser.add_argument("--parallelism", type=int, default=None,
                        help="Max parallel case executions (default: from eval.yaml or sequential)")
    parser.add_argument("--run-id", default=None,
                        help="Run identifier (for hook env vars and log paths)")
    parser.add_argument("--input-override", action="append", default=None,
                        metavar="KEY=VALUE",
                        help="Merge KEY=VALUE into every case's input.yaml before "
                             "execution (repeatable). Lets a caller inject run-level "
                             "values — e.g. matrix factor levels — so they resolve as "
                             "{KEY} in a cli runner command or {{ input.KEY }} in "
                             "arguments. Case fields win only if not overridden.")
    args = parser.parse_args()

    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml(args.config)

    # Determine if prompt mode (execution.prompt is set)
    is_prompt_mode = config.is_prompt_mode()
    # Multi-step: execution.steps drives per-step targets; no single skill.
    is_multi_step = bool(config.execution.steps)

    # Resolve target (skill name or None for prompt/multi-step mode)
    # Priority: CLI --skill > config (execution.skill → top-level skill)
    target = args.skill or config.resolve_skill()

    # For prompt or multi-step mode, force target=None (the executor resolves
    # per-step targets); otherwise a skill is required.
    if is_prompt_mode or is_multi_step:
        target = None
    elif not target:
        # Not prompt mode and no skill specified
        print("ERROR: skill required when execution.prompt is not set. "
              "Set --skill, execution.skill, or execution.prompt in eval.yaml.",
              file=sys.stderr)
        sys.exit(1)

    # Resolve model: CLI > config; required to be set somewhere.
    model = args.model or config.models.skill
    if not model:
        print("ERROR: no model specified. Set --model or models.skill in eval.yaml.",
              file=sys.stderr)
        sys.exit(1)
    subagent_model = args.subagent_model or config.models.subagent or model

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Mirror all console output to <output_dir>/console.log so callers never
    # need to redirect stdout with '>' (which blanks the background-command
    # viewer). The real stdout/stderr streams still receive everything.
    _setup_console_log(output_dir)

    # Resolve skill args: CLI override > config > empty
    # Treat empty/whitespace-only strings as unset (normalize before fallback)
    # For prompt mode, use execution.prompt; for skill mode, use execution.arguments
    if is_multi_step:
        skill_args = ""  # per-step arguments are resolved in the step loop
    elif is_prompt_mode:
        skill_args = (args.skill_args or "").strip() or config.execution.prompt
    else:
        skill_args = (args.skill_args or "").strip() or config.execution.arguments

    # Resolve {prompt} placeholder from batch.yaml
    if skill_args and "{prompt}" in skill_args:
        batch_path = Path(args.workspace) / "batch.yaml"
        if batch_path.exists():
            import yaml as _yaml
            with open(batch_path) as _f:
                batch = _yaml.safe_load(_f) or []
            if isinstance(batch, list) and batch:
                entry = batch[0]
                prompt_text = entry.get("prompt", "") if isinstance(entry, dict) else str(entry)
                skill_args = skill_args.replace("{prompt}", prompt_text.strip())

    # Build runner
    agent = args.agent or config.runner.type
    if agent not in RUNNERS:
        print(f"ERROR: unknown runner '{agent}'. Available: {list(RUNNERS.keys())}",
              file=sys.stderr)
        sys.exit(1)
    if agent == "codex":
        # The config loader enforces these for a declared codex runner;
        # an --agent override must not slip past the same guarantees.
        if config.inputs.tools:
            print("ERROR: runner 'codex' does not support inputs.tools "
                  "interception; use claude-code or remove the tool "
                  "interceptors", file=sys.stderr)
            sys.exit(1)
        if getattr(config.runner, "workspace_mode", None) == "repo":
            print("ERROR: runner 'codex' does not support workspace_mode: "
                  "repo because repository answer-key protections cannot "
                  "be enforced", file=sys.stderr)
            sys.exit(1)
    runner_cls = RUNNERS[agent]

    mlflow_experiment = args.mlflow_experiment or config.mlflow.experiment
    effort = args.effort or config.runner.effort

    # Honor user-authored permissions (allow + deny) in every mode.
    filtered_permissions = _resolve_permissions(config)

    runner = runner_cls.from_config(
        config,
        log_prefix="eval",
        subagent_model=subagent_model,
        mlflow_experiment=mlflow_experiment,
        mlflow_tracking_uri=config.mlflow.tracking_uri,
        effort=effort,
        permissions=filtered_permissions,
    )

    # Resolve timeout and budget: CLI override > config > defaults.
    # Use explicit None checks so that 0 is preserved (an operator who
    # passes --timeout 0 or sets max_budget_usd: 0 in the config gets
    # exactly that, not the default).
    timeout_s = (args.timeout if args.timeout is not None
                 else config.execution.timeout if config.execution.timeout is not None
                 else 3600)
    max_budget = (args.max_budget if args.max_budget is not None
                  else config.execution.max_budget_usd if config.execution.max_budget_usd is not None
                  else 100.0)

    # Compose system prompt: runner.system_prompt (if any) + harness prompt.
    # Skip the harness safety prompt for the opaque CLI runner — it references
    # tool interception hooks and permission controls that don't exist there.
    existing_prompt = (config.runner.system_prompt or "").strip()
    if agent == "cli":
        system_prompt = existing_prompt or None
    else:
        system_prompt = "\n\n".join(p for p in [existing_prompt, _HARNESS_SYSTEM_PROMPT] if p)

    # Capture user-facing eval parameters that defined this run, for the report.
    eval_params = _build_eval_params(args, config, skill_args, max_budget, timeout_s, effort)

    # ── Per-case execution (case mode) ────────────────
    # Case mode executes once per case with separate workspaces
    # (Works for both skill and prompt execution)
    if config.execution.mode == "case":
        parallelism = (args.parallelism if args.parallelism is not None
                       else config.execution.parallelism)
        _execute_per_case(args, config, runner, runner_cls,
                          output_dir, max_budget, timeout_s,
                          model, mlflow_experiment, system_prompt,
                          skill_args_template=skill_args,
                          eval_params=eval_params,
                          parallelism=parallelism,
                          effort=effort,
                          subagent_model=subagent_model,
                          target=target)
        return

    # ── Batch execution (below) ──────────────────────────────────────
    exec_label = f"/{target} {skill_args}" if target else skill_args
    print(f"Executing: {exec_label}", file=sys.stderr)
    print(f"Agent: {runner.name} | Model: {model}", file=sys.stderr)
    print(f"Workspace: {args.workspace}", file=sys.stderr)

    # Build hook environment for batch mode
    run_id = args.run_id or ""
    hook_env = build_hook_env(
        workspace=args.workspace,
        run_id=run_id,
        config_path=str(Path(args.config).resolve()),
        project_root=str(Path.cwd()),
        model=model,
    )
    log_dir = output_dir / "hooks"

    try:
        # Run before_all hooks and collect outputs
        global_hook_outputs = {}
        if config.hooks.before_all:
            print("Running before_all hooks...", file=sys.stderr)
            run_hooks(config.hooks.before_all, env=hook_env,
                      cwd=Path.cwd(), log_dir=log_dir,
                      phase_name="before_all")
            global_hook_outputs = collect_hook_outputs(Path(args.workspace))

        save_hook_data(output_dir, global_hook_outputs.get("data"))

        # Set MLflow environment in the workspace settings
        if mlflow_experiment:
            from agent_eval.mlflow.experiment import inject_tracing_env
            inject_tracing_env(args.workspace, project_root=Path.cwd(),
                               tracking_uri=config.mlflow.tracking_uri,
                               experiment_name=mlflow_experiment)

        workspace_settings = Path(args.workspace) / ".claude" / "settings.json"
        settings_path = workspace_settings if workspace_settings.exists() else None

        result = runner.execute(
            target=target,
            args=skill_args,
            workspace=Path(args.workspace),
            model=model,
            settings_path=settings_path,
            system_prompt=system_prompt,
            max_budget_usd=max_budget,
            timeout_s=timeout_s,
            extra_env=global_hook_outputs.get("env") or None,
        )

        _save_result(result, args, output_dir, runner, model, eval_params=eval_params)

        # Copy batch input files to output dir for MLflow artifact logging.
        _copy_input_files_batch(Path(args.workspace), output_dir)

        sys.exit(result.exit_code)
    finally:
        if config.hooks.after_all:
            print("Running after_all hooks...", file=sys.stderr)
            run_hooks_safe(config.hooks.after_all, env=hook_env,
                           cwd=Path.cwd(), log_dir=log_dir,
                           phase_name="after_all")


def _resolve_arguments(template, case_data, steps=None):
    """Resolve {field} or {{ field }} placeholders from case input data.

    ``steps`` (optional) binds the ``{{ steps.<id>.* }}`` namespace for
    multi-step execution — the accumulated results of earlier steps in the
    same case (Jinja2 syntax only).

    Supports two template syntaxes:
    - Simple: {field} or {field?} for optional fields
    - Jinja2: {{ input.field }} with full Jinja2 expressions

    The syntax is auto-detected: if template contains '{{', uses Jinja2;
    otherwise uses simple regex substitution.

    Args:
        template: String with placeholders
        case_data: Dict from input.yaml with field values

    Returns:
        String with all placeholders replaced

    Raises:
        ValueError: If required fields are missing
    """
    import re

    # Auto-detect Jinja2 syntax
    if '{{' in template or '{%' in template:
        # Use Jinja2 rendering
        try:
            from jinja2 import StrictUndefined, Template, UndefinedError
        except ImportError:
            raise ImportError(
                "Jinja2 is required for {{ }} template syntax. "
                "Install with: pip install jinja2"
            )

        try:
            # StrictUndefined makes missing required fields raise rather than
            # render to an empty string (honoring the docstring contract).
            # Genuinely optional fields should use {{ input.get('x', '') }}
            # or the `| default('')` filter.
            jinja_template = Template(template, undefined=StrictUndefined)
            # Render with input.* (and steps.* for multi-step) namespaces.
            result = jinja_template.render(input=case_data, steps=steps or {})
            return result.strip()
        except UndefinedError as e:
            raise ValueError(
                f"Undefined variable in Jinja2 template: {e}. "
                f"Template: {template}"
            )

    # Fall back to simple {field} regex substitution
    missing = []

    def _replace(m):
        field = m.group(1)
        optional = field.endswith("?")
        if optional:
            field = field[:-1]
        if field not in case_data:
            if optional:
                return ""
            missing.append(field)
            return ""
        value = case_data[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            if optional:
                return ""
            missing.append(field)
            return ""
        return str(value).strip()

    result = re.sub(r'\{([\w-]+\??)\}', _replace, template).strip()
    if missing:
        raise ValueError(
            f"Missing required fields in input.yaml: {', '.join(missing)}. "
            f"Template: {template}")
    return result


def _build_eval_params(args, config, skill_args, max_budget, timeout_s, effort=None):
    """Snapshot the user-facing eval parameters that defined this run.

    Surfaced in the HTML report so reviewers can see *what was run* without
    inspecting the harness invocation. Only includes parameters that are
    meaningful to a reader: the dataset/skill args, budget caps, execution
    mode, and optional flags actually set. Resolved values (effort, budget,
    timeout) are passed in so the snapshot reflects what actually ran, not
    just what was overridden via CLI."""
    params = {
        "execution_mode": config.execution.mode,
        "max_budget_usd": max_budget,
        "timeout_s": timeout_s,
    }
    # skill is optional for prompt mode
    target = args.skill or config.resolve_skill()
    if target:
        params["skill"] = target
    if skill_args:
        params["skill_args"] = skill_args
    if effort:
        params["effort"] = effort
    if getattr(args, "mlflow_experiment", None):
        params["mlflow_experiment"] = args.mlflow_experiment
    return params


def _resolve_step_env(env):
    """Resolve a step's env dict, expanding ``$VAR`` from the caller's
    environment (mirrors ExecutionConfig.env semantics). Missing vars omitted."""
    out = {}
    for k, v in (env or {}).items():
        if isinstance(v, str) and v.startswith("$"):
            val = os.environ.get(v[1:])
            if val is not None:
                out[k] = val
        elif v is not None:
            out[k] = str(v)
    return out


def _extract_last_assistant_text(stdout, limit=4000):
    """Best-effort final assistant message from a run's stdout, for the
    ``{{ steps.<id>.output }}`` template var. Parses claude-code stream-json;
    falls back to the raw stdout tail for other runners."""
    if not stdout:
        return ""
    texts = []
    saw_json = False
    for line in stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        saw_json = True
        if obj.get("type") == "result" and isinstance(obj.get("result"), str):
            texts.append(obj["result"])
        elif obj.get("type") == "assistant" and not obj.get("parent_tool_use_id"):
            msg = obj.get("message") or {}
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "text":
                    if (block.get("text") or "").strip():
                        texts.append(block["text"])
        elif obj.get("type") == "item.completed":
            item = obj.get("item") or {}
            if (isinstance(item, dict) and item.get("type") == "agent_message"
                    and isinstance(item.get("text"), str)
                    and item["text"].strip()):
                texts.append(item["text"])
    text = (texts[-1] if texts else ("" if saw_json else stdout)).strip()
    return text[:limit]


def _cost_label(cost):
    """Render a per-case cost, honestly showing unknown as such."""
    return (f"${cost:.2f}" if isinstance(cost, (int, float))
            and not isinstance(cost, bool) else "cost n/a")


def _sum_reported_costs(case_results):
    """Sum measured case costs, preserving unknown when none were reported."""
    reported = [
        result["cost_usd"] for result in case_results.values()
        if isinstance(result.get("cost_usd"), (int, float))
        and not isinstance(result.get("cost_usd"), bool)
    ]
    return sum(reported) if reported else None


def _list_step_output_files(case_ws, config):
    """Relative paths of files under the configured output dirs after a step
    (best-effort, for ``{{ steps.<id>.files }}``)."""
    paths = []
    for o in (getattr(config, "outputs", None) or []):
        p = o.get("path") if isinstance(o, dict) else getattr(o, "path", None)
        if p:
            paths.append(p)
    if not paths:
        paths = ["output"]
    files = []
    for rel in paths:
        base = case_ws / rel
        if base.is_dir():
            for f in sorted(base.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    try:
                        files.append(str(f.relative_to(case_ws)))
                    except ValueError:
                        pass
    return files


def _aggregate_step_metrics(step_metrics):
    """Roll per-step RunResult dicts up into one case-level result dict."""
    results = list(step_metrics.values())
    agg_tokens = {}
    for r in results:
        for k, v in (r.get("token_usage") or {}).items():
            if isinstance(v, (int, float)):
                agg_tokens[k] = agg_tokens.get(k, 0) + v
    agg_pm = {}
    for r in results:
        for m, stats in (r.get("per_model_usage") or {}).items():
            agg_pm.setdefault(m, {})
            for k, v in (stats or {}).items():
                if isinstance(v, (int, float)):
                    agg_pm[m][k] = agg_pm[m].get(k, 0) + v
    agg_pmt = {}
    for r in results:
        for m, t in (r.get("per_model_turns") or {}).items():
            if isinstance(t, (int, float)):
                agg_pmt[m] = agg_pmt.get(m, 0) + t
    has_cost = any(r.get("cost_usd") is not None for r in results)
    # A case fails if any step did. max() would mask a negative failure code
    # (e.g. a runner timeout of -1) behind a later 0, so surface the first
    # non-zero exit code instead.
    exit_codes = [r.get("exit_code") or 0 for r in results]
    worst_exit = next((ec for ec in exit_codes if ec != 0), 0)
    return {
        "exit_code": worst_exit,
        "duration_s": round(sum(r.get("duration_s") or 0 for r in results), 1),
        "token_usage": agg_tokens or None,
        "cost_usd": (round(sum(r.get("cost_usd") or 0 for r in results), 4)
                     if has_cost else None),
        "num_turns": sum(r.get("num_turns") or 0 for r in results) or None,
        "per_model_usage": agg_pm or None,
        "per_model_turns": agg_pmt or None,
    }


def _run_single_case_in_repo(runner, skill_name, case_ws, output_dir,
                              skill_args_template, model, mlflow_experiment,
                              mlflow_tracking_uri, system_prompt, max_budget,
                              timeout_s, total_cases, index):
    """Execute in-repo mode: agent runs in repo root, I/O in case_ws.

    Thread-safe: case_ws is case-specific.
    Returns (case_id, result_dict) or (case_id, None) if workspace missing.
    """
    import yaml as _yaml
    import subprocess

    if not case_ws.exists():
        print(f"  [{index}/{total_cases}] {case_ws.name}: SKIP (workspace missing)",
              file=sys.stderr)
        return case_ws.name, None

    # Read metadata
    meta_file = case_ws / "_metadata.yaml"
    if not meta_file.exists():
        print(f"  [{index}/{total_cases}] {case_ws.name}: SKIP (metadata missing)",
              file=sys.stderr)
        return case_ws.name, None

    meta = _yaml.safe_load(meta_file.read_text())
    case_id = meta["case_id"]
    repo_cwd = Path(meta["repo_cwd"])

    # Resolve git (raises RuntimeError if not found)
    git_bin = _resolve_git()

    # Snapshot repo state before execution
    repo_before = subprocess.run(
        [git_bin, "status", "--porcelain"],
        cwd=repo_cwd,
        capture_output=True,
        text=True,
        check=True
    ).stdout

    # Resolve arguments
    case_args = skill_args_template
    input_path = case_ws / "input.yaml"
    if input_path.exists() and case_args:
        case_data = _yaml.safe_load(input_path.read_text()) or {}
        if isinstance(case_data, dict):
            case_args = _resolve_arguments(case_args, case_data)

    if mlflow_experiment:
        from agent_eval.mlflow.experiment import inject_tracing_env
        inject_tracing_env(str(case_ws), project_root=repo_cwd,
                           tracking_uri=mlflow_tracking_uri,
                           experiment_name=mlflow_experiment)

    case_settings = case_ws / ".claude" / "settings.json"
    settings_path = case_settings if case_settings.exists() else None

    exec_label = f"/{skill_name} {case_args}" if skill_name else case_args
    print(f"  [{index}/{total_cases}] {case_id}: {exec_label}",
          file=sys.stderr)

    # Execute in repo with case_ws-based settings
    result = runner.execute(
        target=skill_name,
        args=case_args,
        workspace=repo_cwd,  # Agent runs in REPO
        model=model,
        settings_path=settings_path,  # Settings from case_ws
        system_prompt=system_prompt,
        max_budget_usd=max_budget,
        timeout_s=timeout_s,
    )

    # Verify repo wasn't modified
    repo_after = subprocess.run(
        [git_bin, "status", "--porcelain"],
        cwd=repo_cwd,
        capture_output=True,
        text=True,
        check=True
    ).stdout

    repo_dirty = repo_before != repo_after
    if repo_dirty:
        modified_files = [line for line in repo_after.split('\n')
                          if line and line not in repo_before.split('\n')]
        print(f"    ERROR: {case_id} modified repo:", file=sys.stderr)
        for line in modified_files[:5]:
            print(f"      {line}", file=sys.stderr)
        # Save evidence. Record this case as a failed (repo-modified) result
        # below and continue — a single case violating the read-only contract
        # must not abort the whole run and discard the other cases' results.
        (case_ws / "repo_modifications.txt").write_text(
            f"BEFORE:\n{repo_before}\n\nAFTER:\n{repo_after}\n"
        )

    # Write stdout/stderr to workspace output (for collect.py and judges)
    ws_output = case_ws / "output"
    ws_output.mkdir(parents=True, exist_ok=True)

    if result.stdout:
        (ws_output / "stdout.log").write_text(result.stdout)
    if result.stderr:
        (ws_output / "stderr.log").write_text(result.stderr)

    # Collect outputs to run directory
    case_output = output_dir / "cases" / case_id
    case_output.mkdir(parents=True, exist_ok=True)

    # Copy input.yaml, rejecting symlinks to prevent CWE-59 (path traversal)
    if input_path.exists() and not input_path.is_symlink():
        shutil.copy2(input_path, case_output / "input.yaml")

    # Copy all outputs from case workspace (including stdout/stderr we just wrote)
    # Reject symlinks to prevent CWE-59 (arbitrary file disclosure)
    if ws_output.exists():
        for item in ws_output.iterdir():
            if item.is_file() and not item.is_symlink():
                shutil.copy2(item, case_output / item.name)

    # Copy subagent transcripts (reject symlinks to prevent CWE-59)
    ws_subagents = case_ws / "subagents"
    if ws_subagents.exists() and ws_subagents.is_dir():
        out_subagents = case_output / "subagents"
        out_subagents.mkdir(exist_ok=True)
        for f in ws_subagents.iterdir():
            if f.is_file() and not f.is_symlink() and f.suffix == ".jsonl":
                shutil.copy2(f, out_subagents / f.name)

    case_result = {
        "exit_code": max(result.exit_code, 1) if repo_dirty else result.exit_code,
        "duration_s": round(result.duration_s, 1),
        "token_usage": result.token_usage,
        "cost_usd": result.cost_usd,
        "num_turns": result.num_turns,
        "per_model_usage": result.per_model_usage,
        "per_model_turns": result.per_model_turns,
        "repo_modified": repo_dirty,
    }

    with open(case_output / "run_result.json", "w") as f:
        json.dump(case_result, f, indent=2)
        f.write("\n")

    status = "OK" if result.exit_code == 0 else f"FAIL (exit {result.exit_code})"
    if repo_before != repo_after:
        status += " [REPO MODIFIED]"
    print(f"    → {case_id}: {status} | {result.duration_s:.0f}s | "
          f"{_cost_label(result.cost_usd)}", file=sys.stderr)

    return case_id, case_result


def _run_single_case(runner, skill_name, case_id, case_ws, output_dir,
                     skill_args_template, model, mlflow_experiment,
                     mlflow_tracking_uri, system_prompt, max_budget, timeout_s,
                     total_cases, index, config=None, hook_env=None,
                     global_hook_outputs=None, subagent_model=None):
    """Execute and collect results for a single test case.

    Thread-safe: all I/O is to case-specific directories.
    Returns (case_id, result_dict) or (case_id, None) if workspace missing.
    """
    import yaml as _yaml

    if not case_ws.exists():
        print(f"  [{index}/{total_cases}] {case_id}: SKIP (workspace missing)",
              file=sys.stderr)
        return case_id, None

    # Multi-step pipeline: delegate to the step loop. The single-step path
    # below is unchanged.
    if config is not None and getattr(config.execution, "steps", None):
        return _run_multi_step_case(
            runner, case_id, case_ws, output_dir, model, mlflow_experiment,
            mlflow_tracking_uri, system_prompt, max_budget, timeout_s,
            total_cases, index, config, hook_env=hook_env,
            global_hook_outputs=global_hook_outputs,
            subagent_model=subagent_model)

    case_args = skill_args_template
    input_path = case_ws / "input.yaml"
    if input_path.exists() and case_args:
        case_data = _yaml.safe_load(input_path.read_text()) or {}
        if isinstance(case_data, dict):
            case_args = _resolve_arguments(case_args, case_data)

    if mlflow_experiment:
        from agent_eval.mlflow.experiment import inject_tracing_env
        inject_tracing_env(str(case_ws), project_root=Path.cwd(),
                           tracking_uri=mlflow_tracking_uri,
                           experiment_name=mlflow_experiment)

    case_settings = case_ws / ".claude" / "settings.json"
    settings_path = case_settings if case_settings.exists() else None

    exec_label = f"/{skill_name} {case_args}" if skill_name else case_args
    print(f"  [{index}/{total_cases}] {case_id}: {exec_label}",
          file=sys.stderr)

    # Per-case hooks: build case-specific env, run before_each/after_each.
    # The entire block is wrapped in try/finally so that after_each cleanup
    # hooks fire even if before_each or run_skill raises.
    case_hook_env = None
    merged_hook_data = {}
    merged_env = {}
    result = None
    error_msg = ""
    try:
        if config and config.hooks and hook_env:
            dataset_path = config.dataset.path
            case_source_dir = str(config.resolve_path(dataset_path) / case_id) if dataset_path else ""
            case_hook_env = {
                **hook_env,
                "CASE_ID": case_id,
                "CASE_WORKSPACE": str(case_ws.resolve()),
                "CASE_SOURCE_DIR": case_source_dir,
                "CASE_INPUT": str((case_ws / "input.yaml").resolve()),
            }
            log_dir = output_dir / "hooks"
            if config.hooks.before_each:
                run_hooks(config.hooks.before_each, env=case_hook_env,
                          cwd=case_ws, log_dir=log_dir,
                          phase_name="before_each", case_id=case_id)

            # Collect hook outputs and merge with global
            case_outputs = collect_hook_outputs(case_ws)
            global_env = (global_hook_outputs or {}).get("env", {})
            case_env = case_outputs.get("env", {})
            merged_env = {**global_env, **case_env}

            if merged_env:
                case_hook_env.update({k: str(v) for k, v in merged_env.items()})

            global_data = (global_hook_outputs or {}).get("data", {})
            case_data_out = case_outputs.get("data", {})
            merged_hook_data = {**global_data, **case_data_out}

        result = runner.execute(
            target=skill_name,
            args=case_args,
            workspace=case_ws,
            model=model,
            settings_path=settings_path,
            system_prompt=system_prompt,
            max_budget_usd=max_budget,
            timeout_s=timeout_s,
            extra_env=merged_env or None,
        )
    except Exception as exc:
        print(f"    → {case_id}: ERROR ({exc})", file=sys.stderr)
        result = None
        error_msg = str(exc)
    finally:
        # Run after_each hooks (guaranteed, like after_all)
        if config and config.hooks and config.hooks.after_each and case_hook_env:
            log_dir = output_dir / "hooks"
            run_hooks_safe(config.hooks.after_each, env=case_hook_env,
                           cwd=case_ws, log_dir=log_dir,
                           phase_name="after_each", case_id=case_id)

    if result is None:
        case_output = output_dir / "cases" / case_id
        case_output.mkdir(parents=True, exist_ok=True)
        failed_result = {
            "exit_code": 1,
            "duration_s": 0,
            "token_usage": None,
            "cost_usd": None,
            "num_turns": None,
            "per_model_usage": None,
            "per_model_turns": None,
            "error": error_msg,
        }
        with open(case_output / "run_result.json", "w") as f:
            json.dump(failed_result, f, indent=2)
            f.write("\n")
        return case_id, failed_result

    # Write stdout/stderr to workspace output (for collect.py and judges)
    ws_output = case_ws / "output"
    ws_output.mkdir(parents=True, exist_ok=True)

    if result.stdout:
        (ws_output / "stdout.log").write_text(result.stdout)
    if result.stderr:
        (ws_output / "stderr.log").write_text(result.stderr)

    # Also write immediately to case output for instant access
    case_output = output_dir / "cases" / case_id
    case_output.mkdir(parents=True, exist_ok=True)

    if result.stdout:
        (case_output / "stdout.log").write_text(result.stdout)
    if result.stderr:
        (case_output / "stderr.log").write_text(result.stderr)

    # Save hook output data for judges
    save_hook_data(case_output, merged_hook_data)

    if input_path.exists() and not input_path.is_symlink():
        shutil.copy2(input_path, case_output / "input.yaml")

    ws_subagents = case_ws / "subagents"
    if ws_subagents.exists() and ws_subagents.is_dir():
        out_subagents = case_output / "subagents"
        out_subagents.mkdir(exist_ok=True)
        for f in ws_subagents.iterdir():
            if f.is_file() and not f.is_symlink() and f.suffix == ".jsonl":
                shutil.copy2(f, out_subagents / f.name)

    case_result = {
        "exit_code": result.exit_code,
        "duration_s": round(result.duration_s, 1),
        "token_usage": result.token_usage,
        "cost_usd": result.cost_usd,
        "num_turns": result.num_turns,
        "per_model_usage": result.per_model_usage,
        "per_model_turns": result.per_model_turns,
    }

    with open(case_output / "run_result.json", "w") as f:
        json.dump(case_result, f, indent=2)
        f.write("\n")

    status = "OK" if result.exit_code == 0 else f"FAIL (exit {result.exit_code})"
    print(f"    → {case_id}: {status} | {result.duration_s:.0f}s | "
          f"{_cost_label(result.cost_usd)}", file=sys.stderr)

    return case_id, case_result


def _run_multi_step_case(runner, case_id, case_ws, output_dir, model,
                         mlflow_experiment, mlflow_tracking_uri, system_prompt,
                         max_budget, timeout_s, total_cases, index, config,
                         hook_env=None, global_hook_outputs=None,
                         subagent_model=None):
    """Execute a multi-step pipeline for one case.

    Steps run sequentially in the shared case workspace, so files written by
    step N are visible to N+1.  Each step is one ``runner.execute()``; per-step
    stdout/metrics are saved (for step-scoped judges) and the case metrics are
    the roll-up.  before_each/after_each wrap the whole case; before_step/
    after_step wrap each step.  Thread-safe: all I/O is case-specific.
    """
    import yaml as _yaml

    steps = config.execution.steps
    case_data = {}
    input_path = case_ws / "input.yaml"
    if input_path.exists():
        loaded = _yaml.safe_load(input_path.read_text()) or {}
        if isinstance(loaded, dict):
            case_data = loaded

    if mlflow_experiment:
        from agent_eval.mlflow.experiment import inject_tracing_env
        inject_tracing_env(str(case_ws), project_root=Path.cwd(),
                           tracking_uri=mlflow_tracking_uri,
                           experiment_name=mlflow_experiment)

    case_settings = case_ws / ".claude" / "settings.json"
    settings_path = case_settings if case_settings.exists() else None

    log_dir = output_dir / "hooks"
    case_output = output_dir / "cases" / case_id
    case_output.mkdir(parents=True, exist_ok=True)

    has_hooks = bool(config.hooks and hook_env)
    case_hook_env = None
    base_env = {}
    merged_hook_data = {}
    steps_ctx = {}
    step_metrics = {}
    last_result = None
    error_msg = ""
    aborted_at = None

    try:
        if has_hooks:
            dataset_path = config.dataset.path
            case_source_dir = (str(config.resolve_path(dataset_path) / case_id)
                               if dataset_path else "")
            case_hook_env = {
                **hook_env,
                "CASE_ID": case_id,
                "CASE_WORKSPACE": str(case_ws.resolve()),
                "CASE_SOURCE_DIR": case_source_dir,
                "CASE_INPUT": str((case_ws / "input.yaml").resolve()),
            }
            if config.hooks.before_each:
                run_hooks(config.hooks.before_each, env=case_hook_env,
                          cwd=case_ws, log_dir=log_dir,
                          phase_name="before_each", case_id=case_id)
            case_outputs = collect_hook_outputs(case_ws)
            base_env = {**(global_hook_outputs or {}).get("env", {}),
                        **case_outputs.get("env", {})}
            if base_env:
                case_hook_env.update({k: str(v) for k, v in base_env.items()})
            merged_hook_data = {**(global_hook_outputs or {}).get("data", {}),
                                **case_outputs.get("data", {})}

        for si, step in enumerate(steps, 1):
            step_id = step.id or f"step-{si}"
            is_skill = bool(step.skill and step.skill.strip())
            template = step.arguments if is_skill else step.prompt
            step_target = step.skill if is_skill else None
            resolved = (_resolve_arguments(template, case_data, steps=steps_ctx)
                        if template else "")

            step_env = dict(base_env)
            step_env.update(_resolve_step_env(step.env))

            step_runner = runner
            if getattr(step, "runner", None):
                rtype = step.runner.type
                if rtype not in RUNNERS:
                    raise ValueError(
                        f"step '{step_id}': unknown runner '{rtype}'. "
                        f"Available: {list(RUNNERS)}")
                step_cfg = copy.copy(config)
                step_cfg.runner = step.runner
                step_runner = RUNNERS[rtype].from_config(
                    step_cfg, log_prefix=f"eval:{case_id}",
                    subagent_model=subagent_model,
                    mlflow_experiment=mlflow_experiment,
                    mlflow_tracking_uri=mlflow_tracking_uri,
                    permissions=_resolve_permissions(config),
                    effort=step.runner.effort)

            step_timeout = (step.timeout if step.timeout is not None
                            else timeout_s)
            step_budget = (step.max_budget_usd if step.max_budget_usd is not None
                           else max_budget)

            step_hook_env = None
            if case_hook_env is not None:
                step_hook_env = {**case_hook_env, "STEP_ID": step_id,
                                 "STEP_INDEX": str(si)}
                step_hook_env.update(
                    {k: str(v) for k, v in step_env.items() if v is not None})

            # Log the step target/id only — the resolved args may embed
            # $VAR-expanded secrets or prior-step model output (CWE-532).
            print(f"  [{index}/{total_cases}] {case_id} · step {si}/{len(steps)} "
                  f"({step_id}): {'/' + step_target if step_target else 'prompt'}",
                  file=sys.stderr)

            step_result = None
            try:
                if step_hook_env is not None and config.hooks.before_step:
                    run_hooks(config.hooks.before_step, env=step_hook_env,
                              cwd=case_ws, log_dir=log_dir,
                              phase_name=f"before_step:{step_id}",
                              case_id=case_id)
                    hout = collect_hook_outputs(case_ws)
                    if hout.get("env"):
                        step_env.update(
                            {k: str(v) for k, v in hout["env"].items()})
                step_system_prompt = system_prompt
                if getattr(step, "runner", None) and step.runner.system_prompt:
                    # Same precedence Harbor task generation applies: a
                    # per-step runner override wins over the eval default.
                    step_system_prompt = step.runner.system_prompt
                step_result = step_runner.execute(
                    target=step_target,
                    args=resolved,
                    workspace=case_ws,
                    model=model,
                    settings_path=settings_path,
                    system_prompt=step_system_prompt,
                    max_budget_usd=step_budget,
                    timeout_s=step_timeout,
                    extra_env=step_env or None,
                )
            finally:
                if step_hook_env is not None and config.hooks.after_step:
                    ec = (str(step_result.exit_code)
                          if step_result is not None else "1")
                    run_hooks_safe(
                        config.hooks.after_step,
                        env={**step_hook_env, "STEP_EXIT_CODE": ec},
                        cwd=case_ws, log_dir=log_dir,
                        phase_name=f"after_step:{step_id}", case_id=case_id)

            last_result = step_result
            step_dir = case_output / "steps" / step_id
            step_dir.mkdir(parents=True, exist_ok=True)
            if step_result.stdout:
                (step_dir / "stdout.log").write_text(step_result.stdout)
            if step_result.stderr:
                (step_dir / "stderr.log").write_text(step_result.stderr)

            step_metrics[step_id] = {
                "exit_code": step_result.exit_code,
                "duration_s": round(step_result.duration_s, 1),
                "token_usage": step_result.token_usage,
                "cost_usd": step_result.cost_usd,
                "num_turns": step_result.num_turns,
                "per_model_usage": step_result.per_model_usage,
                "per_model_turns": step_result.per_model_turns,
            }
            steps_ctx[step_id] = {
                "output": _extract_last_assistant_text(step_result.stdout),
                "exit_code": step_result.exit_code,
                "files": _list_step_output_files(case_ws, config),
            }
            status = ("OK" if step_result.exit_code == 0
                      else f"FAIL (exit {step_result.exit_code})")
            print(f"    → {case_id} step {step_id}: {status} | "
                  f"{step_result.duration_s:.0f}s | "
                  f"{_cost_label(step_result.cost_usd)}", file=sys.stderr)

            if step_result.exit_code != 0 and step.on_failure == "fail":
                aborted_at = step_id
                break

    except Exception as exc:
        print(f"    → {case_id}: ERROR ({exc})", file=sys.stderr)
        error_msg = str(exc)
    finally:
        if has_hooks and config.hooks.after_each and case_hook_env is not None:
            run_hooks_safe(config.hooks.after_each, env=case_hook_env,
                           cwd=case_ws, log_dir=log_dir,
                           phase_name="after_each", case_id=case_id)

    if last_result is None:
        failed = {
            "exit_code": 1, "duration_s": 0, "token_usage": None,
            "cost_usd": None, "num_turns": None, "per_model_usage": None,
            "per_model_turns": None, "error": error_msg,
            "steps": step_metrics or None,
        }
        with open(case_output / "run_result.json", "w") as f:
            json.dump(failed, f, indent=2)
            f.write("\n")
        return case_id, failed

    # Whole-case stdout/stderr = the final step (for collect.py + default judges).
    ws_output = case_ws / "output"
    ws_output.mkdir(parents=True, exist_ok=True)
    if last_result.stdout:
        (ws_output / "stdout.log").write_text(last_result.stdout)
        (case_output / "stdout.log").write_text(last_result.stdout)
    if last_result.stderr:
        (ws_output / "stderr.log").write_text(last_result.stderr)
        (case_output / "stderr.log").write_text(last_result.stderr)

    save_hook_data(case_output, merged_hook_data)
    if input_path.exists() and not input_path.is_symlink():
        shutil.copy2(input_path, case_output / "input.yaml")

    ws_subagents = case_ws / "subagents"
    if ws_subagents.exists() and ws_subagents.is_dir():
        out_subagents = case_output / "subagents"
        out_subagents.mkdir(exist_ok=True)
        for f in ws_subagents.iterdir():
            if f.is_file() and not f.is_symlink() and f.suffix == ".jsonl":
                shutil.copy2(f, out_subagents / f.name)

    case_result = _aggregate_step_metrics(step_metrics)
    case_result["steps"] = step_metrics
    if error_msg:
        case_result["error"] = error_msg
        case_result["exit_code"] = max(case_result.get("exit_code", 0), 1)
    if aborted_at:
        case_result["aborted_at_step"] = aborted_at
    with open(case_output / "run_result.json", "w") as f:
        json.dump(case_result, f, indent=2)
        f.write("\n")

    print(f"    → {case_id}: {len(step_metrics)} step(s) | "
          f"{case_result['duration_s']:.0f}s | "
          f"{_cost_label(case_result.get('cost_usd'))}", file=sys.stderr)
    return case_id, case_result


def _resolve_git():
    """Lazily resolve git executable path.

    Raises RuntimeError if git is not found. Only called when repo
    verification is actually needed (in-repo mode), not for all executions.
    """
    git_bin = shutil.which("git")
    if not git_bin:
        raise RuntimeError(
            "git executable not found in PATH. In-repo execution mode requires git "
            "for repository state verification."
        )
    return git_bin


def _snapshot_repo_state(repo_root):
    """Capture git status output as baseline for verification."""
    git_bin = _resolve_git()
    result = subprocess.run(
        [git_bin, "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True
    )
    return result.stdout


def _verify_repo_unchanged(repo_root, initial_state):
    """Verify repo is in same state as initial snapshot."""
    current_state = _snapshot_repo_state(repo_root)
    if current_state != initial_state:
        print("WARNING: Repository state changed during execution", file=sys.stderr)
        print(f"Initial state:\n{initial_state}", file=sys.stderr)
        print(f"Current state:\n{current_state}", file=sys.stderr)
        return False
    return True


def _parse_input_overrides(items):
    """Parse repeated ``KEY=VALUE`` strings into a dict (values stay strings)."""
    out = {}
    for item in (items or []):
        if "=" not in item:
            print(f"WARNING: ignoring malformed --input-override {item!r} "
                  "(expected KEY=VALUE)", file=sys.stderr)
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value
    return out


def _merge_input_overrides(input_path, overrides):
    """Merge ``overrides`` into a case's input.yaml (overrides win). No-op when
    the file is missing/symlinked/non-mapping."""
    import yaml as _yaml
    if not overrides or not input_path.exists() or input_path.is_symlink():
        return
    try:
        data = _yaml.safe_load(input_path.read_text()) or {}
    except _yaml.YAMLError:
        return
    if not isinstance(data, dict):
        return
    data.update(overrides)
    input_path.write_text(_yaml.safe_dump(data, default_flow_style=False, sort_keys=False))


def _execute_per_case(args, config, runner, runner_cls,
                      output_dir, max_budget, timeout_s,
                      model, mlflow_experiment, system_prompt="",
                      skill_args_template=None, eval_params=None,
                      parallelism=None, effort=None, subagent_model=None,
                      target=None):
    """Execute the skill once per case with case-specific arguments."""
    import yaml as _yaml

    if skill_args_template is None:
        skill_args_template = config.execution.arguments

    workspace = Path(args.workspace)
    case_order_path = workspace / "case_order.yaml"
    if not case_order_path.exists():
        print("ERROR: no case_order.yaml in workspace", file=sys.stderr)
        sys.exit(1)

    with open(case_order_path) as f:
        case_order = _yaml.safe_load(f) or []

    # Merge any --input-override KEY=VALUE into each case's input.yaml before
    # execution, so run-level values (e.g. matrix factor levels) resolve as
    # {KEY} in a cli runner command or {{ input.KEY }} in arguments.
    overrides = _parse_input_overrides(getattr(args, "input_override", None))
    if overrides:
        for entry in case_order:
            cid = entry if isinstance(entry, str) else entry["case_id"]
            _merge_input_overrides(workspace / "cases" / cid / "input.yaml", overrides)

    # Detect in-repo mode: check if first case has _metadata.yaml with mode: in-repo
    in_repo_mode = False
    initial_repo_state = None
    repo_root = None

    if case_order:
        first_case_id = case_order[0] if isinstance(case_order[0], str) else case_order[0]["case_id"]
        first_case_ws = workspace / "cases" / first_case_id
        metadata_path = first_case_ws / "_metadata.yaml"

        if metadata_path.exists():
            with open(metadata_path) as f:
                meta = _yaml.safe_load(f) or {}
                if meta.get("mode") == "in-repo":
                    in_repo_mode = True
                    repo_root = Path(meta.get("repo_cwd", Path.cwd()))
                    initial_repo_state = _snapshot_repo_state(repo_root)
                    print(f"In-repo mode detected | Repo: {repo_root}", file=sys.stderr)

    # Multi-step pipelines run in isolated per-case workspaces; in-repo mode
    # (shared repo cwd) is not supported for them in v1.
    if in_repo_mode and config.execution.steps:
        print("ERROR: execution.steps is not supported in in-repo mode "
              "(steps run in an isolated per-case workspace).", file=sys.stderr)
        sys.exit(1)

    # Force sequential execution for in-repo mode to prevent cross-case contamination
    # (all agents share the same repo root, so parallel writes would corrupt state)
    if in_repo_mode:
        effective_parallelism = 1
        if parallelism and parallelism > 1:
            print("WARNING: Forcing parallelism=1 for in-repo mode (shared repo state)", file=sys.stderr)
    else:
        effective_parallelism = min(parallelism, len(case_order)) if parallelism and parallelism > 1 else 1
    parallel_label = f", parallelism={effective_parallelism}" if effective_parallelism > 1 else ""
    skill_label = (f"/{target}" if target
                   else ("steps" if config.execution.steps else "prompt"))
    print(f"Executing: {skill_label} (per-case, {len(case_order)} cases{parallel_label})",
          file=sys.stderr)
    print(f"Agent: {runner.name} | Model: {model}", file=sys.stderr)

    # Build hook environment
    run_id = args.run_id or ""
    hook_env = build_hook_env(
        workspace=str(workspace),
        run_id=run_id,
        config_path=str(Path(args.config).resolve()),
        project_root=str(Path.cwd()),
        model=model,
    )
    log_dir = output_dir / "hooks"

    case_results = {}
    wall_clock_start = time.monotonic()

    try:
        # Run before_all hooks and collect outputs
        global_hook_outputs = {}
        if config.hooks.before_all:
            print("Running before_all hooks...", file=sys.stderr)
            run_hooks(config.hooks.before_all, env=hook_env,
                      cwd=Path.cwd(), log_dir=log_dir,
                      phase_name="before_all")
            global_hook_outputs = collect_hook_outputs(workspace)

        if effective_parallelism > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            futures = {}
            # Permissions are identical for every case; resolve once.
            case_permissions = _resolve_permissions(config)
            with ThreadPoolExecutor(max_workers=effective_parallelism) as pool:
                for i, entry in enumerate(case_order, 1):
                    case_id = entry if isinstance(entry, str) else entry["case_id"]
                    case_ws = workspace / "cases" / case_id
                    case_runner = runner_cls.from_config(
                        config,
                        log_prefix=f"eval:{case_id}",
                        subagent_model=subagent_model,
                        mlflow_experiment=mlflow_experiment,
                        mlflow_tracking_uri=config.mlflow.tracking_uri,
                        effort=effort,
                        permissions=case_permissions,
                    )

                    # Route to in-repo or regular execution based on mode
                    if in_repo_mode:
                        # In-repo mode: agent runs in repo, I/O in case_ws
                        # NO hooks (not yet implemented, see docs/in-repo-hooks-future-enhancement.md)
                        fut = pool.submit(
                            _run_single_case_in_repo, case_runner, target, case_ws,
                            output_dir, skill_args_template, model, mlflow_experiment,
                            config.mlflow.tracking_uri, system_prompt, max_budget, timeout_s,
                            len(case_order), i)
                    else:
                        # Regular isolated workspace mode with hooks
                        fut = pool.submit(
                            _run_single_case, case_runner, target, case_id,
                            case_ws, output_dir, skill_args_template, model,
                            mlflow_experiment, config.mlflow.tracking_uri,
                            system_prompt, max_budget, timeout_s,
                            len(case_order), i,
                            config=config, hook_env=hook_env,
                            global_hook_outputs=global_hook_outputs,
                            subagent_model=subagent_model)
                    futures[fut] = case_id

                for fut in as_completed(futures):
                    case_id, result = fut.result()
                    if result is not None:
                        case_results[case_id] = result
        else:
            for i, entry in enumerate(case_order, 1):
                case_id = entry if isinstance(entry, str) else entry["case_id"]
                case_ws = workspace / "cases" / case_id

                # Route to in-repo or regular execution based on mode
                if in_repo_mode:
                    # In-repo mode: agent runs in repo, I/O in case_ws
                    # NO hooks (not yet implemented, see docs/in-repo-hooks-future-enhancement.md)
                    # Isolate per-case failures (git errors, etc.) so one bad
                    # case is recorded as failed instead of aborting the run.
                    try:
                        case_id, result = _run_single_case_in_repo(
                            runner, target, case_ws, output_dir,
                            skill_args_template, model, mlflow_experiment,
                            config.mlflow.tracking_uri, system_prompt,
                            max_budget, timeout_s, len(case_order), i)
                    except Exception as exc:
                        print(f"    → {case_id}: ERROR ({exc})", file=sys.stderr)
                        result = {
                            "exit_code": 1,
                            "duration_s": 0,
                            "token_usage": None,
                            "cost_usd": None,
                            "num_turns": None,
                            "per_model_usage": None,
                            "per_model_turns": None,
                            "error": str(exc),
                        }
                        case_output = output_dir / "cases" / case_id
                        case_output.mkdir(parents=True, exist_ok=True)
                        with open(case_output / "run_result.json", "w") as f:
                            json.dump(result, f, indent=2)
                            f.write("\n")
                else:
                    # Regular isolated workspace mode with hooks
                    case_id, result = _run_single_case(
                        runner, target, case_id, case_ws, output_dir,
                        skill_args_template, model, mlflow_experiment,
                        config.mlflow.tracking_uri, system_prompt,
                        max_budget, timeout_s, len(case_order), i,
                        config=config, hook_env=hook_env,
                        global_hook_outputs=global_hook_outputs,
                        subagent_model=subagent_model)
                if result is not None:
                    case_results[case_id] = result
    finally:
        if config.hooks.after_all:
            print("Running after_all hooks...", file=sys.stderr)
            run_hooks_safe(config.hooks.after_all, env=hook_env,
                           cwd=Path.cwd(), log_dir=log_dir,
                           phase_name="after_all")

    wall_clock_s = round(time.monotonic() - wall_clock_start, 1)

    # Final repo verification for in-repo mode
    # Use 'is not None' check to handle empty string baseline (clean repo)
    repo_verification_failed = False
    if in_repo_mode and repo_root and initial_repo_state is not None:
        repo_clean = _verify_repo_unchanged(repo_root, initial_repo_state)
        if not repo_clean:
            print("ERROR: Repository was modified during in-repo execution", file=sys.stderr)
            print("This violates the in-repo mode contract - no repo files should change", file=sys.stderr)
            repo_verification_failed = True
        else:
            print("✓ Repository state verified: no changes detected", file=sys.stderr)

    # Aggregate metrics across cases
    total_duration = sum(r["duration_s"] for r in case_results.values())
    total_cost = _sum_reported_costs(case_results)
    total_turns = sum(r.get("num_turns") or 0 for r in case_results.values())
    worst_exit = max((r["exit_code"] for r in case_results.values()), default=0)
    # Ensure exit code reflects repo verification failure
    if repo_verification_failed:
        worst_exit = max(worst_exit, 1)

    agg_tokens = {}
    for r in case_results.values():
        tu = r.get("token_usage") or {}
        for k, v in tu.items():
            if isinstance(v, (int, float)):
                agg_tokens[k] = agg_tokens.get(k, 0) + v

    agg_per_model = {}
    for r in case_results.values():
        pmu = r.get("per_model_usage") or {}
        for m, stats in pmu.items():
            if m not in agg_per_model:
                agg_per_model[m] = {}
            for k, v in stats.items():
                if isinstance(v, (int, float)):
                    agg_per_model[m][k] = agg_per_model[m].get(k, 0) + v

    agg_per_model_turns = {}
    for r in case_results.values():
        pmt = r.get("per_model_turns") or {}
        for m, t in pmt.items():
            if isinstance(t, (int, float)):
                agg_per_model_turns[m] = agg_per_model_turns.get(m, 0) + t

    run_meta = {
        "exit_code": worst_exit,
        "duration_s": round(total_duration, 1),
        "wall_clock_s": wall_clock_s,
        "cost_usd": round(total_cost, 2) if total_cost is not None else None,
        "token_usage": agg_tokens or None,
        "num_turns": total_turns or None,
        "per_model_usage": agg_per_model or None,
        "per_model_turns": agg_per_model_turns or None,
        "num_cases": len(case_results),
        "model": model,
        "agent": runner.name,
        "agent_version": getattr(runner, "version", ""),
        "execution_mode": config.execution.mode,
        "eval_params": eval_params or {},
        "per_case": case_results,
    }
    with open(output_dir / "run_result.json", "w") as f:
        json.dump(run_meta, f, indent=2)
        f.write("\n")

    print(f"EXIT: {worst_exit}")
    print(f"DURATION: {wall_clock_s:.0f}s wall-clock, {total_duration:.0f}s total")
    print(f"COST: ${total_cost:.2f} total" if total_cost is not None
          else "COST: unavailable")
    print(f"CASES: {len(case_results)} "
          f"({sum(1 for r in case_results.values() if r['exit_code'] == 0)} OK, "
          f"{sum(1 for r in case_results.values() if r['exit_code'] != 0)} FAIL)")

    sys.exit(worst_exit)


def _copy_input_files_batch(workspace, output_dir):
    """Copy batch.yaml and case_order.yaml to output dir for MLflow artifact logging."""
    for name in ("batch.yaml", "case_order.yaml"):
        src = workspace / name
        if src.exists() and not src.is_symlink():
            shutil.copy2(src, output_dir / name)


def _save_result(result, args, output_dir, runner, model, eval_params=None):
    """Save batch execution results (stdout, stderr, run_result.json)."""
    if result.stdout:
        (output_dir / "stdout.log").write_text(result.stdout)
    if result.stderr:
        (output_dir / "stderr.log").write_text(result.stderr)

    # Copy subagent transcripts captured by the SubagentStop hook.
    # Only copy regular .jsonl files — reject symlinks (CWE-59).
    ws_subagents = Path(args.workspace) / "subagents"
    if ws_subagents.exists() and ws_subagents.is_dir():
        out_subagents = output_dir / "subagents"
        out_subagents.mkdir(exist_ok=True)
        for f in ws_subagents.iterdir():
            if f.is_file() and not f.is_symlink() and f.suffix == ".jsonl":
                shutil.copy2(f, out_subagents / f.name)

    full_model = result.resolved_model or model
    models_used = result.models_used or []
    # Claude Code annotates the parent model with bracketed suffixes like
    # "[1m]" (the 1M-context variant), but server-side per-message model
    # fields drop the suffix. Compare on the base name so subagents using
    # the same effective model don't get flagged as a distinct subagent.
    def _base(name):
        i = name.find("[")
        return name[:i] if i >= 0 else name
    full_base = _base(full_model)
    subagent_models = [m for m in models_used if _base(m) != full_base]
    subagent_model_str = ", ".join(subagent_models) if subagent_models else full_model

    run_meta = {
        "exit_code": result.exit_code,
        "duration_s": round(result.duration_s, 1),
        "token_usage": result.token_usage,
        "cost_usd": result.cost_usd,
        "per_model_usage": result.per_model_usage,
        "num_turns": result.num_turns,
        "per_model_turns": result.per_model_turns,
        "model": full_model,
        "subagent_model": subagent_model_str,
        "agent": runner.name,
        "agent_version": getattr(runner, "version", ""),
        "execution_mode": "batch",
        "eval_params": eval_params or {},
    }
    run_result_path = output_dir / "run_result.json"
    with open(run_result_path, "w") as f:
        json.dump(run_meta, f, indent=2)
        f.write("\n")

    # Verify the file is valid JSON.

    with open(run_result_path) as f:
        json.load(f)

    print(f"EXIT: {result.exit_code}")
    print(f"DURATION: {result.duration_s:.0f}s")
    if result.cost_usd:
        print(f"COST: ${result.cost_usd:.2f}")


if __name__ == "__main__":
    main()
