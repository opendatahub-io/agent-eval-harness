"""Every skill script must be able to reach the .eval-venv third-party deps.

Three invariants, each of which has silently regressed before:

1. Each ``skills/<skill>/scripts/`` directory carries an ``agent_eval`` symlink
   back to the source package. Without it ``import agent_eval`` raises
   ModuleNotFoundError when the script is invoked the documented way
   (``python3 ${CLAUDE_SKILL_DIR}/scripts/foo.py``), because sys.path[0] is the
   script's own directory.
2. ``import agent_eval._bootstrap`` precedes the first *third-party* top-level
   import in any script that has one. Bootstrap is what puts the venv's
   site-packages on sys.path (or re-execs into the venv interpreter on an ABI
   mismatch), so an earlier ``import yaml`` resolves against the system
   interpreter — the deps look missing even though the venv has them.
3. The activation sentinel does not leak into spawned python children. It exists
   to survive ``os.execv`` within one process; a child is a fresh entry that has
   to activate the venv itself.

Stdlib imports before the bootstrap line are fine, and so is a ``sys.path``
insert that the bootstrap import itself depends on (``scripts/discover.py``).
"""

import ast
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

# Modules that live in the venv, not the interpreter. `agent_eval` counts: it is
# imported from the source tree, but its submodules pull yaml/mlflow/anthropic.
_STDLIB = getattr(sys, "stdlib_module_names", frozenset())

# scripts/ensure_deps.py is the SessionStart hook that *creates* .eval-venv, so it
# runs before there is a venv to activate — importing _bootstrap there would be
# circular. It guards its own `import yaml` with a _parse_yaml_minimal fallback.
_BOOTSTRAP_EXEMPT = {"scripts/ensure_deps.py"}


def _skill_script_dirs():
    return sorted(p for p in SKILLS_DIR.glob("*/scripts") if p.is_dir())


def _package_entry_points():
    """agent_eval modules with a __main__ block — SKILL.md invokes some of them
    directly by path (e.g. `python3 .../scripts/agent_eval/state.py`), so they are
    entry points too, not just importable modules."""
    out = []
    for f in sorted((REPO_ROOT / "agent_eval").rglob("*.py")):
        if '__name__ == "__main__"' in f.read_text():
            out.append(f)
    return out


def _python_scripts():
    """Every harness-owned .py entry point, excluding the agent_eval symlinks."""
    files = [f for d in _skill_script_dirs() for f in sorted(d.glob("*.py"))]
    files += sorted((REPO_ROOT / "scripts").glob("*.py"))
    files += _package_entry_points()
    return files


def _rel(path):
    return str(path.relative_to(REPO_ROOT))


def _imports(nodes):
    """(module_root, lineno) for each import in `nodes`, in source order."""
    out = []
    for node in nodes:
        if isinstance(node, ast.Import):
            out += [(a.name.split(".")[0], node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` (level > 0) is never third-party.
            if node.level == 0 and node.module:
                out.append((node.module.split(".")[0], node.lineno))
    return out


def _toplevel_imports(tree):
    return _imports(tree.body)


def _all_imports(tree):
    """Including deferred ones inside functions — a lazy `import yaml` still
    needs the venv on sys.path by the time it runs."""
    return _imports(ast.walk(tree))


def _is_third_party(root, script):
    """Neither stdlib nor a sibling module importable from the script's own dir.

    Skill scripts import each other by bare name (sys.path[0] is their shared
    directory, e.g. orchestrate.py's `from analyze import analyze_runs`); those
    live in the source tree, not the venv.
    """
    if root in _STDLIB or root == "__future__":
        return False
    return not (script.parent / f"{root}.py").exists()


@pytest.mark.parametrize("scripts_dir", _skill_script_dirs(), ids=_rel)
def test_skill_scripts_dir_has_agent_eval_symlink(scripts_dir):
    link = scripts_dir / "agent_eval"
    assert link.is_symlink(), (
        f"{_rel(link)} is missing. Skill scripts are invoked as "
        f"`python3 ${{CLAUDE_SKILL_DIR}}/scripts/foo.py`, so `import agent_eval` "
        f"only resolves through this symlink. Create it with:\n"
        f"    ln -s ../../../agent_eval {_rel(link)}"
    )
    assert link.resolve() == (REPO_ROOT / "agent_eval").resolve(), (
        f"{_rel(link)} points at {link.resolve()}, expected the repo's agent_eval package"
    )


@pytest.mark.parametrize("script", _python_scripts(), ids=_rel)
def test_bootstrap_precedes_third_party_imports(script):
    if _rel(script) in _BOOTSTRAP_EXEMPT:
        pytest.skip("runs before the venv exists — see _BOOTSTRAP_EXEMPT")

    tree = ast.parse(script.read_text(), filename=str(script))

    bootstrap_line = next(
        (line for root, line in _toplevel_imports(tree)
         if root == "agent_eval" and _imports_bootstrap(tree, line)),
        None,
    )
    # Any third-party import anywhere decides *whether* the venv is needed; only
    # module-level ones constrain *where* the bootstrap import has to sit.
    needed = [(root, line) for root, line in _all_imports(tree) if _is_third_party(root, script)]
    if not needed:
        pytest.skip("stdlib-only script — does not need the venv")

    first_root, first_line = min(needed, key=lambda rl: rl[1])
    assert bootstrap_line is not None, (
        f"{_rel(script)} imports third-party `{first_root}` at line {first_line} "
        f"but never activates the venv. Add as the first import:\n"
        f"    import agent_eval._bootstrap  # noqa: F401 — auto-activate venv"
    )

    top_level = [(root, line) for root, line in _toplevel_imports(tree)
                 if _is_third_party(root, script)]
    if not top_level:
        return  # deferred imports only — module-level bootstrap is enough
    first_root, first_line = top_level[0]
    assert bootstrap_line <= first_line, (
        f"{_rel(script)}: `import agent_eval._bootstrap` is at line {bootstrap_line}, "
        f"after third-party `{first_root}` at line {first_line}. The venv's "
        f"site-packages are not on sys.path yet at that point — move the bootstrap "
        f"import above it."
    )


def test_orchestrate_does_not_leak_the_sentinel_to_children():
    """eval-anova spawns eval-run steps as `python3 <step>.py` children.

    The bootstrap sentinel is inherited across os.execv within one process, but a
    child is a fresh entry that must activate the venv itself. If the sentinel
    crossed over, every child would short-circuit `_activate()` and run without
    the venv's site-packages — which is exactly what happens under an ABI *match*,
    where the parent patches only its own sys.path and `sys.executable` is still
    the unpatched interpreter.
    """
    sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-anova" / "scripts"))
    import orchestrate

    from agent_eval._bootstrap import _SENTINEL

    os.environ[_SENTINEL] = "1"
    try:
        assert _SENTINEL not in orchestrate._child_env()
        assert _SENTINEL not in orchestrate._child_env({"FOO": "bar"})
        assert orchestrate._child_env({"FOO": "bar"})["FOO"] == "bar"
        # The overrides must not be able to reinstate it either.
        assert _SENTINEL not in orchestrate._child_env({_SENTINEL: "1"})
    finally:
        os.environ.pop(_SENTINEL, None)


def _imports_bootstrap(tree, lineno):
    """True if the import at `lineno` is specifically agent_eval._bootstrap."""
    for node in tree.body:
        if getattr(node, "lineno", None) != lineno:
            continue
        if isinstance(node, ast.Import):
            return any(a.name == "agent_eval._bootstrap" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            return node.module == "agent_eval" and any(
                a.name == "_bootstrap" for a in node.names)
    return False
