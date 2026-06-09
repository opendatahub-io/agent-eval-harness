"""Generate self-contained Harbor task packages from an eval.yaml dataset.

Each task package is a directory that any Harbor agent (Claude Code, OpenCode,
Codex, etc.) can run directly via ``harbor run -p <task> --agent <agent>``.
No custom agent wrapper needed — all setup (inputs, tool interception hooks,
project resources) lives in ``environment/``, which Harbor auto-uploads to the
agent workspace at trial start.

Both entry points use this:
- ``/eval-dataset`` → ``skills/eval-dataset/scripts/harbor.py``
  (a thin CLI wrapper around :func:`generate_tasks`),
- ``/eval-run --runner harbor`` → ``agent_eval.harbor.run`` imports and calls it.

Per case it emits::

    <out>/<case-id>/
      task.toml               # env image + verifier config
      instruction.md           # resolved skill command + input context
      tests/
        test.sh                # verifier: judge -> reward bridge
        eval.yaml              # bundled config for the judge engine
      environment/             # auto-uploaded to the agent workspace by Harbor
        input.yaml             # case input
        answers.yaml           # (if present)
        hooks/tools.py         # tool interceptor script (if inputs.tools configured)
        tool_handlers.yaml     # handler config (if inputs.tools configured)
        .claude/settings.json  # PreToolUse hooks + permissions (Claude Code)

Project resources (skills, scripts, .context) are baked into the task image
at a known path and staged into the workspace by the image's entrypoint or
``[environment].workdir`` setup — not by the agent.
"""

import shutil
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig, resolve_arguments
from agent_eval.tools.interception import generate_interception

_TEMPLATES = Path(__file__).resolve().parent / "templates"


def _render(template_name: str, mapping: dict) -> str:
    text = (_TEMPLATES / template_name).read_text()
    for key, value in mapping.items():
        text = text.replace(f"@@{key}@@", str(value))
    return text


def _find_input_file(case_dir: Path):
    for suffix in (".yaml", ".yml", ".json"):
        candidate = case_dir / f"input{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _bundle_eval_config(config_path: Path, judge_model: str | None = None) -> dict:
    """Load the eval.yaml and sanitize it for in-container verification."""
    raw = yaml.safe_load(config_path.read_text()) or {}
    if "dataset" in raw and isinstance(raw["dataset"], dict):
        raw["dataset"] = {**raw["dataset"], "path": ""}
    if judge_model:
        raw.setdefault("models", {})["judge"] = judge_model
    return raw


def _generate_tool_interception(env_dir: Path, config: EvalConfig,
                                config_path: Path, workdir: str) -> None:
    """Generate tool interception artifacts into environment/.

    Delegates to :func:`agent_eval.tools.interception.generate_interception`
    (shared with the local workspace path). Prefers a pre-resolved
    ``tool_handlers.yaml`` alongside ``eval.yaml`` (from ``/eval-analyze``);
    falls back to heuristic extraction.
    """
    resolved = config_path.parent / "tool_handlers.yaml"
    generate_interception(
        env_dir, config,
        hooks_command=f"python3 {workdir}/hooks/tools.py",
        resolved_handlers_path=resolved if resolved.is_file() else None)


def generate_tasks(
    config: EvalConfig,
    config_path: Path,
    out_dir: Path,
    image: str,
    *,
    arguments: str | None = None,
    skill: str | None = None,
    workdir: str = "/workspace",
    cases: list[str] | None = None,
    verifier_timeout: float = 300.0,
    agent_timeout: float = 1800.0,
    judge_model: str | None = None,
) -> list[Path]:
    """Generate one self-contained Harbor task package per dataset case."""
    args_template = arguments if arguments is not None else config.execution.arguments
    skill_name = skill if skill is not None else config.skill

    cases_root = config.resolve_path(config.dataset.path)
    if not cases_root.is_dir():
        raise FileNotFoundError(f"Dataset path not found: {cases_root}")

    case_dirs = sorted(d for d in cases_root.iterdir() if d.is_dir())
    if cases:
        wanted = set(cases)
        case_dirs = [d for d in case_dirs if d.name in wanted]
    if not case_dirs:
        raise ValueError("No matching cases found")

    bundled_cfg = _bundle_eval_config(config_path, judge_model=judge_model)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []

    for case_dir in case_dirs:
        case_id = case_dir.name
        input_file = _find_input_file(case_dir)
        input_data = {}
        if input_file:
            input_data = yaml.safe_load(input_file.read_text()) or {}

        resolved_args = resolve_arguments(args_template, input_data) if args_template else ""
        command = f"/{skill_name} {resolved_args}".strip() if skill_name else resolved_args

        task_dir = out_dir / case_id
        (task_dir / "tests").mkdir(parents=True, exist_ok=True)
        env_dir = task_dir / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)

        # task.toml
        (task_dir / "task.toml").write_text(_render("task.toml.tmpl", {
            "TASK_NAME": f"{config.name or 'eval'}/{case_id}",
            "TASK_DESC": (config.description or config.name or "agent-eval task")[:120],
            "EVAL_NAME": config.name,
            "CASE_ID": case_id,
            "IMAGE": image,
            "VERIFIER_TIMEOUT": verifier_timeout,
            "AGENT_TIMEOUT": agent_timeout,
        }))

        # instruction.md
        input_context = yaml.safe_dump(input_data, sort_keys=False, allow_unicode=True).strip()
        (task_dir / "instruction.md").write_text(_render("instruction.md.tmpl", {
            "COMMAND": command,
            "INPUT_CONTEXT": input_context or "(no input fields)",
        }))

        # tests/test.sh + bundled eval.yaml (verifier)
        (task_dir / "tests" / "test.sh").write_text(_render("test.sh.tmpl", {
            "WORKDIR": workdir,
        }))
        (task_dir / "tests" / "test.sh").chmod(0o755)
        (task_dir / "tests" / "eval.yaml").write_text(
            yaml.safe_dump(bundled_cfg, sort_keys=False, allow_unicode=True))

        # environment/ — auto-uploaded to the workspace by Harbor
        if input_file:
            shutil.copy2(input_file, env_dir / input_file.name)
        answers = case_dir / "answers.yaml"
        if answers.is_file():
            shutil.copy2(answers, env_dir / "answers.yaml")

        # Tool interception (hooks + .claude/settings.json)
        _generate_tool_interception(env_dir, config, Path(config_path), workdir)

        generated.append(task_dir)
        print(f"  {case_id}: {task_dir}")

    return generated
