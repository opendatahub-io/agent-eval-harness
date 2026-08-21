"""Contract test: every name in evalhub runner functions must resolve.

_run_with_client once referenced ``url`` (and the SDK request classes) from
its caller's local scope — module import succeeded, and every submission
crashed with NameError at runtime. The SDK is optional, so this cannot be
caught by importing and running the function in CI; instead resolve each
function's free variables statically against its own bindings, module scope,
and builtins.
"""

import ast
import builtins
from pathlib import Path

RUNNER = Path(__file__).parent.parent / "agent_eval" / "evalhub" / "runner.py"


def _module_scope(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _bound_in(fn):
    bound = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    bound.update(a.arg for a in (fn.args.vararg, fn.args.kwarg) if a)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
    return bound


def test_every_function_name_resolves():
    tree = ast.parse(RUNNER.read_text())
    module_scope = _module_scope(tree) | set(dir(builtins))
    problems = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound = _bound_in(fn)
        free = {
            node.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        } - bound - module_scope
        if free:
            problems.append(f"{fn.name}: {sorted(free)}")
    assert not problems, (
        "names that resolve in no enclosing scope (NameError at runtime): "
        + "; ".join(problems)
    )
