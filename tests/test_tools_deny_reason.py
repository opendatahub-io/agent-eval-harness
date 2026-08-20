"""The PreToolUse interceptor's deny output must carry the reason in the field
Claude Code actually reads.

The hook emitted only the legacy ``reason`` key; the CLI reads
``permissionDecisionReason``, so agents saw the generic "Hook PreToolUse denied
this tool" and every handler's crafted guidance was silently dropped — on a
real eval run the denied agents had to reverse-engineer why the call was
blocked by reading tool_handlers.yaml themselves.
"""

import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_PY = REPO_ROOT / "skills" / "eval-run" / "scripts" / "tools.py"


def _run_hook(tmp_path, tool_name, tool_input):
    (tmp_path / "tool_handlers.yaml").write_text(yaml.safe_dump({
        "handlers": [{
            "match": "network fetch skipped - files are pre-provisioned; "
                     "treat this step as successful and continue",
            "patterns": ["Bash"],
            "input_filters": [r"fetch_strategy\.py"],
        }],
    }))
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    hook = hook_dir / "tools.py"
    hook.write_text(TOOLS_PY.read_text())
    proc = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"tool_name": tool_name, "tool_input": tool_input}),
        capture_output=True, text=True, cwd=tmp_path,
    )
    return proc


def test_deny_carries_permission_decision_reason(tmp_path):
    proc = _run_hook(tmp_path, "Bash",
                     {"command": "python3 scripts/fetch_strategy.py fetch-one X-1"})
    out = json.loads(proc.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    # The field the CLI reads:
    assert "pre-provisioned" in hso["permissionDecisionReason"]
    # Back-compat for consumers of the old key:
    assert hso["permissionDecisionReason"] == hso["reason"]


def test_unmatched_bash_passes_through_silently(tmp_path):
    proc = _run_hook(tmp_path, "Bash", {"command": "ls -la"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_hook_runs_without_agent_eval_importable(tmp_path):
    """The copied hook must work as a bare python3 script: agent_eval is not
    importable from a workspace, and a crashing PreToolUse hook is silently
    treated as pass-through by the CLI — which disabled ALL interception on a
    real run."""
    proc = _run_hook(tmp_path, "Bash",
                     {"command": "python3 scripts/fetch_strategy.py fetch-one X-1"})
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
