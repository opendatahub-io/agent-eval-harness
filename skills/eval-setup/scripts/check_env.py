#!/usr/bin/env python3
"""Preflight environment checks for the agent eval harness.

Verifies dependencies, API keys, MLflow server, and directory structure.
Returns exit code 0 if all required checks pass, 1 otherwise.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/check_env.py [--config eval.yaml] [--fix]
"""

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None,
                        help="Path to eval.yaml (checks config validity if provided)")
    parser.add_argument("--fix", action="store_true",
                        help="Create missing directories")
    args = parser.parse_args()

    checks = []

    # 1. Python version
    py = sys.version_info
    ok = py >= (3, 11)
    checks.append(("python", ok, f"{py.major}.{py.minor}.{py.micro}",
                    "Requires Python >= 3.11"))

    # 2. mlflow[genai]
    mlflow_ver = _check_import("mlflow")
    checks.append(("mlflow", bool(mlflow_ver), mlflow_ver or "not installed",
                    "Run /eval-setup to install"))

    # 3. pyyaml
    yaml_ver = _check_import("yaml", "pyyaml")
    checks.append(("pyyaml", bool(yaml_ver), yaml_ver or "not installed",
                    "Run /eval-setup to install"))

    # 4. agent_eval (available via symlink in each skill's scripts/ dir)
    script_dir = Path(__file__).parent
    agent_eval_link = script_dir / "agent_eval"
    link_ok = agent_eval_link.is_dir()
    if link_ok:
        link_detail = f"symlink at {agent_eval_link}"
    else:
        link_detail = f"missing symlink at {agent_eval_link}"
    checks.append(("agent_eval", link_ok, link_detail,
                    "Create symlink: ln -sf ../../../agent_eval scripts/agent_eval"))

    # 5. anthropic SDK (optional — needed for LLM judges and pairwise comparison)
    anthropic_ver = _check_import("anthropic")
    checks.append(("anthropic", bool(anthropic_ver),
                    anthropic_ver or "not installed (optional)",
                    "Run /eval-setup to install"))

    # 6. API keys
    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_vertex = bool(os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"))
    api_ok = has_api_key or has_vertex
    api_detail = []
    if has_api_key:
        api_detail.append("ANTHROPIC_API_KEY set")
    if has_vertex:
        api_detail.append(f"ANTHROPIC_VERTEX_PROJECT_ID={os.environ['ANTHROPIC_VERTEX_PROJECT_ID']}")
    checks.append(("api_keys", api_ok,
                    ", ".join(api_detail) if api_detail else "none set",
                    "export ANTHROPIC_API_KEY=<key> or ANTHROPIC_VERTEX_PROJECT_ID=<id>"))

    # 7. MLflow tracking URI
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "")
    if tracking_uri:
        mlflow_ok = True
        mlflow_detail = tracking_uri
    else:
        mlflow_ok = _check_mlflow_server()
        mlflow_detail = "localhost:5000 reachable" if mlflow_ok else "no server (will use local file store)"

    checks.append(("mlflow_server", True,  # not a hard requirement
                    mlflow_detail,
                    "export MLFLOW_TRACKING_URI=<uri> or run: mlflow server --port 5000"))

    # 8. Directory structure
    runs_dir = os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs")
    dirs_to_check = [runs_dir, "tmp"]
    missing_dirs = [d for d in dirs_to_check if not Path(d).exists()]
    dirs_ok = len(missing_dirs) == 0
    if not dirs_ok and args.fix:
        for d in missing_dirs:
            Path(d).mkdir(parents=True, exist_ok=True)
        dirs_ok = True
        missing_dirs = []
    checks.append(("directories", dirs_ok,
                    "all present" if dirs_ok else f"missing: {', '.join(missing_dirs)}",
                    f"mkdir -p {runs_dir} tmp" + (" (use --fix to create)" if not args.fix else "")))

    # 9. eval.yaml (if --config provided)
    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            try:
                from agent_eval.config import EvalConfig
                config = EvalConfig.from_yaml(args.config)
                config_detail = f"valid: {config.name} (skill={config.skill})"
                config_ok = bool(config.skill)
            except Exception as e:
                config_detail = f"invalid: {e}"
                config_ok = False
        else:
            config_detail = f"not found: {args.config}"
            config_ok = False
        checks.append(("eval_config", config_ok, config_detail,
                        "Run /eval-analyze to generate eval.yaml"))

    # Print report
    all_required_ok = True
    print("Environment Check")
    print("=" * 60)
    for name, ok, detail, fix in checks:
        status = "OK" if ok else "MISSING"
        icon = "+" if ok else "-"
        print(f"  [{icon}] {name:<16} {detail}")
        if not ok and name not in ("anthropic", "mlflow", "mlflow_server"):
            all_required_ok = False
            print(f"      Fix: {fix}")

    print("=" * 60)
    if all_required_ok:
        print("Ready to run evaluations.")
    else:
        print("Some required checks failed. Fix the issues above.")
        sys.exit(1)


def _check_import(module_name, pip_name=None):
    """Check if a module is importable, return its version or None."""
    try:
        mod = __import__(module_name)
        return getattr(mod, "__version__", "installed")
    except ImportError:
        return None


def _check_mlflow_server(port=5000):
    """Check if MLflow server is reachable."""
    try:
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    main()
