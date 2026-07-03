"""MLflow experiment management utilities."""

import os
import shutil
import subprocess
import sys
from typing import Optional


def get_experiment_id(experiment_name: str) -> Optional[str]:
    """Get MLflow experiment ID by name.

    Returns:
        Experiment ID string, or None if not found or MLflow unavailable.
    """
    try:
        import mlflow
        exp = mlflow.get_experiment_by_name(experiment_name)
        return exp.experiment_id if exp else None
    except Exception:
        return None


def resolve_tracking_uri(config) -> str:
    """Resolve MLflow tracking URI with config precedence.

    Precedence: config.mlflow.tracking_uri > MLFLOW_TRACKING_URI env var
    > local default (http://127.0.0.1:5000).
    """
    return (config.mlflow.tracking_uri
            or os.environ.get("MLFLOW_TRACKING_URI")
            or "http://127.0.0.1:5000")


def setup_experiment(experiment_name: str, tracking_uri: Optional[str] = None):
    """Create or set the MLflow experiment.

    Args:
        experiment_name: Name for the experiment.
        tracking_uri: MLflow tracking URI (default: from env or local).
    """
    try:
        import mlflow
    except ImportError:
        print("MLflow not installed. Install with: pip install 'mlflow[genai]'",
              file=sys.stderr)
        return

    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)


def ensure_server(port: int = 5000) -> bool:
    """Check if MLflow server is reachable.

    Checks MLFLOW_TRACKING_URI if set, otherwise localhost:port.

    Returns:
        True if server is available.
    """
    import os
    import urllib.request
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "")
    if tracking_uri and tracking_uri.startswith("http"):
        url = tracking_uri.rstrip("/") + "/version"
    else:
        url = f"http://127.0.0.1:{port}/version"
    try:
        urllib.request.urlopen(url, timeout=3)
        return True
    except Exception:
        return False


def inject_tracing_env(workspace, project_root=None, tracking_uri=None,
                       experiment_name=None):
    """Set MLflow environment variables in workspace settings WITHOUT the Stop hook.

    Use this for eval runs where subagents would each fire the Stop hook,
    creating fragmented traces. Instead, traces are created post-hoc by
    /eval-mlflow from the stream-json log.
    """
    import json

    workspace = os.path.realpath(workspace) if isinstance(workspace, str) else workspace
    claude_dir = os.path.join(str(workspace), ".claude")
    settings_path = os.path.join(claude_dir, "settings.json")

    # Handle symlinked .claude dir (same logic as inject_tracing_hook)
    if os.path.islink(claude_dir):
        link_target = os.path.realpath(claude_dir)
        os.unlink(claude_dir)
        os.makedirs(claude_dir, exist_ok=True)
        if os.path.isdir(link_target):
            for child in os.listdir(link_target):
                if child == "settings.json":
                    continue
                src = os.path.join(link_target, child)
                dst = os.path.join(claude_dir, child)
                if not os.path.exists(dst):
                    os.symlink(src, dst)

    os.makedirs(claude_dir, exist_ok=True)

    settings = {}
    if os.path.exists(settings_path) and not os.path.islink(settings_path):
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    elif project_root:
        proj_settings = os.path.join(str(project_root), ".claude", "settings.json")
        if os.path.exists(proj_settings):
            try:
                with open(proj_settings) as f:
                    settings = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    # Set env vars only — no Stop hook. Claude Code reads the "env" key of
    # settings.json (matching workspace.py / tools.interception); "environment"
    # is ignored, so these vars would never reach the agent under that key.
    env = settings.setdefault("env", {})
    resolved_uri = (tracking_uri
                    or os.environ.get("MLFLOW_TRACKING_URI")
                    or "http://127.0.0.1:5000")
    env["MLFLOW_TRACKING_URI"] = resolved_uri
    if experiment_name:
        env["MLFLOW_EXPERIMENT_NAME"] = experiment_name

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    return settings_path


def inject_tracing_hook(workspace, project_root=None, tracking_uri=None,
                        experiment_name=None):
    """Inject the MLflow Stop hook into a workspace's .claude/settings.json.

    Args:
        workspace: Path to the eval workspace directory.
        project_root: Path to the outer project (for reading base settings).
        tracking_uri: MLflow tracking URI. If not provided, auto-detected
            from MLFLOW_TRACKING_URI env var or defaults to localhost:5000.
        experiment_name: MLflow experiment name. If provided, traces are
            tagged with this experiment instead of the Default experiment.

    This is meant for eval workspaces — the inner Claude Code session that
    runs the skill being evaluated.  It must NOT modify the outer project's
    settings.

    If the workspace's .claude/ is a symlink (no tool-interception hooks),
    it is replaced with a real directory: subdirectories are re-symlinked
    and settings.json is created from the project's original.

    Returns the path to the workspace settings.json, or None on failure.
    """
    import json

    workspace = os.path.realpath(workspace) if isinstance(workspace, str) else workspace
    claude_dir = os.path.join(str(workspace), ".claude")
    settings_path = os.path.join(claude_dir, "settings.json")

    # Resolve the python3 path so the hook works regardless of PATH at
    # runtime (e.g. inside a virtualenv at setup time but bare shell later).
    python_path = shutil.which("python3") or shutil.which("python") or "python3"

    # If .claude is a symlink to the project, replace it with a real dir
    if os.path.islink(claude_dir):
        link_target = os.path.realpath(claude_dir)
        os.unlink(claude_dir)
        os.makedirs(claude_dir, exist_ok=True)
        # Re-symlink subdirectories (skills/, etc.) but not settings.json
        if os.path.isdir(link_target):
            for child in os.listdir(link_target):
                if child == "settings.json":
                    continue
                src = os.path.join(link_target, child)
                dst = os.path.join(claude_dir, child)
                if not os.path.exists(dst):
                    os.symlink(src, dst)

    os.makedirs(claude_dir, exist_ok=True)

    # Load existing settings (workspace-generated or project original)
    settings = {}
    if os.path.exists(settings_path) and not os.path.islink(settings_path):
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    elif project_root:
        proj_settings = os.path.join(str(project_root), ".claude", "settings.json")
        if os.path.exists(proj_settings):
            try:
                with open(proj_settings) as f:
                    settings = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    # Add the Stop hook
    stop_hook = {
        "hooks": [{
            "type": "command",
            "command": (f'{python_path} -c '
                        '"from mlflow.claude_code.hooks import stop_hook_handler; '
                        'stop_hook_handler()"'),
        }],
    }
    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])
    # Avoid duplicates
    if not any("stop_hook_handler" in str(h) for h in stop_hooks):
        stop_hooks.append(stop_hook)

    # Enable tracing via env. Claude Code reads the "env" key of settings.json
    # (matching workspace.py / tools.interception); "environment" is ignored.
    env = settings.setdefault("env", {})
    env["MLFLOW_CLAUDE_TRACING_ENABLED"] = "true"

    # Set tracking URI so traces go to the server, not local mlruns/
    resolved_uri = (tracking_uri
                    or os.environ.get("MLFLOW_TRACKING_URI")
                    or "http://127.0.0.1:5000")
    env["MLFLOW_TRACKING_URI"] = resolved_uri

    if experiment_name:
        env["MLFLOW_EXPERIMENT_NAME"] = experiment_name

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    return settings_path


def setup_autolog(project_dir: str, tracking_uri: str = "http://127.0.0.1:5000",
                  experiment_name: str = ""):
    """Configure MLflow autolog for Claude Code in a project directory.

    .. deprecated::
        Use :func:`inject_tracing_hook` instead — it writes the hook
        directly into the eval workspace without running ``mlflow autolog``
        and without touching the outer project's settings.
    """
    cmd = ["python3", "-m", "mlflow", "autolog", "claude", project_dir,
           "-u", tracking_uri]
    if experiment_name:
        cmd.extend(["-n", experiment_name])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Failed to setup MLflow autolog: {result.stderr}", file=sys.stderr)
        return False

    python_path = shutil.which("python3") or shutil.which("python")
    settings_path = os.path.join(project_dir, ".claude", "settings.json")
    if python_path and os.path.exists(settings_path):
        with open(settings_path) as f:
            content = f.read()
        fixed = content.replace('"python -c', f'"{python_path} -c')
        if fixed != content:
            with open(settings_path, "w") as f:
                f.write(fixed)

    return True


def log_feedback(trace_id: str, name: str, value, source_type: str = "CODE",
                 source_id: str = "agent-eval", rationale: str = ""):
    """Log feedback to a trace."""
    try:
        import mlflow
        from mlflow.entities.assessment import AssessmentSource

        mlflow.log_feedback(
            trace_id=trace_id,
            name=name,
            value=value,
            source=AssessmentSource(source_type=source_type, source_id=source_id),
            rationale=rationale if rationale else None,
        )
    except Exception:
        pass
