"""Run an eval on Harbor and map results into the harness run-dir layout.

This is the `/eval-run --runner harbor` path: instead of staging workspaces and
calling a runner per case, it generates Harbor task packages, invokes
`harbor run` (Podman locally / Kubernetes on OpenShift), then maps the Harbor
job's per-case verifier output into the SAME `run_result.json` + `summary.yaml`
shape the local scorer writes — so `report.py`, regression detection, and the
MLflow logger consume Harbor runs unchanged.

Per-case judging happens in-container (the reward bridge as the Harbor verifier),
so this step does not re-run judges; it aggregates their results. Pairwise stays
a suite-level step on top (run separately over two run dirs).
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv before 3p imports
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

from agent_eval.agent.claude_code import CLAUDE_CODE_EFFORTS
from agent_eval.agent.codex import CODEX_EFFORTS
from agent_eval.config import (
    EvalConfig, resolve_plugin_dir, resolve_plugin_skill_roots,
)
from agent_eval.harbor import results as results_mod
from agent_eval.harbor import tasks as tasks_mod
from agent_eval.harbor.reward import _load_score_module

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_dotenv() -> None:
    """Load .env from cwd or any ancestor, if present.

    Uses os.environ.setdefault so explicit exports always win over .env values.
    Supports only simple ``KEY=VALUE``, ``KEY="VALUE"``, and ``KEY='VALUE'`` forms.
    Does NOT handle: ``export KEY=VALUE``, inline comments (``KEY=val # comment``),
    multiline values, or escaped quotes inside values.
    """
    for p in [Path.cwd(), *Path.cwd().parents]:
        env_file = p / ".env"
        if env_file.is_file():
            try:
                for raw in env_file.read_text().splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k:
                        os.environ.setdefault(k, v)
            except OSError:
                pass
            break
# Mapping from eval.yaml runner.type to Harbor agent name.
# runner.type is an agent-eval-harness concept; Harbor --agent is Harbor's.
# Names that match directly (claude-code) need no mapping.
_RUNNER_TO_HARBOR_AGENT = {
    "claude-code": "claude-code",
    "codex": "codex",
    "cli": None,            # CLI runner is generic — user must pass --agent explicitly
    "responses-api": None,  # no Harbor equivalent
}
_DEFAULT_AGENT = "claude-code"
_ENV_IMPORT_PATHS = {
    "podman": "agent_eval.harbor.podman:PodmanEnvironment",
    "kubernetes": "agent_eval.harbor.kubernetes:KubernetesEnvironment",
    "k8s": "agent_eval.harbor.kubernetes:KubernetesEnvironment",
    "openshift": "agent_eval.harbor.kubernetes:KubernetesEnvironment",
}


def _resolve_harbor_skill_roots(config: EvalConfig,
                                agent_name: str) -> list[Path]:
    """Resolve plugin skill roots for Harbor's ``--skill`` option.

    Passing every manifest-declared skill root makes all sibling skills
    available, which is important for orchestrator skills whose dependencies
    are selected dynamically by the agent.

    A plugin that exports no skills is fatal for Codex and tolerated for
    everyone else: Codex consumes a plugin *only* through its skills, so an
    empty one is a misconfiguration that would surface as a model-quality
    failure. A Claude plugin may legitimately ship only commands, agents, or
    hooks, and must not fail a run that never wanted skills from it.
    """
    roots: list[Path] = []
    for configured in config.runner.plugin_dirs:
        path = resolve_plugin_dir(config, configured)
        try:
            roots.extend(resolve_plugin_skill_roots(path))
        except (ValueError, FileNotFoundError):
            if agent_name == "codex":
                raise
            print(f"WARNING: plugin exports no skills; not forwarding {path} "
                  f"to the Harbor {agent_name} agent", file=sys.stderr)
    return roots


def _parse_bind_mount(spec: str) -> dict:
    """Parse ``SOURCE:TARGET[:ro|rw]`` into Harbor's mount schema."""
    parts = spec.rsplit(":", 2)
    if len(parts) == 2:
        source_text, target = parts
        mode = "ro"
    elif len(parts) == 3 and parts[2] in {"ro", "rw"}:
        source_text, target, mode = parts
    else:
        raise ValueError(
            f"Invalid --mount {spec!r}; expected SOURCE:TARGET[:ro|rw]")

    if not source_text.strip():
        raise ValueError("Mount source must not be empty")
    if not target.strip():
        raise ValueError("Mount target must not be empty")

    source = Path(source_text).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Mount source does not exist: {source}")
    if not Path(target).is_absolute():
        raise ValueError(f"Mount target must be absolute: {target}")
    if source == Path("/"):
        raise ValueError("Refusing to mount the host filesystem root")
    # normpath, not resolve(): the target is a container path, and
    # resolving it against the host filesystem would follow unrelated
    # host symlinks. Lexical normalization catches /data/../ spellings.
    if os.path.normpath(target) == "/":
        raise ValueError("Refusing to mount over the container filesystem root")

    mount = {"type": "bind", "source": str(source), "target": target}
    if mode == "ro":
        mount["read_only"] = True
    return mount


# Effort a stock Harbor agent applies when the harness forwards none. Harbor's
# Codex agent declares CliFlag("reasoning_effort", default="high"); its
# claude-code counterpart declares the same flag with no default. Recording the
# agent default keeps run metadata honest about what a trial actually ran at.
_HARBOR_AGENT_DEFAULT_EFFORT = {"codex": "high"}


def _harbor_agent_effort(config: EvalConfig, agent_name: str) -> str | None:
    """Effort value the harness forwards to the Harbor agent, validated per agent.

    Both stock Harbor agents expose a ``reasoning_effort`` kwarg but accept
    different vocabularies. Agents without an effort kwarg return None so the
    harness never forwards a kwarg the agent cannot take. This is what to pass
    to Harbor — use ``_harbor_effective_effort`` for what to record.
    """
    if agent_name == "codex":
        effort = (config.runner.effort
                  or config.runner.settings.get("model_reasoning_effort"))
        valid, label = CODEX_EFFORTS, "Codex"
    elif agent_name == "claude-code":
        effort = config.runner.effort
        valid, label = CLAUDE_CODE_EFFORTS, "claude-code"
    else:
        return None
    if effort and effort not in valid:
        raise ValueError(
            f"Invalid {label} effort '{effort}'. "
            f"Must be one of: {sorted(valid)}")
    return effort or None


def _harbor_effective_effort(config: EvalConfig, agent_name: str) -> str | None:
    """Effort the trial actually runs at, including Harbor's own agent default.

    Forwarding nothing does not mean the trial ran without an effort setting —
    Harbor applies its agent default. Run metadata records this value so a
    matrix cell with no ``effort:`` is not compared against an explicit one as
    though the two were different conditions.
    """
    return (_harbor_agent_effort(config, agent_name)
            or _HARBOR_AGENT_DEFAULT_EFFORT.get(agent_name))


def _harbor_agent_kwargs(config: EvalConfig, agent_name: str) -> list[str]:
    """Translate runner settings that have an equivalent Harbor agent kwarg."""
    effort = _harbor_agent_effort(config, agent_name)
    return [f"reasoning_effort={effort}"] if effort else []


def _resolve_harbor_agent_env(config: EvalConfig) -> dict[str, str]:
    """Resolve eval/runner env for Harbor's agent process."""
    resolved: dict[str, str] = {}
    configured = {**config.execution.env, **config.runner.env}
    for key, value in configured.items():
        if value is None:
            continue
        if isinstance(value, str) and value.startswith("$"):
            host_value = os.environ.get(value[1:])
            if host_value is None:
                continue
            resolved[key] = host_value
        else:
            resolved[key] = str(value)
    return resolved


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _harbor_agent_env_args(config: EvalConfig) -> tuple[list[str], dict[str, str]]:
    """Build value-free Harbor argv plus its process-environment carriers.

    Harbor resolves ``${NAME}`` templates in AgentConfig.env immediately before
    it creates the agent.  Passing an indirect template therefore preserves
    ``--agent-env`` semantics without putting the resolved value in
    ``/proc/<pid>/cmdline``.  Same-UID processes can still inspect the child
    environment; this prevents argv/log disclosure, not host credential
    isolation.
    """
    args: list[str] = []
    child_env: dict[str, str] = {}
    for index, (key, value) in enumerate(_resolve_harbor_agent_env(config).items()):
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(f"Invalid agent environment variable name: {key!r}")
        carrier = f"AGENT_EVAL_HARBOR_AGENT_ENV_{index}"
        child_env[carrier] = value
        args += ["--agent-env", f"{key}=${{{carrier}}}"]
    return args, child_env


def _display_command(cmd: list[str]) -> str:
    """Format a Harbor command without exposing agent environment values."""
    shown = list(cmd)
    for index, value in enumerate(shown):
        if index and shown[index - 1] in {"--agent-env", "--verifier-env"}:
            key = value.partition("=")[0]
            shown[index] = f"{key}=<redacted>"
    return " ".join(shown)


def _judge_types(config: EvalConfig) -> dict:
    """Map judge name -> type, mirroring score.load_judges' discrimination."""
    types = {}
    for jc in config.judges:
        if jc.name == "pairwise":
            continue
        if jc.check:
            t = "check"
        elif jc.prompt or jc.prompt_file:
            t = "llm"
        elif jc.module and jc.function:
            t = "code"
        elif jc.builtin:
            t = "builtin"
        else:
            t = "check"
        types[jc.name] = t
    return types


def build_summary(parsed_job: dict, config: EvalConfig) -> dict:
    """Map a parsed Harbor job into the harness summary shape.

    Returns ``{"judges": {name: {mean, pass_rate}}, "per_case": {case_id:
    {judge: {value, rationale, judge_type}}}}`` — identical to what
    ``score.py``'s ``cmd_judges`` writes, so downstream code is agnostic to
    whether judging ran locally or in a Harbor verifier.
    """
    types = _judge_types(config)
    per_case: dict = {}
    agg_values: dict = {}

    for trial in parsed_job["trials"]:
        case_judges = {}
        for name, rec in trial.get("per_judge", {}).items():
            value = rec.get("value")
            case_judges[name] = {
                "value": value,
                "rationale": rec.get("rationale", "") or rec.get("error", ""),
                "judge_type": types.get(name) or rec.get("judge_type", "check"),
            }
            if value is not None:
                agg_values.setdefault(name, []).append(value)
        per_case[trial["case_id"]] = case_judges

    judges: dict = {}
    for name, vals in agg_values.items():
        if vals and all(isinstance(v, bool) for v in vals):
            rate = sum(vals) / len(vals)
            judges[name] = {"mean": rate, "pass_rate": rate}
        elif vals and all(isinstance(v, (int, float)) for v in vals):
            judges[name] = {"mean": sum(vals) / len(vals), "pass_rate": None}
        else:
            judges[name] = {"mean": None, "pass_rate": None}

    return {"judges": judges, "per_case": per_case}


def _purge_task_packages(tasks_dir: Path) -> None:
    """Remove existing task packages before regeneration.

    Harbor runs every package under ``-p``, so a package surviving a
    selective regeneration would execute with stale config/image and
    contaminate the reported run. Non-package files are left alone.
    """
    if not tasks_dir.is_dir():
        return
    for stale in sorted(d for d in tasks_dir.iterdir()
                        if d.is_dir() and (d / "task.toml").is_file()):
        shutil.rmtree(stale)


def _count_task_packages(tasks_dir: Path) -> int:
    """Count Harbor task packages (subdirs with a task.toml) under tasks_dir."""
    if not tasks_dir.is_dir():
        return 0
    return sum(1 for d in tasks_dir.iterdir()
               if d.is_dir() and (d / "task.toml").is_file())


def _validate_task_package_reuse(tasks_dir: Path, config: EvalConfig, *,
                                 no_llm_judges: bool = False) -> None:
    """Reject pre-generated packages whose provenance mismatches this run.

    The RESERVED ``thresholds.simulator`` key gates the run-level simulator
    block, never a judge — it must not demand a bundled judge named
    'simulator'.
    """
    required = set(config.thresholds or {}) - {"simulator"}
    requested_mode = "deterministic-only" if no_llm_judges else "full"
    for task_dir in sorted(d for d in tasks_dir.iterdir() if d.is_dir()
                           and (d / "task.toml").is_file()):
        try:
            task = tomllib.loads((task_dir / "task.toml").read_text())
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(
                f"Cannot validate pre-generated Harbor task {task_dir}: {exc}") from exc
        metadata = task.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError(
                f"Pre-generated Harbor task {task_dir} has invalid metadata; "
                "regenerate it with --regenerate --image IMAGE")
        eval_name = metadata.get("eval_name")
        if eval_name is not None and config.name and eval_name != config.name:
            raise ValueError(
                f"Pre-generated Harbor task {task_dir} was generated for eval "
                f"{eval_name!r}, not {config.name!r}; point --tasks-dir at "
                "that eval's packages or pass --regenerate --image IMAGE")
        mode = metadata.get("judge_mode")
        if mode not in {None, "full", "deterministic-only"}:
            raise ValueError(
                f"Pre-generated Harbor task {task_dir} has unknown judge_mode "
                f"{mode!r}; regenerate it with --regenerate --image IMAGE")
        if (mode or "full") != requested_mode:
            if mode == "deterministic-only":
                raise ValueError(
                    f"Pre-generated Harbor task {task_dir} was built with "
                    "--no-llm-judges and cannot be reused for a full run; "
                    "pass --regenerate --image IMAGE")
            raise ValueError(
                f"Pre-generated Harbor task {task_dir} bundles model judges; "
                "reusing it with --no-llm-judges would still run them. Pass "
                "--regenerate --image IMAGE to rebuild deterministic-only "
                "packages")
        bundled_judges = set()
        for bundled_path in task_dir.rglob("eval.yaml"):
            try:
                bundled = yaml.safe_load(bundled_path.read_text()) or {}
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                raise ValueError(
                    f"Cannot validate bundled config {bundled_path}: {exc}") from exc
            bundled_judges.update(
                judge.get("name") for judge in (bundled.get("judges") or [])
                if isinstance(judge, dict) and judge.get("name"))
        missing = sorted(required - bundled_judges)
        if missing:
            raise ValueError(
                f"Pre-generated Harbor task {task_dir} is missing thresholded "
                f"judge(s): {', '.join(missing)}; regenerate it with "
                "--regenerate --image IMAGE")


def _load_report_module():
    """Load report.py from the eval-run skill (by path)."""
    path = _REPO_ROOT / "skills" / "eval-run" / "scripts" / "report.py"
    spec = importlib.util.spec_from_file_location("agent_eval_report", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _copy_case_artifacts(parsed: dict, output_dir: Path,
                         config: EvalConfig) -> None:
    """Copy configured outputs from Harbor trials into the harness run dir.

    A task verifier copies every ``outputs[].path`` to
    ``/logs/verifier/<path>``. Harbor downloads that as
    ``<trial>/verifier/<path>``; mirror the same paths beneath the harness's
    conventional ``cases/<case>/<configured path>`` location used by local
    collection and report rendering.
    """
    import shutil
    for trial in parsed["trials"]:
        trial_path = Path(trial.get("trial_path", ""))
        verifier_dirs = [trial_path / "verifier"]
        steps_dir = trial_path / "steps"
        if steps_dir.is_dir():
            step_order = {
                step.id: index
                for index, step in enumerate(config.execution.steps)
            }
            verifier_dirs.extend(
                step / "verifier"
                for step in sorted(
                    (candidate for candidate in steps_dir.iterdir()
                     if candidate.is_dir()),
                    key=lambda candidate: (
                        step_order.get(candidate.name, len(step_order)),
                        candidate.name)))

        case_root = output_dir / "cases" / trial["case_id"]
        for configured_output in config.outputs:
            if not configured_output.path:
                continue
            # Later pipeline steps take precedence when the same configured
            # output exists more than once.
            sources = [
                verifier_dir / configured_output.path
                for verifier_dir in verifier_dirs
                if (verifier_dir / configured_output.path).exists()
            ]
            if not sources:
                continue
            src = sources[-1]
            dst = case_root / configured_output.path
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_dir():
                shutil.rmtree(dst)
            elif dst.exists():
                dst.unlink()
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)


def _note_min_alpha_skipped(thresholds) -> bool:
    """One combined stderr notice when reliability gates cannot run here.

    Harbor aggregation carries no per-sample stability data and no
    judge-panel data, so ``min_alpha`` and ``min_panel_alpha`` thresholds
    are skipped (``include_irr=False`` covers both) rather than regressing
    every run. (Panels still EXECUTE in-container — the in-container
    verifier runs score_cases, so you pay m× judge cost — but the
    cross-case panel alpha is not aggregated on this path yet.)
    Consequence tiers never inject on this path either — the detector
    receives raw ``config.thresholds``, not ``effective_thresholds()``.
    """
    skipped = sorted({key
                      for t in (thresholds or {}).values()
                      if isinstance(t, dict)
                      for key in ("min_alpha", "min_panel_alpha")
                      if key in t})
    if skipped:
        print(f"NOTE: reliability gates ({', '.join(skipped)}) skipped on "
              "this execution path: no sampling stability data or "
              "judge-panel data in aggregated results", file=sys.stderr)
        return True
    return False


def _strip_simulator_thresholds(thresholds) -> dict:
    """Drop the reserved ``thresholds.simulator`` key, with a notice.

    Harbor aggregation carries no hook-ledger data, so the simulator gates
    (``max_fallback_rate``/``min_gold_agreement``) cannot be evaluated here
    — stripping with a stderr notice extends the reliability-gate
    skip-notice pattern (:func:`_note_min_alpha_skipped`) instead of
    regressing every containerized run as configured-but-unavailable.
    """
    out = {k: v for k, v in (thresholds or {}).items() if k != "simulator"}
    if "simulator" in (thresholds or {}):
        print("NOTE: thresholds.simulator is not evaluated on the Harbor "
              "path (no simulator ledger aggregation)", file=sys.stderr)
    return out


def _write_report(config_path: Path, output_dir: Path, summary: dict,
                  run_meta: dict) -> None:
    """Render report.html with the same generator the local path uses."""
    try:
        raw_cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        # Resolve dataset.path to absolute (report renders case inputs from it).
        ds = raw_cfg.get("dataset")
        if isinstance(ds, dict) and ds.get("path") and not Path(ds["path"]).is_absolute():
            ds["path"] = str((Path(config_path).resolve().parent / ds["path"]).resolve())
        report = _load_report_module()
        html = report.generate_report(
            config=raw_cfg, summary=summary, run_result=run_meta,
            run_dir=output_dir, review=None, baseline_dir=None,
            baseline_summary=None, baseline_result=None,
            reward_cfg=report.load_reward_cfg(config_path),
        )
        (output_dir / "report.html").write_text(html)
        print(f"report: {output_dir}/report.html")
    except Exception as exc:  # report is best-effort; don't fail the run
        print(f"WARNING: report generation failed: {exc}", file=sys.stderr)


def run_eval_on_harbor(
    config_path: Path,
    *,
    image: str | None = None,
    model: str,
    output_dir: Path,
    tasks_dir: Path,
    jobs_dir: Path,
    arguments: str | None = None,
    skill: str | None = None,
    judge_model: str | None = None,
    cases: list[str] | None = None,
    n_concurrent: int = 1,
    workdir: str = "/workspace",
    agent_name: str | None = None,
    env_import_path: str | None = None,
    mounts: list[dict] | None = None,
    no_llm_judges: bool = False,
    cpus: int | None = None,
    memory_mb: int | None = None,
    harbor_bin: str = "harbor",
    regenerate: bool = False,
) -> int:
    """Run an eval on Harbor and map results. Returns an exit code
    (non-zero if regression thresholds are violated).

    Tasks: if ``tasks_dir`` already holds Harbor task packages (e.g. emitted by
    ``/eval-dataset``), they are used as-is; otherwise they're generated now
    (one-shot convenience). ``regenerate=True`` forces regeneration.
    """
    config = EvalConfig.from_yaml(config_path)

    # Resolve Harbor agent from eval.yaml runner.type if not explicitly passed.
    if not agent_name:
        mapped = _RUNNER_TO_HARBOR_AGENT.get(config.runner.type)
        if mapped:
            agent_name = mapped
        elif config.runner.type in _RUNNER_TO_HARBOR_AGENT:
            raise ValueError(
                f"runner.type '{config.runner.type}' in eval.yaml has no Harbor "
                f"agent equivalent. Pass --agent explicitly (e.g. --agent opencode).")
        else:
            agent_name = config.runner.type

    if no_llm_judges:
        # A threshold naming a model judge cannot be satisfied by a
        # deterministic-only run, whether packages are generated or reused.
        filtered = tasks_mod._bundle_eval_config(
            Path(config_path), judge_model=judge_model, no_llm_judges=True)
        kept_names = {
            judge.get("name") for judge in filtered.get("judges", [])
        }
        # 'simulator' is the reserved simulator-gate key, not a judge name —
        # dropping model judges never removes it.
        removed_thresholds = sorted(
            name for name in config.thresholds
            if name != "simulator" and name not in kept_names)
        if removed_thresholds:
            raise ValueError(
                "--no-llm-judges would skip thresholded judge(s): "
                f"{', '.join(removed_thresholds)}. Remove those thresholds "
                "or run the model judges.")

    # 1. Use pre-generated task packages if present; else generate them.
    existing = _count_task_packages(tasks_dir)
    if existing and not regenerate:
        ignored = []
        if arguments is not None:
            ignored.append("--arguments")
        if skill is not None:
            ignored.append("--skill")
        if cases:
            ignored.append("--cases")
        if judge_model is not None:
            ignored.append("--judge-model")
        if ignored:
            raise ValueError(
                "Pre-generated Harbor tasks cannot apply generation options "
                f"{', '.join(ignored)}; pass --regenerate --image IMAGE to "
                "rebuild them")
        _validate_task_package_reuse(
            tasks_dir, config, no_llm_judges=no_llm_judges)
        print(f"Using {existing} pre-generated task package(s) in {tasks_dir} "
              f"(skipping generation; --regenerate to force)", file=sys.stderr)
    else:
        if not image:
            raise ValueError(
                ("--regenerate discards the existing task packages and needs "
                 "--image to rebuild them. " if existing else
                 "No tasks in --tasks-dir and no --image to generate them. ")
                + "Either pre-generate with /eval-dataset (scripts/harbor.py) "
                "or pass --image.")
        if regenerate:
            _purge_task_packages(tasks_dir)
        tasks_mod.generate_tasks(
            config, Path(config_path), tasks_dir, image,
            arguments=arguments, skill=skill, workdir=workdir, cases=cases,
            judge_model=judge_model, no_llm_judges=no_llm_judges,
            agent_name=agent_name,
        )

    # 2. Run on Harbor (one job over the tasks dir).
    if harbor_bin == "harbor":
        # Prefer the harbor CLI installed alongside this interpreter: the
        # harness pins harbor's version in its own environment, and a stale
        # global `harbor` on PATH would silently run a different (possibly
        # incompatible) agent bootstrap than the pinned one.
        sibling = Path(sys.executable).parent / "harbor"
        if sibling.is_file():
            harbor_bin = str(sibling)
    cmd = [
        harbor_bin, "run", "-p", str(tasks_dir),
        "-a", agent_name, "-m", model,
        "-n", str(n_concurrent), "-o", str(jobs_dir),
    ]
    # --skill is agent-agnostic: skills_dir is a BaseAgent constructor argument
    # (harbor/agents/base.py), and both stock agents copy it into the location
    # their CLI reads. Gating this on one agent would silently run a
    # claude-code trial without the very skills under test.
    for root in _resolve_harbor_skill_roots(config, agent_name):
        cmd += ["--skill", str(root)]
    for kwarg in _harbor_agent_kwargs(config, agent_name):
        cmd += ["--agent-kwarg", kwarg]
    agent_env_args, harbor_env = _harbor_agent_env_args(config)
    cmd += agent_env_args
    if mounts:
        if env_import_path != _ENV_IMPORT_PATHS["podman"]:
            raise ValueError(
                "Host bind mounts are supported only by the Podman Harbor "
                "environment; Kubernetes/OpenShift require cluster-visible storage")
        cmd += ["--mounts", json.dumps(mounts, separators=(",", ":"))]
    if cpus is not None:
        cmd += ["--override-cpus", str(cpus)]
    if memory_mb is not None:
        cmd += ["--override-memory-mb", str(memory_mb)]
    if env_import_path:
        cmd += ["--environment-import-path", env_import_path]
    print(f"harbor: {_display_command(cmd)}", file=sys.stderr)
    import signal
    child_env = os.environ.copy()
    child_env.update(harbor_env)
    proc = subprocess.Popen(cmd, env=child_env)
    def _forward_signal(signum, frame):
        try:
            proc.send_signal(signum)
        except (ProcessLookupError, OSError):
            # Harbor may exit in the window between the signal arriving and
            # the forward; the wait()/result mapping below must still run.
            pass
    prev_term = signal.signal(signal.SIGTERM, _forward_signal)
    prev_int = signal.signal(signal.SIGINT, _forward_signal)
    try:
        proc.wait()
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
    if proc.returncode != 0:
        print(f"harbor run exited {proc.returncode}", file=sys.stderr)
        return proc.returncode

    # 3. Locate the job dir Harbor just wrote (newest under jobs_dir).
    # Harbor can exit 0 without creating jobs_dir (e.g. an empty task list),
    # so a missing directory is a reportable condition, not a traceback.
    job_dirs = sorted((d for d in jobs_dir.iterdir() if d.is_dir()),
                      key=lambda d: d.stat().st_mtime) if jobs_dir.is_dir() else []
    if not job_dirs:
        print(f"No Harbor job dir under {jobs_dir}", file=sys.stderr)
        return 1
    parsed = results_mod.parse_job(job_dirs[-1])

    # 4. Map into the harness run-dir layout.
    summary = build_summary(parsed, config)
    output_dir.mkdir(parents=True, exist_ok=True)
    _copy_case_artifacts(parsed, output_dir, config)
    run_meta = {
        "exit_code": 0 if parsed["n_errored"] == 0 else 1,
        "execution_mode": "harbor",
        "agent": f"harbor:{agent_name}",
        "effort": _harbor_effective_effort(config, agent_name),
        "agent_version": parsed.get("agent_version"),
        "model": model,
        "num_cases": parsed["n_completed"],
        "num_turns": parsed.get("num_turns"),
        "duration_s": parsed.get("duration_s"),
        "mean_reward": parsed["mean_reward"],
        "cost_usd": parsed.get("cost_usd"),
        "token_usage": parsed.get("token_usage"),
        "per_model_usage": parsed.get("per_model_usage"),
        "harbor_job_dir": parsed["job_dir"],
        "n_infra_errors": parsed.get("n_infra_errors", 0),
        "infra_errors": parsed.get("infra_errors", []),
        "n_trial_errors": parsed.get("n_trial_errors", 0),
        "trial_errors": parsed.get("trial_errors", []),
        "n_unjudged_steps": parsed.get("n_unjudged_steps", 0),
        "unjudged_steps": parsed.get("unjudged_steps", []),
    }
    (output_dir / "run_result.json").write_text(json.dumps(run_meta, indent=2) + "\n")
    (output_dir / "summary.yaml").write_text(
        yaml.safe_dump({"run_id": output_dir.name, **summary},
                       sort_keys=False, allow_unicode=True))

    # 4b. Generate the HTML report (same renderer as the local path).
    _write_report(config_path, output_dir, summary, run_meta)

    # 5. Surface Harbor infra/trial errors first, so they appear even when the
    # run also regresses (the regression check below early-returns).
    infra = parsed.get("infra_errors", [])
    if infra:
        print(f"INFRA-ERRORS: {len(infra)} step(s) had no verifier reward "
              f"(transient k8s exec; excluded from judge means, not scored 0):",
              file=sys.stderr)
        for case_id, step in infra:
            print(f"  [{case_id}] {step}", file=sys.stderr)
    trial_errs = parsed.get("trial_errors", [])
    if trial_errs:
        print(f"TRIAL-ERRORS: {len(trial_errs)} trial(s) failed before producing "
              f"a reward (e.g. pod never Ready):", file=sys.stderr)
        for case_id, reason in trial_errs:
            print(f"  [{case_id}] {reason}", file=sys.stderr)
    unjudged = parsed.get("unjudged_steps", [])
    if unjudged:
        print(f"UNJUDGED: {len(unjudged)} step(s) recorded no judgement "
              f"(no judge targeted them; excluded from the reward, not scored 0):",
              file=sys.stderr)
        for case_id, step in unjudged:
            print(f"  [{case_id}] {step}", file=sys.stderr)

    # 6. Regression detection (suite-level), mirroring score.py regression.
    # Raw config.thresholds on purpose (never effective_thresholds()):
    # consequence tiers must not inject min_alpha on this path — Harbor
    # aggregation carries no per-sample stability data, so a
    # consequence-tagged judge must not regress a Harbor run. Explicit
    # min_alpha and min_panel_alpha keys are skipped via include_irr=False,
    # with one combined notice. The reserved thresholds.simulator key is
    # STRIPPED with its own notice — Harbor aggregation carries no
    # hook-ledger data, so the simulator gates cannot be evaluated here.
    score = _load_score_module()
    _note_min_alpha_skipped(config.thresholds)
    thresholds = _strip_simulator_thresholds(config.thresholds)
    regressions = score.detect_regressions(summary["judges"], thresholds,
                                           include_irr=False)
    if regressions:
        print(f"REGRESSIONS: {len(regressions)} detected", file=sys.stderr)
        for r in regressions:
            print(f"  [{r.judge_name}] {r.metric}: {r.baseline_value} -> {r.current_value}",
                  file=sys.stderr)
        return 1
    if parsed["mean_reward"] is None:
        print("NO-SCORES: Harbor completed without producing any numeric reward",
              file=sys.stderr)
        return 1
    print(f"Mapped {parsed['n_completed']} case(s) → {output_dir}/summary.yaml "
          f"(mean_reward={parsed['mean_reward']}); "
          f"REGRESSIONS: 0; INFRA-ERRORS: {len(infra)}; TRIAL-ERRORS: {len(trial_errs)}; "
          f"UNJUDGED: {len(unjudged)}")
    return 0


def main() -> None:
    _load_dotenv()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True)
    p.add_argument("--image", default=None,
                   help="Task image (required only when generating tasks; "
                        "pre-generated tasks already reference their image)")
    p.add_argument("--model", required=True)
    p.add_argument("--output", required=True, help="Harness run dir to write")
    p.add_argument("--tasks-dir", required=True)
    p.add_argument("--jobs-dir", required=True)
    p.add_argument("--arguments", default=None)
    p.add_argument("--skill", default=None)
    p.add_argument("--judge-model", default=None)
    p.add_argument(
        "--no-llm-judges", action="store_true",
        help="Skip model-calling judges inside Harbor; run deterministic judges only")
    p.add_argument("--cases", nargs="*", default=None)
    p.add_argument("--n-concurrent", type=int, default=1)
    p.add_argument("--cpus", type=int, default=None,
                   help="Hard CPU limit per Harbor environment")
    p.add_argument("--memory-mb", type=int, default=None,
                   help="Hard memory limit in MiB per Harbor environment")
    p.add_argument("--workdir", default="/workspace")
    p.add_argument("--agent", default=None,
                   help="Harbor agent name (default: from runner.type in eval.yaml; "
                        "e.g. claude-code, opencode)")
    p.add_argument("--env", default="kubernetes",
                   choices=["podman", "kubernetes", "k8s", "openshift"],
                   help="Execution environment (default: kubernetes)")
    p.add_argument("--environment-import-path", default=None,
                   help="Custom Harbor environment import path (overrides --env)")
    p.add_argument(
        "--mount", action="append", default=[], metavar="SOURCE:TARGET[:ro|rw]",
        help="Bind-mount host data into each Harbor environment (repeatable; "
             "read-only by default)")
    p.add_argument("--regenerate", action="store_true",
                   help="Regenerate task packages even if --tasks-dir already has them "
                        "(default: reuse pre-generated tasks, e.g. from /eval-dataset)")
    args = p.parse_args()

    env_import = args.environment_import_path or _ENV_IMPORT_PATHS.get(args.env)
    mounts = [_parse_bind_mount(spec) for spec in args.mount]

    code = run_eval_on_harbor(
        Path(args.config), image=args.image, model=args.model,
        output_dir=Path(args.output), tasks_dir=Path(args.tasks_dir),
        jobs_dir=Path(args.jobs_dir), arguments=args.arguments, skill=args.skill,
        judge_model=args.judge_model, cases=args.cases,
        n_concurrent=args.n_concurrent, workdir=args.workdir,
        agent_name=args.agent,
        env_import_path=env_import,
        mounts=mounts,
        no_llm_judges=args.no_llm_judges,
        cpus=args.cpus,
        memory_mb=args.memory_mb,
        regenerate=args.regenerate,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
