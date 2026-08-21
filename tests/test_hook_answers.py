"""Simulator answer-provenance ledger: hook_answers.jsonl end to end.

tools.py (the PreToolUse interceptor) writes one JSONL record per intercepted
AskUserQuestion — anchored to its own directory, never CWD — plus explicit
``disabled`` records on both silent-disable paths. collect.py harvests the
ledger per case (case/in-repo mode) and to the run root (batch mode), and
score.py's load_case_record exposes it with a load-bearing None-vs-[]
distinction for the simulator_provenance judge.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

TOOLS_PY = REPO_ROOT / "skills" / "eval-run" / "scripts" / "tools.py"

ASK_INPUT = {
    "tool_name": "AskUserQuestion",
    "tool_input": {"questions": [
        {"question": "Which priority?", "options": [
            {"label": "Normal", "description": "default"},
            {"label": "High", "description": "urgent"}]},
    ]},
}

HANDLERS = {
    "handlers": [{
        "match": "Questions asked via AskUserQuestion.",
        "patterns": ["AskUserQuestion"],
        "prompt": "Answer from case context.",
    }],
}


def _offline_env(extra=None):
    """Env with no Anthropic credentials so the LLM tier fails
    deterministically offline (constructor raises before any request)."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    # Belt and braces: even a mis-resolved credential can't leave the host.
    env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
    if extra:
        env.update(extra)
    return env


def _stage_hook(workspace):
    """Copy tools.py to <workspace>/hooks/tools.py like every mode does."""
    hooks_dir = workspace / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "tools.py"
    hook.write_text(TOOLS_PY.read_text())
    return hook


def _run_hook(hook, stdin_obj, cwd, env=None):
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(stdin_obj),
        capture_output=True, text=True, cwd=cwd,
        env=env or _offline_env(),
    )


def _read_ledger(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Ledger writing (subprocess — the hook runs exactly as the CLI runs it)
# ---------------------------------------------------------------------------

class TestLedgerWriting:

    def test_override_answer_recorded(self, tmp_path):
        handlers = dict(HANDLERS)
        handlers["case_overrides"] = {"Which priority?": "High"}
        (tmp_path / "tool_handlers.yaml").write_text(yaml.safe_dump(handlers))
        hook = _stage_hook(tmp_path)

        proc = _run_hook(hook, ASK_INPUT, cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which priority?": "High"}

        records = _read_ledger(tmp_path / "hooks" / "hook_answers.jsonl")
        assert len(records) == 1
        rec = records[0]
        assert rec["tier"] == "override"
        assert rec["question"] == "Which priority?"
        assert rec["options"] == ["Normal", "High"]
        assert rec["answer"] == "High"
        assert rec["ts"]

    def test_fallback_records_llm_failure(self, tmp_path):
        (tmp_path / "tool_handlers.yaml").write_text(yaml.safe_dump(HANDLERS))
        hook = _stage_hook(tmp_path)

        proc = _run_hook(hook, ASK_INPUT, cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        # Fallback = first option
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which priority?": "Normal"}

        rec = _read_ledger(tmp_path / "hooks" / "hook_answers.jsonl")[0]
        assert rec["tier"] == "fallback"
        assert rec["error"]  # no credentials / no module — attempt recorded
        assert rec["hook_model"]

    def test_ledger_anchored_to_script_dir_not_cwd(self, tmp_path):
        """In-repo containment: the hook's CWD is the user's repo there —
        the ledger must land next to the script, never under CWD."""
        casews = tmp_path / "casews"
        hook = _stage_hook(casews)
        sibling = tmp_path / "repo-cwd"
        sibling.mkdir()
        handlers = dict(HANDLERS)
        handlers["case_overrides"] = {"Which priority?": "High"}
        (sibling / "tool_handlers.yaml").write_text(yaml.safe_dump(handlers))

        proc = _run_hook(hook, ASK_INPUT, cwd=sibling)
        assert proc.returncode == 0, proc.stderr
        assert (casews / "hooks" / "hook_answers.jsonl").is_file()
        assert not list(sibling.rglob("hook_answers.jsonl"))

    def test_missing_tool_handlers_writes_disabled_record(self, tmp_path):
        hook = _stage_hook(tmp_path / "ws")
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()

        proc = _run_hook(hook, ASK_INPUT, cwd=cwd)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == ""  # pass-through, no decision emitted

        rec = _read_ledger(tmp_path / "ws" / "hooks" / "hook_answers.jsonl")[0]
        assert rec["tier"] == "disabled"
        assert rec["reason"] == "tool-handlers-missing"
        assert rec["cwd"] == str(cwd)
        assert rec["ts"]

    def test_missing_pyyaml_writes_disabled_record(self, tmp_path):
        blocker = tmp_path / "blocker" / "yaml"
        blocker.mkdir(parents=True)
        (blocker / "__init__.py").write_text(
            'raise ImportError("pyyaml blocked for test")\n')
        (tmp_path / "tool_handlers.yaml").write_text("handlers: []\n")
        hook = _stage_hook(tmp_path)

        proc = _run_hook(hook, ASK_INPUT, cwd=tmp_path,
                         env=_offline_env(
                             {"PYTHONPATH": str(tmp_path / "blocker")}))
        assert proc.returncode == 0, proc.stderr
        assert "Traceback" not in proc.stderr

        rec = _read_ledger(tmp_path / "hooks" / "hook_answers.jsonl")[0]
        assert rec["tier"] == "disabled"
        assert rec["reason"] == "pyyaml-missing"

    def test_tool_handlers_resolved_via_file_fallback(self, tmp_path):
        """The adopted in-repo fix: CWD lookup misses, but the file next to
        the hooks/ dir (where every generation site writes it) is found."""
        casews = tmp_path / "casews"
        hook = _stage_hook(casews)
        handlers = dict(HANDLERS)
        handlers["case_overrides"] = {"Which priority?": "High"}
        (casews / "tool_handlers.yaml").write_text(yaml.safe_dump(handlers))
        repo_cwd = tmp_path / "repo-cwd"
        repo_cwd.mkdir()

        proc = _run_hook(hook, ASK_INPUT, cwd=repo_cwd)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
            "Which priority?": "High"}
        rec = _read_ledger(casews / "hooks" / "hook_answers.jsonl")[0]
        assert rec["tier"] == "override"

    def test_deny_path_writes_no_records(self, tmp_path):
        (tmp_path / "tool_handlers.yaml").write_text(yaml.safe_dump({
            "handlers": [{
                "match": "network fetch skipped",
                "patterns": ["Bash"],
                "input_filters": [r"fetch_strategy\.py"],
            }],
        }))
        hook = _stage_hook(tmp_path)

        proc = _run_hook(
            hook,
            {"tool_name": "Bash",
             "tool_input": {"command": "python3 fetch_strategy.py X-1"}},
            cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert not (tmp_path / "hooks" / "hook_answers.jsonl").exists()


# ---------------------------------------------------------------------------
# Collection (collect.py)
# ---------------------------------------------------------------------------

class TestCollectLedger:

    def test_collect_case_ledger_per_case(self, tmp_path):
        from collect import _collect_case_ledger
        case_ws = tmp_path / "workspace" / "cases" / "c1"
        (case_ws / "hooks").mkdir(parents=True)
        (case_ws / "hooks" / "hook_answers.jsonl").write_text(
            '{"tier": "override"}\n')
        dest = tmp_path / "out" / "cases" / "c1"

        assert _collect_case_ledger(case_ws, dest) is True
        assert (dest / "hook_answers.jsonl").read_text() == \
            '{"tier": "override"}\n'

    def test_collect_case_ledger_absent(self, tmp_path):
        from collect import _collect_case_ledger
        case_ws = tmp_path / "c1"
        case_ws.mkdir()
        dest = tmp_path / "out"
        assert _collect_case_ledger(case_ws, dest) is False
        assert not (dest / "hook_answers.jsonl").exists()

    def test_collect_case_ledger_rejects_symlinks(self, tmp_path):
        from collect import _collect_case_ledger
        secret = tmp_path / "secret.jsonl"
        secret.write_text('{"leak": true}\n')

        # Symlinked ledger file
        ws1 = tmp_path / "ws1"
        (ws1 / "hooks").mkdir(parents=True)
        (ws1 / "hooks" / "hook_answers.jsonl").symlink_to(secret)
        assert _collect_case_ledger(ws1, tmp_path / "out1") is False

        # Symlinked hooks/ dir
        real_hooks = tmp_path / "outside-hooks"
        real_hooks.mkdir()
        (real_hooks / "hook_answers.jsonl").write_text('{"leak": true}\n')
        ws2 = tmp_path / "ws2"
        ws2.mkdir()
        (ws2 / "hooks").symlink_to(real_hooks)
        assert _collect_case_ledger(ws2, tmp_path / "out2") is False

    def test_collect_batch_run_root(self, tmp_path):
        from collect import _collect_case_ledger
        workspace = tmp_path / "workspace"
        (workspace / "hooks").mkdir(parents=True)
        (workspace / "hooks" / "hook_answers.jsonl").write_text(
            '{"tier": "llm"}\n')
        output_dir = tmp_path / "run-root"

        assert _collect_case_ledger(workspace, output_dir) is True
        assert (output_dir / "hook_answers.jsonl").is_file()

    def test_hooks_stay_out_of_modified(self):
        """The ledger lives under hooks/, which _HARNESS_PATHS excludes from
        _modified/ collection — it can never leak in as an 'edit'."""
        from collect import _HARNESS_PATHS
        assert "hooks" in _HARNESS_PATHS


# ---------------------------------------------------------------------------
# Record loading (score.py load_case_record)
# ---------------------------------------------------------------------------

class TestLoadCaseRecordLedger:

    def _config(self):
        from agent_eval.config import EvalConfig
        return EvalConfig()

    def test_absent_ledger_is_none(self, tmp_path):
        from score import load_case_record
        from agent_eval.config import ToolInputConfig
        case_dir = tmp_path / "case-001"
        case_dir.mkdir()

        config = self._config()
        record = load_case_record(case_dir, config)
        assert record["hook_answers"] is None  # not []
        assert record["hook_answers_scope"] is None
        assert record["interception_configured"] is False

        config.inputs.tools.append(ToolInputConfig(match="AskUserQuestion"))
        record = load_case_record(case_dir, config)
        assert record["interception_configured"] is True
        assert record["hook_answers"] is None

    def test_present_ledger_parsed_leniently(self, tmp_path):
        from score import load_case_record
        case_dir = tmp_path / "case-001"
        case_dir.mkdir()
        (case_dir / "hook_answers.jsonl").write_text(
            '{"tier": "override", "question": "Q1"}\n'
            'NOT JSON {{{\n'
            '{"tier": "llm", "question": "Q2"}\n')

        record = load_case_record(case_dir, self._config())
        assert [r["tier"] for r in record["hook_answers"]] == [
            "override", "llm"]
        assert record["hook_answers_scope"] == "case"

    def test_empty_ledger_is_empty_list(self, tmp_path):
        from score import load_case_record
        case_dir = tmp_path / "case-001"
        case_dir.mkdir()
        (case_dir / "hook_answers.jsonl").write_text("")

        record = load_case_record(case_dir, self._config())
        assert record["hook_answers"] == []  # distinct from None
        assert record["hook_answers_scope"] == "case"

    def test_harbor_hooks_dir_fallback(self, tmp_path):
        """In-container Harbor scoring: case_dir IS the agent workspace, so
        the ledger sits at hooks/hook_answers.jsonl uncollected."""
        from score import load_case_record
        case_dir = tmp_path / "case-001"
        (case_dir / "hooks").mkdir(parents=True)
        (case_dir / "hooks" / "hook_answers.jsonl").write_text(
            '{"tier": "llm", "question": "Q"}\n')

        record = load_case_record(case_dir, self._config())
        assert len(record["hook_answers"]) == 1
        assert record["hook_answers_scope"] == "case"

    def test_run_root_fallback_scope_run(self, tmp_path):
        from score import load_case_record
        case_dir = tmp_path / "r1" / "cases" / "case-001"
        case_dir.mkdir(parents=True)
        (tmp_path / "r1" / "hook_answers.jsonl").write_text(
            '{"tier": "override", "question": "Q"}\n')

        record = load_case_record(case_dir, self._config(),
                                  run_id="r1", runs_dir=tmp_path)
        assert len(record["hook_answers"]) == 1
        assert record["hook_answers_scope"] == "run"

    def test_parse_hook_ledger_unreadable_returns_none(self, tmp_path):
        from score import _parse_hook_ledger
        assert _parse_hook_ledger(tmp_path / "nope" / "x.jsonl") is None


# ---------------------------------------------------------------------------
# Report rendering (report.py)
# ---------------------------------------------------------------------------

class TestSimUserLine:

    def test_case_scope_tier_counts_and_fallback_flag(self, tmp_path):
        import report
        case_dir = tmp_path / "cases" / "c1"
        case_dir.mkdir(parents=True)
        (case_dir / "hook_answers.jsonl").write_text(
            '{"tier": "override", "question": "Q1", "answer": "A"}\n'
            '{"tier": "llm", "question": "Q2", "answer": "B"}\n'
            '{"tier": "fallback", "question": "Q3?", "answer": "C"}\n')

        html = report._render_sim_user_line(case_dir, tmp_path)
        assert "Simulated user: 3 answer(s)" in html
        assert "1 override" in html
        assert "1 llm" in html
        assert "1 fallback" in html
        assert '<span class="fail">fallback: Q3?</span>' in html
        assert "run-level" not in html

    def test_run_scope_note(self, tmp_path):
        import report
        case_dir = tmp_path / "cases" / "c1"
        case_dir.mkdir(parents=True)
        (tmp_path / "hook_answers.jsonl").write_text(
            '{"tier": "llm", "question": "Q", "answer": "A"}\n')

        html = report._render_sim_user_line(case_dir, tmp_path)
        assert "(run-level ledger" in html

    def test_escapes_html(self, tmp_path):
        import report
        case_dir = tmp_path / "cases" / "c1"
        case_dir.mkdir(parents=True)
        (case_dir / "hook_answers.jsonl").write_text(json.dumps(
            {"tier": "fallback", "question": "<script>alert(1)</script>",
             "answer": "x"}) + "\n")

        html = report._render_sim_user_line(case_dir, tmp_path)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_no_ledger_renders_nothing(self, tmp_path):
        import report
        case_dir = tmp_path / "cases" / "c1"
        case_dir.mkdir(parents=True)
        assert report._render_sim_user_line(case_dir, tmp_path) == ""

    def test_disabled_record_flagged(self, tmp_path):
        import report
        case_dir = tmp_path / "cases" / "c1"
        case_dir.mkdir(parents=True)
        (case_dir / "hook_answers.jsonl").write_text(
            '{"tier": "disabled", "reason": "pyyaml-missing"}\n')

        html = report._render_sim_user_line(case_dir, tmp_path)
        assert "1 disabled" in html
        assert '<span class="fail">disabled: pyyaml-missing</span>' in html
