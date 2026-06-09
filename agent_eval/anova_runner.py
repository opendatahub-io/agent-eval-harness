"""Production ``run_fn`` bridge for the eval-anova matrix skill.

``skills/eval-anova/scripts/orchestrate.py::run_cell`` needs a caller-supplied
``run_fn(case_id, **runner_kwargs, **run_skill_kwargs) -> {judge_name: value}``.
This module provides that bridge by reusing the existing harness pieces — the
same pattern as ``agent_eval/evalhub/adapter.py``:

    runner.run_skill(...)            # run the agent on the case
    collect._collect_modified_files  # capture in-place repo edits as a diff
    score.load_judges / score_cases  # score with the eval.yaml judges

It supports two workspace modes:

* **repo mode** (``repo_clone`` + ``base_commit`` given): each cell runs in a
  fresh ``git worktree`` detached at the base commit, so the agent edits real
  files and ``git diff HEAD`` captures its work. Used by repo-editing datasets
  like harbor-maas-v1.
* **fresh mode** (no repo): each cell runs in an empty git-initialised
  workspace (mirrors ``workspace.py``) so artifact/modified-file collection
  still works.

Real run artifacts land under ``runs_dir`` (caller passes a gitignored
``eval/runs/...`` path); nothing here writes to tracked locations.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import yaml

from agent_eval.agent import RUNNERS
from agent_eval.evalhub.adapter import _resolve_arguments

# Resolve symlinks: agent_eval may be imported through a symlinked skills dir
# (conftest puts those on sys.path), so __file__ can point inside skills/.
# .resolve() follows the link to the real repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent

_MODULE_CACHE: dict[str, Any] = {}


def _load_skill_module(name: str):
    """Load a skills/eval-run/scripts/<name>.py module by absolute path (cached)."""
    if name in _MODULE_CACHE:
        return _MODULE_CACHE[name]
    path = _REPO_ROOT / "skills" / "eval-run" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_anova_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _MODULE_CACHE[name] = mod
    return mod


def _score_module():
    """The eval-run score module the bridge scores with (tests patch this)."""
    return _load_skill_module("score")


def _git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)


def _sanitize(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in str(s))


def _copy_modified(changed: list[tuple[str, Path]], dest: Path) -> int:
    """Copy collected (rel, abs) modified files into dest/<rel>. Returns count."""
    n = 0
    for rel, abs_path in changed:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(abs_path, target)
            n += 1
        except OSError:
            pass
    return n


def make_run_fn(
    eval_config: Any,
    *,
    runs_dir: Path,
    repo_clone: Path | None = None,
    base_commit: str | None = None,
    project_root: Path | None = None,
    timeout_s: int = 900,
    max_budget_usd: float = 5.0,
    log_prefix: str | None = None,
    normalize: Callable[[str, Any, str], Any] | None = None,
    prepare_workspace: Callable[[Path, str, Path], None] | None = None,
) -> Callable[..., dict[str, Any]]:
    """Build the ``run_fn`` closure that ``run_cell`` calls per matrix cell.

    The returned callable has signature ``run_fn(case_id, *, model, effort=None,
    **_)`` (matching what ``apply_condition`` produces) and returns a flat
    ``{judge_name: value}`` dict ready for ``composite_score``.

    normalize(judge_name, value, judge_type) -> value lets the caller rescale a
    judge (e.g. a 1-5 LLM score to [0,1]); defaults to identity.
    """
    runs_dir = Path(runs_dir)
    repo_clone = Path(repo_clone) if repo_clone else None
    project_root = Path(project_root) if project_root else Path.cwd()
    score = _score_module()
    collect = _load_skill_module("collect")
    judges = score.load_judges(eval_config, project_root=project_root)
    runner_cls = RUNNERS[eval_config.runner.type]
    dataset_root = eval_config.resolve_path(eval_config.dataset_path).resolve()
    norm = normalize or (lambda name, value, jtype: value)

    def _make_workspace(cond_slug: str, case_id: str) -> Path:
        ws = runs_dir / "work" / f"{cond_slug}-{case_id}"
        if ws.exists():
            if repo_clone:
                _git("-C", str(repo_clone), "worktree", "remove", "--force", str(ws))
            shutil.rmtree(ws, ignore_errors=True)
        ws.parent.mkdir(parents=True, exist_ok=True)
        if repo_clone and base_commit:
            res = _git("-C", str(repo_clone), "worktree", "add",
                       "--detach", str(ws), base_commit)
            if res.returncode != 0:
                raise RuntimeError(f"git worktree add failed: {res.stderr}")
        else:
            # Fresh workspace with a committed baseline so git-diff collection works.
            ws.mkdir(parents=True, exist_ok=True)
            _git("-C", str(ws), "init", "-q")
            (ws / ".gitkeep").write_text("")
            _git("-C", str(ws), "add", "-A")
            _git("-C", str(ws), "-c", "user.email=eval@local",
                 "-c", "user.name=eval", "commit", "-qm", "initial")
        return ws

    def _commit_baseline(ws: Path) -> None:
        """Commit the current tree so `git diff HEAD` later captures only the
        agent's edits (not any prepare_workspace setup)."""
        _git("-C", str(ws), "add", "-A")
        _git("-C", str(ws), "-c", "user.email=eval@local", "-c", "user.name=eval",
             "commit", "-qm", "pre-agent baseline", "--allow-empty")

    def _teardown_workspace(ws: Path) -> None:
        if repo_clone and base_commit:
            _git("-C", str(repo_clone), "worktree", "remove", "--force", str(ws))
        shutil.rmtree(ws, ignore_errors=True)

    def run_fn(case_id: str, *, model: str | None = None,
               effort: str | None = None, **_: Any) -> dict[str, Any]:
        input_data = yaml.safe_load(
            (dataset_root / case_id / "input.yaml").read_text())
        args = _resolve_arguments(eval_config.execution.arguments, input_data)

        cond_slug = _sanitize(model or "default")
        if effort:
            cond_slug += f"-{_sanitize(effort)}"
        cell_case_dir = runs_dir / "cells" / cond_slug / case_id
        cell_case_dir.mkdir(parents=True, exist_ok=True)

        ws = _make_workspace(cond_slug, case_id)
        try:
            if prepare_workspace:
                prepare_workspace(ws, case_id, dataset_root)
            _commit_baseline(ws)
            runner = runner_cls.from_config(
                eval_config, log_prefix=log_prefix, effort=effort)
            result = runner.run_skill(
                skill_name=eval_config.skill or "",
                args=args,
                workspace=ws,
                model=model,
                max_budget_usd=max_budget_usd,
                timeout_s=timeout_s,
            )
            changed = collect._collect_modified_files(ws, eval_config)
            n_modified = _copy_modified(changed, cell_case_dir / "_modified")
            (cell_case_dir / "stderr.log").write_text(result.stderr or "")
            (cell_case_dir / "stdout.log").write_text(result.stdout or "")
        finally:
            _teardown_workspace(ws)

        scored = score.score_cases(judges, [cell_case_dir], eval_config, run_id=None)
        per = scored["per_case"].get(case_id, {})

        flat: dict[str, Any] = {}
        for name, scorer, _cond, jtype in judges:
            entry = per.get(name, {})
            flat[name] = norm(name, entry.get("value"), jtype)

        print(f"    [{case_id} / {model}{('/'+effort) if effort else ''}] "
              f"exit={result.exit_code} modified={n_modified} "
              f"judges={flat} {result.duration_s:.0f}s ${result.cost_usd or 0:.3f}",
              flush=True)
        return flat

    return run_fn
