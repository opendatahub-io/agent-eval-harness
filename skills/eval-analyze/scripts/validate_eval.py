#!/usr/bin/env python3
"""Validate eval.yaml and eval.md.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py config [eval.yaml]
    python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py memory [eval.md]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import hashlib
import sys
from pathlib import Path

import yaml

# Import skill lookup from sibling module
sys.path.insert(0, str(Path(__file__).parent))
from find_skills import find_skill


def validate_config(path="eval.yaml"):
    """Validate eval.yaml — structure, completeness, and file references."""
    p = Path(path)
    if not p.exists():
        print(f"NOT_FOUND: {path}")
        sys.exit(1)

    with open(p) as f:
        try:
            config = yaml.safe_load(f) or {}
        except yaml.scanner.ScannerError as e:
            print(f"YAML_SYNTAX_ERROR: {path}")
            print(f"  {e}")
            print("\nTip: Look for unquoted strings in lists, especially with '()', '-', or ':'")
            print("  Bad:  - field (type)")
            print("  Good: - \"field (type)\"")
            sys.exit(1)

    config_dir = p.resolve().parent

    errors = []
    warnings = []

    # --- Structure checks ---
    if not config.get("name"):
        errors.append("Missing 'name' field")

    # Either execution.skill or execution.prompt must be set (with top-level skill as fallback)
    execution = config.get("execution", {})
    has_exec_skill = bool(execution.get("skill", "").strip())
    has_exec_prompt = bool(execution.get("prompt", "").strip())
    has_top_level_skill = bool(config.get("skill", "").strip())

    # This check is now redundant with the ExecutionConfig validation but kept for backward compat
    if not has_exec_skill and not has_exec_prompt and not has_top_level_skill:
        errors.append("Either execution.skill or execution.prompt must be set (or top-level 'skill' for backward compat)")

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
    # Check execution.skill first, then fall back to top-level skill
    skill_name = execution.get("skill", "") or config.get("skill", "")
    if skill_name and not find_skill(skill_name):
        warnings.append(f"skill '{skill_name}' not found in project")

    # --- Documentation structure check ---
    domain = dataset.get("domain", {})
    doc_structure = domain.get("documentation_structure", {})
    entry_point = doc_structure.get("entry_point", "")
    if entry_point:
        ep = Path(entry_point)
        if not ep.exists():
            errors.append(f"dataset.domain.documentation_structure.entry_point '{entry_point}' does not exist")

    # --- File reference checks (resolve relative to config file location) ---
    dataset_path = dataset.get("path", "")
    if dataset_path:
        dp = Path(dataset_path) if Path(dataset_path).is_absolute() else config_dir / dataset_path
        if not dp.exists():
            warnings.append(f"dataset.path '{dataset_path}' does not exist (run /eval-dataset to generate)")
        elif not any(p for p in dp.iterdir() if not p.name.startswith(".")):
            warnings.append(f"dataset.path '{dataset_path}' is empty (run /eval-dataset to generate cases)")

    for i, o in enumerate(outputs):
        out_path = o.get("path", "")
        if out_path:
            op = Path(out_path)
            if op.is_absolute():
                errors.append(f"outputs[{i}].path must be relative: {out_path}")
            elif ".." in op.parts:
                errors.append(f"outputs[{i}].path must not traverse parent: {out_path}")

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
                importlib.import_module(module)
            except ImportError:
                errors.append(f"judges.{name}.module '{module}' not importable")

    # --- Execution config ---
    execution = config.get("execution", {})
    exec_mode = execution.get("mode", "case")
    if exec_mode not in ("case", "batch"):
        errors.append(f"execution.mode must be 'case' or 'batch', got '{exec_mode}'")

    # Check mutual exclusivity of skill and prompt
    has_skill = bool(execution.get("skill", "").strip())
    has_prompt = bool(execution.get("prompt", "").strip())
    top_level_skill = bool(config.get("skill", "").strip())

    if has_skill and has_prompt:
        errors.append(
            "execution.skill and execution.prompt are mutually exclusive. "
            "Use execution.skill for '/skill-name' invocations or execution.prompt for direct prompts."
        )

    # At least one of skill/prompt must be set (with top-level skill as fallback)
    if not has_skill and not has_prompt and not top_level_skill:
        errors.append(
            "Either execution.skill or execution.prompt must be set. "
            "Use execution.skill for skill invocations, execution.prompt for direct prompt mode."
        )

    # Warn if arguments missing for skill mode
    if (has_skill or top_level_skill) and not has_prompt and not execution.get("arguments"):
        warnings.append("No execution.arguments — skill will be invoked with no arguments")

    # For prompt mode, the prompt template should be valid
    if has_prompt:
        prompt = execution.get("prompt", "")
        if not prompt.strip():
            errors.append("execution.prompt is set but empty")

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
    skill_display = config.get("skill") or f"mode={exec_mode}"
    print(f"{status}: {config.get('name')} (skill={skill_display})")
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
    """Check if eval.md is fresh (skill or documentation hasn't changed)."""
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
    eval_type = fm.get("type", "")

    # Handle documentation-based evals
    if eval_type == "documentation-eval":
        stored_hash = fm.get("documentation_hash", "")
        if not stored_hash:
            print("STALE: missing documentation_hash in frontmatter")
            sys.exit(1)

        # Check CLAUDE.md or AGENTS.md (prefer CLAUDE.md)
        doc_path = Path("CLAUDE.md") if Path("CLAUDE.md").exists() else Path("AGENTS.md")
        if not doc_path.exists():
            print("STALE: no CLAUDE.md or AGENTS.md found")
            sys.exit(1)

        current_hash = hashlib.sha256(doc_path.read_bytes()).hexdigest()[:12]
        if current_hash == stored_hash:
            print(f"FRESH: documentation-eval (hash={stored_hash})")
        else:
            print(f"STALE: documentation changed ({stored_hash} -> {current_hash})")
            sys.exit(1)
        return

    # Handle skill-based evals
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
