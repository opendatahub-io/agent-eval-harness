"""run_result.json must not silently drop RunResult telemetry.

The runner has carried permission_denials since the CLI-side union fix, but
execute.py never serialized it: on a real run, the only permission denial (a
workspace-escape attempt by a subagent) survived solely in raw stdout.log —
invisible to run_result.json, summary.yaml and every downstream reader, which
is exactly how an earlier escape stayed hidden until a manual log audit.

Pinned structurally (like test_venv_activation pins import order): every dict
literal in execute.py that serializes per-RunResult telemetry (identified by a
"per_model_turns" key fed from a result attribute) must also carry
"permission_denials". This guards future result-dict sites too.
"""

import ast
from pathlib import Path

EXECUTE = (Path(__file__).resolve().parent.parent
           / "skills" / "eval-run" / "scripts" / "execute.py")


def _dict_keys(node):
    keys = []
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.append(k.value)
    return keys


def _serializes_runresult(node):
    """True for dicts whose per_model_turns value reads an attribute off a
    result object (result.per_model_turns / step_result.per_model_turns) —
    i.e. RunResult serialization sites, not aggregation dicts."""
    for k, v in zip(node.keys, node.values):
        if (isinstance(k, ast.Constant) and k.value == "per_model_turns"
                and isinstance(v, ast.Attribute)
                and v.attr == "per_model_turns"):
            return True
    return False


def test_every_runresult_dict_carries_permission_denials():
    tree = ast.parse(EXECUTE.read_text(), filename=str(EXECUTE))
    sites = [n for n in ast.walk(tree)
             if isinstance(n, ast.Dict) and _serializes_runresult(n)]
    assert sites, "no RunResult serialization sites found — did execute.py move?"
    missing = [n.lineno for n in sites
               if "permission_denials" not in _dict_keys(n)]
    assert not missing, (
        f"RunResult-serializing dict(s) at execute.py line(s) {missing} drop "
        f"permission_denials — denial telemetry would silently vanish from "
        f"run_result.json again.")
