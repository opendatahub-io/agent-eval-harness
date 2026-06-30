#!/usr/bin/env python3
"""Validate eval.yaml and eval.md.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py config [eval.yaml]
    python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py memory [eval.md]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import ast
import hashlib
import sys
from pathlib import Path

import yaml

# Import skill lookup from sibling module
sys.path.insert(0, str(Path(__file__).parent))
from find_skills import find_skill


def _discover_builtin_judge_names() -> set[str]:
    """Scan agent_eval/judges/ for available builtin judge names (no module loading)."""
    judges_dir = Path(__file__).resolve().parents[2] / ".." / "agent_eval" / "judges"
    judges_dir = judges_dir.resolve()
    names: set[str] = set()
    if not judges_dir.is_dir():
        return names
    for category_dir in judges_dir.iterdir():
        if not category_dir.is_dir() or category_dir.name.startswith("_"):
            continue
        for f in category_dir.iterdir():
            if f.name.startswith("_"):
                continue
            if f.suffix in (".py", ".md"):
                names.add(f.stem)
    return names


def validate_config(path="eval.yaml"):
    """Validate eval.yaml — structure, completeness, and file references."""
    p = Path(path)
    if not p.exists():
        print(f"NOT_FOUND: {path}")
        sys.exit(1)

    with open(p) as f:
        config = yaml.safe_load(f) or {}

    config_dir = p.resolve().parent

    errors = []
    warnings = []

    # --- Structure checks ---
    if not config.get("skill"):
        errors.append("Missing 'skill' field")
    if not config.get("name"):
        errors.append("Missing 'name' field")

    dataset = config.get("dataset", {})
    outputs = config.get("outputs", [])
    judges = config.get("judges", [])

    if not dataset.get("path"):
        warnings.append("No dataset.path — eval-run won't find test cases")
    if not dataset.get("schema"):
        warnings.append("No dataset.schema — agents won't understand case structure")
    if not outputs:
        warnings.append("No outputs — collect step won't know where to find artifacts")
    if not judges:
        warnings.append("No judges — scoring step will have nothing to run")

    # --- Skill reference check ---
    skill_name = config.get("skill", "")
    if skill_name and not find_skill(skill_name):
        warnings.append(f"skill '{skill_name}' not found in project")

    # --- File reference checks (resolve relative to config file location) ---
    dataset_path = dataset.get("path", "")
    if dataset_path:
        dp = Path(dataset_path) if Path(dataset_path).is_absolute() else config_dir / dataset_path
        if not dp.exists():
            errors.append(f"dataset.path '{dataset_path}' does not exist")
        elif not any(p for p in dp.iterdir() if not p.name.startswith(".")):
            warnings.append(f"dataset.path '{dataset_path}' is empty")

    for i, o in enumerate(outputs):
        out_path = o.get("path", "")
        if out_path:
            op = Path(out_path)
            if op.is_absolute():
                errors.append(f"outputs[{i}].path must be relative: {out_path}")
            elif ".." in op.parts:
                errors.append(f"outputs[{i}].path must not traverse parent: {out_path}")

    builtin_names = _discover_builtin_judge_names()

    for j in judges:
        name = j.get("name", "unnamed")
        prompt_file = j.get("prompt_file", "")
        if prompt_file:
            pf = Path(prompt_file) if Path(prompt_file).is_absolute() else config_dir / prompt_file
            if not pf.exists():
                errors.append(f"judges.{name}.prompt_file '{prompt_file}' not found")
        for ctx_file in j.get("context", []):
            cf = Path(ctx_file) if Path(ctx_file).is_absolute() else config_dir / ctx_file
            if not cf.exists():
                warnings.append(f"judges.{name}.context '{ctx_file}' not found")
        module = j.get("module", "")
        if module:
            try:
                import importlib
                mod = importlib.import_module(module)
            except ImportError:
                errors.append(f"judges.{name}.module '{module}' not importable")
                mod = None
            func_name = j.get("function", "")
            if mod and func_name:
                fn = getattr(mod, func_name, None)
                if fn is None:
                    errors.append(f"judges.{name}.function '{func_name}' not found in module '{module}'")
                elif not callable(fn):
                    errors.append(f"judges.{name}.function '{func_name}' in '{module}' is not callable")

        builtin = j.get("builtin", "")
        if builtin:
            flat_name = builtin.rsplit("/", 1)[-1] if "/" in builtin else builtin
            if flat_name not in builtin_names:
                errors.append(
                    f"judges.{name}.builtin '{builtin}' not found "
                    f"(available: {', '.join(sorted(builtin_names)) or 'none'})")

        check_expr = j.get("check", "")
        if check_expr:
            try:
                ast.parse(check_expr, mode="exec")
            except SyntaxError as e:
                errors.append(f"judges.{name}.check has invalid Python syntax: {e.msg}")

    # --- Execution config ---
    execution = config.get("execution", {})
    exec_mode = execution.get("mode", "case")
    if exec_mode not in ("case", "batch"):
        errors.append(f"execution.mode must be 'case' or 'batch', got '{exec_mode}'")
    if not execution.get("arguments"):
        warnings.append("No execution.arguments — skill will be invoked with no arguments")

    # --- Inputs (tool interception) ---
    for t in (config.get("inputs", {}).get("tools") or []):
        if not t.get("match"):
            warnings.append("inputs.tools entry missing 'match' field")
        if not t.get("prompt") and not t.get("prompt_file"):
            warnings.append(f"inputs.tools entry '{t.get('match', '?')[:30]}' has no prompt")
        prompt_file = t.get("prompt_file", "")
        if prompt_file:
            pf = Path(prompt_file) if Path(prompt_file).is_absolute() else config_dir / prompt_file
            if not pf.exists():
                errors.append(f"inputs.tools prompt_file '{prompt_file}' not found")

    runner = config.get("runner") or {}
    settings = runner.get("settings")
    if isinstance(settings, str) and settings:
        sp = Path(settings) if Path(settings).is_absolute() else config_dir / settings
        if not sp.exists():
            errors.append(f"runner.settings '{settings}' not found")

    plugin_dirs = runner.get("plugin_dirs") or []
    for i, pd in enumerate(plugin_dirs):
        pp = Path(pd) if Path(pd).is_absolute() else config_dir / pd
        if not pp.exists():
            warnings.append(f"runner.plugin_dirs[{i}] '{pd}' not found")
        elif not pp.is_dir():
            warnings.append(f"runner.plugin_dirs[{i}] '{pd}' is not a directory")

    # --- Models ---
    models = config.get("models") or {}
    if not models.get("skill"):
        warnings.append("No models.skill — eval-run will require --model on every invocation")
    if not models.get("judge"):
        warnings.append("No models.judge — LLM/pairwise judges will need EVAL_JUDGE_MODEL or per-judge 'model:'")

    # --- Thresholds ---
    thresholds = config.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        errors.append("thresholds must be a mapping of <judge_name>: <threshold_config>")
        thresholds = {}
    judge_names = {j.get("name", "") for j in judges if isinstance(j, dict) and j.get("name")}
    for thresh_name in thresholds:
        if thresh_name not in judge_names:
            warnings.append(
                f"thresholds.{thresh_name} references non-existent judge "
                f"(available: {', '.join(sorted(judge_names)) or 'none'})")

    # --- Report ---
    if errors:
        for e in errors:
            print(f"ERROR: {e}")

    status = "VALID"
    if errors:
        status = "INVALID"
    elif warnings:
        status = "INCOMPLETE"

    mlflow = config.get("mlflow") or {}
    print(f"{status}: {config.get('name')} (skill={config.get('skill')})")
    print(f"  execution: mode={exec_mode}, arguments={'yes' if execution.get('arguments') else 'no'}")
    print(f"  runner: {runner.get('type', 'claude-code')}")
    print(f"  models: skill={models.get('skill', 'unset')}, judge={models.get('judge', 'unset')}")
    print(f"  mlflow: experiment={mlflow.get('experiment') or config.get('name', 'unset')}")
    print(f"  dataset: {dataset.get('path', 'not set')}")
    print(f"  schema: {'yes' if dataset.get('schema') else 'no'}")
    print(f"  outputs: {len(outputs)} directories")
    print(f"  judges: {len(judges)}")

    for w in warnings:
        print(f"  WARNING: {w}")

    if errors:
        sys.exit(1)


def validate_memory(path="eval.md"):
    """Check if eval.md is fresh (skill hasn't changed)."""
    p = Path(path)
    if not p.exists():
        print("STALE: eval.md does not exist")
        sys.exit(1)

    content = p.read_text()
    if not content.startswith("---"):
        print("STALE: no frontmatter")
        sys.exit(1)

    parts = content.split("---", 2)
    if len(parts) < 3:
        print("STALE: invalid frontmatter")
        sys.exit(1)

    fm = yaml.safe_load(parts[1]) or {}
    skill_name = fm.get("skill", "")
    stored_hash = fm.get("skill_hash", "")

    if not skill_name or not stored_hash:
        print("STALE: missing skill or hash in frontmatter")
        sys.exit(1)

    skill_path = find_skill(skill_name)
    if not skill_path:
        print(f"STALE: skill '{skill_name}' not found")
        sys.exit(1)

    current_hash = hashlib.sha256(skill_path.read_bytes()).hexdigest()[:12]
    if current_hash == stored_hash:
        print(f"FRESH: {skill_name} (hash={stored_hash})")
    else:
        print(f"STALE: skill changed ({stored_hash} -> {current_hash})")
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: validate_eval.py <config|memory> [path]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "config":
        path = sys.argv[2] if len(sys.argv) > 2 else "eval.yaml"
        validate_config(path)
    elif cmd == "memory":
        path = sys.argv[2] if len(sys.argv) > 2 else "eval.md"
        validate_memory(path)
    else:
        print(f"Unknown: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
