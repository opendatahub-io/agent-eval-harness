"""Lifecycle hook executor for eval pipelines.

Runs user-defined shell commands at well-defined points in the eval
lifecycle (before_all, before_each, after_each, before_scoring, after_all).
"""

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent_eval.config import HookEntry


class HookError(Exception):
    """Raised when a hook fails and on_failure is 'fail'."""

    def __init__(self, hook: HookEntry, phase: str, exit_code: int,
                 case_id: Optional[str] = None, timed_out: bool = False):
        self.hook = hook
        self.phase = phase
        self.exit_code = exit_code
        self.case_id = case_id
        self.timed_out = timed_out
        label = hook.description or hook.command[:60]
        detail = "timed out" if timed_out else f"exit code {exit_code}"
        super().__init__(f"Hook failed in {phase}: {label} ({detail})")


@dataclass
class HookResult:
    """Result of a single hook execution."""
    hook: HookEntry
    phase: str
    case_id: Optional[str]
    exit_code: int
    duration_s: float
    skipped: bool
    timed_out: bool
    log_file: Optional[Path]


def _kill_process(proc: subprocess.Popen):
    """Send SIGTERM to the process group, then SIGKILL if needed."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    except OSError:
        pass


def _run_condition(condition: str, env: dict, cwd: Path) -> bool:
    """Check a hook condition. Returns True if the hook should run."""
    try:
        result = subprocess.run(
            ["bash", "-c", condition],
            env=env, cwd=str(cwd),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _safe_log_component(value: str) -> str:
    """Sanitize a string for use in log filenames (CWE-22)."""
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def run_hooks(
    entries: list[HookEntry],
    env: dict[str, str],
    cwd: Path,
    log_dir: Path,
    phase_name: str,
    case_id: Optional[str] = None,
) -> list[HookResult]:
    """Run hooks sequentially. Returns results. Raises HookError on failure.

    Args:
        entries: Hook entries to run in order.
        env: Environment variables for the hook process.
        cwd: Working directory for the hook.
        log_dir: Directory to write hook log files.
        phase_name: Hook phase name (e.g., "before_all").
        case_id: Case ID for per-case hooks (used in log filenames).

    Returns:
        List of HookResult for each hook.

    Raises:
        HookError: If a hook fails and on_failure is "fail".
    """
    if not entries:
        return []

    log_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for i, hook in enumerate(entries):
        label = hook.description or hook.command.split("\n")[0][:60]
        suffix = f".{_safe_log_component(case_id)}" if case_id else ""
        log_file = log_dir / f"{phase_name}{suffix}.{i}.log"

        if hook.condition:
            if not _run_condition(hook.condition, env, cwd):
                print(f"  hook: {label} ... SKIP (condition)", file=sys.stderr)
                results.append(HookResult(
                    hook=hook, phase=phase_name, case_id=case_id,
                    exit_code=0, duration_s=0.0, skipped=True,
                    timed_out=False, log_file=None,
                ))
                continue

        start = time.monotonic()
        timed_out = False
        exit_code = -1

        try:
            with open(log_file, "w") as log_fh:
                proc = subprocess.Popen(
                    ["bash", "-c", hook.command],
                    env=env, cwd=str(cwd),
                    stdout=log_fh, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    proc.wait(timeout=hook.timeout)
                    exit_code = proc.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_process(proc)
                    exit_code = -1
        except OSError as e:
            with open(log_file, "a") as log_fh:
                log_fh.write(f"\nHook failed to start: {e}\n")
            exit_code = -1

        duration = round(time.monotonic() - start, 1)

        if timed_out:
            status = f"TIMEOUT ({hook.timeout}s)"
        elif exit_code == 0:
            status = "OK"
        else:
            status = f"FAIL (exit {exit_code})"
        print(f"  hook: {label} ... {status}", file=sys.stderr)

        results.append(HookResult(
            hook=hook, phase=phase_name, case_id=case_id,
            exit_code=exit_code, duration_s=duration,
            skipped=False, timed_out=timed_out, log_file=log_file,
        ))

        failed = timed_out or exit_code != 0
        if failed and hook.on_failure == "fail":
            raise HookError(hook, phase_name, exit_code,
                            case_id=case_id, timed_out=timed_out)

    return results


def run_hooks_safe(
    entries: list[HookEntry],
    env: dict[str, str],
    cwd: Path,
    log_dir: Path,
    phase_name: str,
    case_id: Optional[str] = None,
) -> list[HookResult]:
    """Like run_hooks but never raises — all failures use continue semantics.

    Used for after_all hooks which are guaranteed to run even on failure.
    """
    if not entries:
        return []

    override = []
    for hook in entries:
        patched = HookEntry(
            command=hook.command,
            timeout=hook.timeout,
            description=hook.description,
            on_failure="continue",
            condition=hook.condition,
        )
        override.append(patched)

    return run_hooks(override, env, cwd, log_dir, phase_name, case_id)


def build_hook_env(
    workspace: str,
    run_id: str,
    config_path: str,
    project_root: str,
    model: str,
    case_id: Optional[str] = None,
    case_workspace: Optional[str] = None,
    case_source_dir: Optional[str] = None,
    case_input: Optional[str] = None,
) -> dict[str, str]:
    """Build environment dict with harness-injected variables."""
    env = dict(os.environ)
    env["AGENT_EVAL_WORKSPACE"] = workspace
    env["AGENT_EVAL_RUN_ID"] = run_id
    env["AGENT_EVAL_CONFIG"] = config_path
    env["AGENT_EVAL_PROJECT_ROOT"] = project_root
    env["AGENT_EVAL_MODEL"] = model

    if case_id is not None:
        env["CASE_ID"] = case_id
    if case_workspace is not None:
        env["CASE_WORKSPACE"] = case_workspace
    if case_source_dir is not None:
        env["CASE_SOURCE_DIR"] = case_source_dir
    if case_input is not None:
        env["CASE_INPUT"] = case_input

    return env


def main():
    """CLI entry point for standalone hook invocation."""
    import argparse
    from agent_eval.config import EvalConfig

    parser = argparse.ArgumentParser(
        description="Run lifecycle hooks from eval.yaml")
    parser.add_argument("--config", default="eval.yaml",
                        help="Path to eval.yaml")
    parser.add_argument("--phase", required=True,
                        choices=["before_all", "before_each", "after_each",
                                 "before_scoring", "after_all"],
                        help="Hook phase to run")
    parser.add_argument("--workspace", required=True,
                        help="Workspace root path")
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--model", required=True, help="Skill model")
    parser.add_argument("--output", required=True,
                        help="Output/run directory for hook logs")
    parser.add_argument("--case-id", default=None,
                        help="Case ID (for per-case hooks)")
    parser.add_argument("--case-workspace", default=None,
                        help="Case workspace path (for per-case hooks)")
    parser.add_argument("--case-source-dir", default=None,
                        help="Case source directory (for per-case hooks)")
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)
    entries = getattr(config.hooks, args.phase, [])

    if not entries:
        return

    env = build_hook_env(
        workspace=args.workspace,
        run_id=args.run_id,
        config_path=str(Path(args.config).resolve()),
        project_root=str(Path.cwd()),
        model=args.model,
        case_id=args.case_id,
        case_workspace=args.case_workspace,
        case_source_dir=args.case_source_dir,
        case_input=(str(Path(args.case_workspace) / "input.yaml")
                    if args.case_workspace else None),
    )

    cwd = Path(args.case_workspace) if args.case_workspace else Path.cwd()
    log_dir = Path(args.output) / "hooks"

    phase = args.phase
    if phase == "after_all":
        run_hooks_safe(entries, env, cwd, log_dir, phase,
                       case_id=args.case_id)
    else:
        try:
            run_hooks(entries, env, cwd, log_dir, phase,
                      case_id=args.case_id)
        except HookError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
