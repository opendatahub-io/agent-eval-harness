"""Contract test: every name in evalhub runner functions must resolve.

_run_with_client once referenced ``url`` (and the SDK request classes) from
its caller's local scope — module import succeeded, and every submission
crashed with NameError at runtime. The SDK is optional, so this cannot be
caught by importing and running the function in CI; instead resolve names
statically.

Resolution uses :mod:`symtable` — the compiler's own scope analysis — rather
than a hand-rolled AST walk. A flat ``ast.walk`` over a function treats
comprehension targets, nested-function locals and class-body assignments as
outer-function bindings, which hides exactly the NameError class this test
exists to catch (comprehension targets do not leak in Python 3). symtable
scopes each of those correctly and marks closure captures FREE rather than
GLOBAL, so legitimate nesting never false-positives.

A name is a problem when a function scope references it, never assigns it,
and resolution falls through to module scope (``is_global()``) where neither
the module body nor builtins define it.
"""

import ast
import builtins
import symtable
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


def _unresolved(source, filename, module_names):
    """``[(scope_name, [names])]`` that resolve in no enclosing scope."""
    known = module_names | set(dir(builtins))
    problems = []

    def visit(table):
        if table.get_type() == "function":
            bad = sorted(
                s.get_name()
                for s in table.get_symbols()
                if s.is_referenced() and s.is_global() and not s.is_assigned()
                and s.get_name() not in known
            )
            if bad:
                problems.append((table.get_name(), bad))
        for child in table.get_children():
            visit(child)

    visit(symtable.symtable(source, filename, "exec"))
    return problems


def test_every_function_name_resolves():
    source = RUNNER.read_text()
    problems = _unresolved(source, str(RUNNER), _module_scope(ast.parse(source)))
    assert not problems, (
        "names that resolve in no enclosing scope (NameError at runtime): "
        + "; ".join(f"{scope}: {names}" for scope, names in problems)
    )


# ── Detector regression cases ────────────────────────────────────────────
# Each fixture is a scoping trap a flat ast.walk-based binder gets wrong.

def _check(snippet):
    tree = ast.parse(snippet)
    return _unresolved(snippet, "<snippet>", _module_scope(tree))


def test_detects_caller_scope_leak():
    # The original bug shape: a helper using its caller's local.
    problems = _check(
        "def caller(client):\n"
        "    url = 'http://x'\n"
        "    return helper(client)\n"
        "def helper(client):\n"
        "    return str(url)\n"
    )
    assert problems == [("helper", ["url"])]


def test_comprehension_target_does_not_count_as_binding():
    problems = _check(
        "def f(values):\n"
        "    [x for x in values]\n"
        "    return x\n"
    )
    assert ("f", ["x"]) in problems


def test_nested_function_local_does_not_leak_out():
    problems = _check(
        "def outer():\n"
        "    def inner():\n"
        "        y = 1\n"
        "        return y\n"
        "    inner()\n"
        "    return y\n"
    )
    assert any(scope == "outer" and "y" in names for scope, names in problems)


def test_class_body_assignment_does_not_leak_into_methods():
    problems = _check(
        "def build():\n"
        "    class C:\n"
        "        attr = 1\n"
        "        def m(self):\n"
        "            return attr\n"
        "    return C\n"
    )
    assert any("attr" in names for _, names in problems)


def test_closure_and_comprehension_use_are_not_false_positives():
    problems = _check(
        "def outer(values):\n"
        "    threshold = 2\n"
        "    def inner():\n"
        "        return threshold\n"
        "    squares = [v * v for v in values if v > threshold]\n"
        "    fn = lambda v: v + threshold\n"
        "    return inner(), squares, fn(1)\n"
    )
    assert problems == []
