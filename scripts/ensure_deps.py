#!/usr/bin/env python3
"""Install Python dependencies into an isolated venv.

Creates .eval-venv/ at the plugin root and installs packages there.
Prefers uv for speed, falls back to stdlib venv + pip.

Checks eval.yaml (if it exists) to decide which optional deps to install:
- pyyaml: always required
- mlflow[genai]: if a mlflow block is present in eval.yaml
- anthropic[vertex]: if LLM judges or pairwise comparison are configured

Caches a stamp file in CLAUDE_PLUGIN_DATA so installs only run once
(or when eval.yaml changes).
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

VENV_DIR_NAME = ".eval-venv"


def main():
    plugin_data = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    plugin_root = Path(__file__).parent.parent
    venv_dir = plugin_root / VENV_DIR_NAME
    venv_python = venv_dir / "bin" / "python3"

    deps = _resolve_deps(plugin_root)
    stamp = _compute_stamp(deps)

    stamp_file = None
    if plugin_data:
        plugin_data.mkdir(parents=True, exist_ok=True)
        stamp_file = plugin_data / "deps.stamp"
        if stamp_file.exists() and stamp_file.read_text().strip() == stamp:
            if venv_python.exists() and _all_importable(venv_python, deps):
                return

    _ensure_venv(venv_dir)
    _install_deps(venv_dir, [spec for spec, _ in deps])

    if stamp_file:
        stamp_file.write_text(stamp)


def _resolve_deps(plugin_root):
    """Determine which deps are needed based on eval.yaml."""
    deps = [("pyyaml>=6.0", "yaml")]

    eval_yaml = _find_eval_yaml(plugin_root)
    if not eval_yaml or not eval_yaml.exists():
        return deps

    try:
        import yaml
        config = yaml.safe_load(eval_yaml.read_text()) or {}
    except Exception:
        try:
            config = _parse_yaml_minimal(eval_yaml.read_text())
        except Exception:
            return deps

    if not isinstance(config, dict):
        return deps

    if config.get("mlflow") is not None:
        deps.append(("mlflow[genai]>=3.5", "mlflow"))

    judges = config.get("judges", [])
    if isinstance(judges, list):
        for j in judges:
            if not isinstance(j, dict):
                continue
            if j.get("prompt") or j.get("prompt_file") or j.get("pairwise"):
                deps.append(("anthropic[vertex]>=0.40", "anthropic"))
                break
    elif isinstance(judges, dict):
        # _parse_yaml_minimal can't parse YAML lists, so judges may be
        # a dict or empty. Install anthropic as a safe default since we
        # can't tell whether LLM judges are configured.
        deps.append(("anthropic[vertex]>=0.40", "anthropic"))

    return deps


def _parse_yaml_minimal(text):
    """Minimal YAML-like extraction when pyyaml isn't available yet."""
    result = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if ":" in stripped and not stripped.startswith("-"):
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                result[key] = val
            else:
                result[key] = {}
    return result


def _ensure_venv(venv_dir):
    """Create the venv if it doesn't exist."""
    if (venv_dir / "bin" / "python3").exists():
        return

    uv = shutil.which("uv")
    if uv:
        print(f"Creating venv with uv: {venv_dir}")
        subprocess.run([uv, "venv", str(venv_dir), "--seed",
                        "--python", sys.executable],
                       check=True, capture_output=True, text=True)
    else:
        print(f"Creating venv: {venv_dir}")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)],
                       check=True, capture_output=True, text=True)


def _install_deps(venv_dir, specs):
    """Install packages into the venv."""
    if not specs:
        return

    uv = shutil.which("uv")
    venv_pip = venv_dir / "bin" / "pip"
    venv_python = venv_dir / "bin" / "python3"

    print(f"Installing: {', '.join(specs)}")

    if uv:
        result = subprocess.run(
            [uv, "pip", "install", "-q", "--python", str(venv_python), *specs],
            capture_output=True, text=True,
        )
    elif venv_pip.exists():
        result = subprocess.run(
            [str(venv_pip), "install", "-q", *specs],
            capture_output=True, text=True,
        )
    else:
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-q", *specs],
            capture_output=True, text=True,
        )

    if result.returncode != 0:
        print(f"Install failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def _all_importable(venv_python, deps):
    """Check all deps are importable in the venv python."""
    imports = ";".join(f"__import__('{mod}')" for _, mod in deps)
    result = subprocess.run(
        [str(venv_python), "-c", imports],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _find_eval_yaml(plugin_root):
    cwd = Path.cwd()
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agent_eval.config import discover_configs
        configs = discover_configs(cwd)
        if configs:
            return configs[0].path
    except Exception:
        pass
    for candidate in [cwd / "eval.yaml", plugin_root / "eval.yaml"]:
        if candidate.exists():
            return candidate
    return None


def _compute_stamp(deps):
    return hashlib.sha256("|".join(sorted(s for s, _ in deps)).encode()).hexdigest()[:12]


if __name__ == "__main__":
    main()
