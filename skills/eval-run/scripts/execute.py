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
import json
import os
import shutil
import sys
import time
from pathlib import Path

from agent_eval.agent import RUNNERS
from agent_eval.hooks import (
    HookError, build_hook_env, collect_hook_outputs, inject_hook_env,
    run_hooks, run_hooks_safe, save_hook_data,
)

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


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--skill", required=True)
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
                        choices=["low", "medium", "high", "xhigh", "max"],
                        help="Claude Code reasoning effort (default: from eval.yaml runner.effort)")
    parser.add_argument("--parallelism", type=int, default=None,
                        help="Max parallel case executions (default: from eval.yaml or sequential)")
    parser.add_argument("--run-id", default=None,
                        help="Run identifier (for hook env vars and log paths)")
    args = parser.parse_args()

    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml(args.config)

    # Resolve model: CLI > config; required to be set somewhere.
    model = args.model or config.models.skill
    if not model:
        print("ERROR: no model specified. Set --model or models.skill in eval.yaml.",
              file=sys.stderr)
        sys.exit(1)
    subagent_model = args.subagent_model or config.models.subagent or model

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Resolve skill args: CLI override > config > empty
    skill_args = args.skill_args if args.skill_args is not None else config.execution.arguments

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
    runner_cls = RUNNERS[agent]

    mlflow_experiment = args.mlflow_experiment or config.mlflow.experiment
    effort = args.effort or config.runner.effort

    runner = runner_cls.from_config(
        config,
        log_prefix="eval",
        subagent_model=subagent_model,
        mlflow_experiment=mlflow_experiment,
        mlflow_tracking_uri=config.mlflow.tracking_uri,
        effort=effort,
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

    # ── Per-case execution ───────────────────────────────────────
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
                          subagent_model=subagent_model)
        return

    # ── Batch execution (below) ──────────────────────────────────
    print(f"Executing: /{args.skill} {skill_args}", file=sys.stderr)
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

        # Inject hook-output env vars into workspace settings.json
        inject_hook_env(args.workspace, global_hook_outputs.get("env"))
        save_hook_data(output_dir, global_hook_outputs.get("data"))

        # Set MLflow environment in the workspace settings
        if mlflow_experiment:
            from agent_eval.mlflow.experiment import inject_tracing_env
            inject_tracing_env(args.workspace, project_root=Path.cwd(),
                               tracking_uri=config.mlflow.tracking_uri,
                               experiment_name=mlflow_experiment)

        workspace_settings = Path(args.workspace) / ".claude" / "settings.json"
        settings_path = workspace_settings if workspace_settings.exists() else None

        result = runner.run_skill(
            skill_name=args.skill,
            args=skill_args,
            workspace=Path(args.workspace),
            model=model,
            settings_path=settings_path,
            system_prompt=system_prompt,
            max_budget_usd=max_budget,
            timeout_s=timeout_s,
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


def _resolve_arguments(template, case_data):
    """Resolve {field} and {field?} placeholders from case input data."""
    import re

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
        "skill": args.skill,
        "skill_args": skill_args or "",
        "execution_mode": config.execution.mode,
        "max_budget_usd": max_budget,
        "timeout_s": timeout_s,
    }
    if effort:
        params["effort"] = effort
    if getattr(args, "mlflow_experiment", None):
        params["mlflow_experiment"] = args.mlflow_experiment
    return params


def _run_single_case(runner, skill_name, case_id, case_ws, output_dir,
                     skill_args_template, model, mlflow_experiment,
                     mlflow_tracking_uri, system_prompt, max_budget, timeout_s,
                     total_cases, index, config=None, hook_env=None,
                     global_hook_outputs=None):
    """Execute and collect results for a single test case.

    Thread-safe: all I/O is to case-specific directories.
    Returns (case_id, result_dict) or (case_id, None) if workspace missing.
    """
    import yaml as _yaml

    if not case_ws.exists():
        print(f"  [{index}/{total_cases}] {case_id}: SKIP (workspace missing)",
              file=sys.stderr)
        return case_id, None

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

    print(f"  [{index}/{total_cases}] {case_id}: /{skill_name} {case_args}",
          file=sys.stderr)

    # Per-case hooks: build case-specific env and run before_each
    case_hook_env = None
    merged_hook_data = {}
    if config and config.hooks and hook_env:
        dataset_path = config.dataset_path
        case_source_dir = str(Path(dataset_path).resolve() / case_id) if dataset_path else ""
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
            inject_hook_env(case_ws, merged_env)
            # Re-check settings_path since we may have just created it
            settings_path = case_settings if case_settings.exists() else settings_path
            # Forward-propagate to after_each hooks
            case_hook_env.update({k: str(v) for k, v in merged_env.items()})

        global_data = (global_hook_outputs or {}).get("data", {})
        case_data_out = case_outputs.get("data", {})
        merged_hook_data = {**global_data, **case_data_out}

    try:
        result = runner.run_skill(
            skill_name=skill_name,
            args=case_args,
            workspace=case_ws,
            model=model,
            settings_path=settings_path,
            system_prompt=system_prompt,
            max_budget_usd=max_budget,
            timeout_s=timeout_s,
        )
    except Exception as exc:
        print(f"    → {case_id}: ERROR ({exc})", file=sys.stderr)
        result = None
    finally:
        # Run after_each hooks (guaranteed, like after_all)
        if config and config.hooks and config.hooks.after_each and case_hook_env:
            log_dir = output_dir / "hooks"
            run_hooks_safe(config.hooks.after_each, env=case_hook_env,
                           cwd=case_ws, log_dir=log_dir,
                           phase_name="after_each", case_id=case_id)

    if result is None:
        return case_id, None

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
          f"${result.cost_usd or 0:.2f}", file=sys.stderr)

    return case_id, case_result


def _execute_per_case(args, config, runner, runner_cls,
                      output_dir, max_budget, timeout_s,
                      model, mlflow_experiment, system_prompt="",
                      skill_args_template=None, eval_params=None,
                      parallelism=None, effort=None, subagent_model=None):
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

    effective_parallelism = min(parallelism, len(case_order)) if parallelism and parallelism > 1 else 1
    parallel_label = f", parallelism={effective_parallelism}" if effective_parallelism > 1 else ""
    print(f"Executing: /{args.skill} (per-case, {len(case_order)} cases{parallel_label})",
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
            with ThreadPoolExecutor(max_workers=effective_parallelism) as pool:
                for i, entry in enumerate(case_order, 1):
                    case_id = entry["case_id"] if isinstance(entry, dict) else entry
                    case_ws = workspace / "cases" / case_id
                    case_runner = runner_cls.from_config(
                        config,
                        log_prefix=f"eval:{case_id}",
                        subagent_model=subagent_model,
                        mlflow_experiment=mlflow_experiment,
                        mlflow_tracking_uri=config.mlflow.tracking_uri,
                        effort=effort,
                    )
                    fut = pool.submit(
                        _run_single_case, case_runner, args.skill, case_id,
                        case_ws, output_dir, skill_args_template, model,
                        mlflow_experiment, config.mlflow.tracking_uri,
                        system_prompt, max_budget, timeout_s,
                        len(case_order), i,
                        config=config, hook_env=hook_env,
                        global_hook_outputs=global_hook_outputs)
                    futures[fut] = case_id

                for fut in as_completed(futures):
                    case_id, result = fut.result()
                    if result is not None:
                        case_results[case_id] = result
        else:
            for i, entry in enumerate(case_order, 1):
                case_id = entry["case_id"] if isinstance(entry, dict) else entry
                case_ws = workspace / "cases" / case_id
                case_id, result = _run_single_case(
                    runner, args.skill, case_id, case_ws, output_dir,
                    skill_args_template, model, mlflow_experiment,
                    config.mlflow.tracking_uri, system_prompt,
                    max_budget, timeout_s, len(case_order), i,
                    config=config, hook_env=hook_env,
                    global_hook_outputs=global_hook_outputs)
                if result is not None:
                    case_results[case_id] = result
    finally:
        if config.hooks.after_all:
            print("Running after_all hooks...", file=sys.stderr)
            run_hooks_safe(config.hooks.after_all, env=hook_env,
                           cwd=Path.cwd(), log_dir=log_dir,
                           phase_name="after_all")

    wall_clock_s = round(time.monotonic() - wall_clock_start, 1)

    # Aggregate metrics across cases
    total_duration = sum(r["duration_s"] for r in case_results.values())
    total_cost = sum(r.get("cost_usd") or 0 for r in case_results.values())
    total_turns = sum(r.get("num_turns") or 0 for r in case_results.values())
    worst_exit = max((r["exit_code"] for r in case_results.values()), default=0)

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
        "cost_usd": round(total_cost, 2),
        "token_usage": agg_tokens or None,
        "num_turns": total_turns or None,
        "per_model_usage": agg_per_model or None,
        "per_model_turns": agg_per_model_turns or None,
        "num_cases": len(case_results),
        "model": model,
        "agent": runner.name,
        "agent_version": getattr(runner, "version", ""),
        "execution_mode": "case",
        "eval_params": eval_params or {},
        "per_case": case_results,
    }
    with open(output_dir / "run_result.json", "w") as f:
        json.dump(run_meta, f, indent=2)
        f.write("\n")

    print(f"EXIT: {worst_exit}")
    print(f"DURATION: {wall_clock_s:.0f}s wall-clock, {total_duration:.0f}s total")
    print(f"COST: ${total_cost:.2f} total")
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
