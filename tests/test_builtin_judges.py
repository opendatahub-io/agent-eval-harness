"""Unit tests for individual builtin judge implementations."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.judges.process.tool_call_validation import judge as tool_call_judge
from agent_eval.judges.efficiency.cost_budget import judge as cost_budget_judge
from agent_eval.judges.process.simulator_provenance import (
    judge as provenance_judge,
)


# ---------------------------------------------------------------------------
# tool_call_validation
# ---------------------------------------------------------------------------

class TestToolCallValidation:

    def test_pass_with_successful_calls(self):
        outputs = {"tool_calls": [
            {"name": "Bash", "input": {"command": "ls"}},
            {"name": "Read", "input": {"file_path": "/f"}},
        ]}
        passed, rationale = tool_call_judge(outputs)
        assert passed is True
        assert "2 tool calls completed successfully" in rationale

    def test_fail_with_error_result(self):
        outputs = {"tool_calls": [
            {"name": "Bash", "input": {}, "result": {"error": "command not found"}},
        ]}
        passed, rationale = tool_call_judge(outputs)
        assert passed is False
        assert "command not found" in rationale

    def test_fail_with_error_string(self):
        outputs = {"tool_calls": [
            {"name": "Bash", "input": {}, "result": "Error: permission denied"},
        ]}
        passed, rationale = tool_call_judge(outputs)
        assert passed is False
        assert "permission denied" in rationale

    def test_pass_with_empty_calls(self):
        outputs = {"tool_calls": []}
        passed, rationale = tool_call_judge(outputs)
        assert passed is True
        assert "No tool calls to validate" in rationale

    def test_pass_with_missing_tool_calls(self):
        outputs = {}
        passed, rationale = tool_call_judge(outputs)
        assert passed is True
        assert "No tool calls to validate" in rationale


# ---------------------------------------------------------------------------
# cost_budget
# ---------------------------------------------------------------------------

class TestCostBudget:

    def test_pass_within_budget(self):
        outputs = {"cost_usd": 0.50}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is True
        assert "$0.50" in rationale
        assert "$1.00" in rationale

    def test_fail_over_budget(self):
        outputs = {"cost_usd": 1.50}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is False
        assert "exceeds" in rationale

    def test_custom_threshold(self):
        outputs = {"cost_usd": 0.30}
        passed, rationale = cost_budget_judge(outputs, max_cost_usd=0.25)
        assert passed is False
        assert "$0.25" in rationale

    def test_custom_threshold_pass(self):
        outputs = {"cost_usd": 0.10}
        passed, rationale = cost_budget_judge(outputs, max_cost_usd=0.50)
        assert passed is True

    def test_missing_cost_data(self):
        outputs = {}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is False
        assert "No cost data" in rationale

    def test_none_cost_data(self):
        outputs = {"cost_usd": None}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is False
        assert "No cost data" in rationale

    def test_exact_budget(self):
        outputs = {"cost_usd": 1.0}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is True


# ---------------------------------------------------------------------------
# simulator_provenance
# ---------------------------------------------------------------------------

def _ask_user_event(n_questions=1):
    """An assistant event carrying one AskUserQuestion call."""
    return {
        "type": "assistant",
        "tools": [{
            "name": "AskUserQuestion",
            "input": {"questions": [
                {"question": f"Q{i}?"} for i in range(n_questions)]},
        }],
    }


class TestSimulatorProvenance:

    def test_pass_no_interception(self):
        passed, rationale = provenance_judge({})
        assert passed is True
        assert "No tool interception configured" in rationale

    def test_pass_all_recorded_tiers(self):
        outputs = {
            "interception_configured": True,
            "hook_answers_scope": "case",
            "hook_answers": [
                {"tier": "override", "question": "Q1?", "answer": "A"},
                {"tier": "llm", "question": "Q2?", "answer": "B",
                 "hook_model": "m", "match": "exact"},
            ],
            "events": [_ask_user_event(2)],
        }
        passed, rationale = provenance_judge(outputs)
        assert passed is True
        assert "1 override / 1 llm" in rationale

    def test_fail_on_fallback(self):
        outputs = {
            "interception_configured": True,
            "hook_answers_scope": "case",
            "hook_answers": [
                {"tier": "fallback", "question": "Which priority?",
                 "answer": "Normal", "error": "no api key"},
            ],
            "events": [_ask_user_event(1)],
        }
        passed, rationale = provenance_judge(outputs)
        assert passed is False
        assert "Which priority?" in rationale
        assert "fallback" in rationale

    def test_fail_on_disabled(self):
        outputs = {
            "interception_configured": True,
            "hook_answers_scope": "case",
            "hook_answers": [
                {"tier": "disabled", "reason": "pyyaml-missing"},
            ],
            "events": [],
        }
        passed, rationale = provenance_judge(outputs)
        assert passed is False
        assert "pyyaml-missing" in rationale

    def test_fail_on_error_record(self):
        outputs = {
            "interception_configured": True,
            "hook_answers_scope": "case",
            "hook_answers": [
                {"tier": "llm", "question": "Q?", "answer": "A",
                 "error": "API error mid-flight"},
            ],
            "events": [_ask_user_event(1)],
        }
        passed, rationale = provenance_judge(outputs)
        assert passed is False
        assert "error" in rationale

    def test_fail_when_ledger_missing_but_questions_asked(self):
        # The fail-open trap: interception configured, trace shows
        # AskUserQuestion, no ledger — unrecorded simulation.
        outputs = {
            "interception_configured": True,
            "hook_answers": None,
            "hook_answers_scope": None,
            "events": [_ask_user_event(1)],
        }
        passed, rationale = provenance_judge(outputs)
        assert passed is False
        assert "unrecorded" in rationale

    def test_pass_when_ledger_missing_no_questions(self):
        outputs = {
            "interception_configured": True,
            "hook_answers": None,
            "hook_answers_scope": None,
            "events": [{"type": "assistant", "tools": [{"name": "Read"}]}],
        }
        passed, rationale = provenance_judge(outputs)
        assert passed is True
        assert "nothing to certify" in rationale

    def test_fail_on_partial_coverage(self):
        # 2 questions in one AskUserQuestion call, only 1 recorded answer.
        outputs = {
            "interception_configured": True,
            "hook_answers_scope": "case",
            "hook_answers": [
                {"tier": "override", "question": "Q0?", "answer": "A"},
            ],
            "events": [_ask_user_event(2)],
        }
        passed, rationale = provenance_judge(outputs)
        assert passed is False
        assert "partial provenance" in rationale

    def test_run_scope_skips_coverage_and_notes_batch(self):
        # Same counts as the partial-coverage case, but run scope: per-case
        # attribution is impossible, so coverage is skipped.
        outputs = {
            "interception_configured": True,
            "hook_answers_scope": "run",
            "hook_answers": [
                {"tier": "override", "question": "Q0?", "answer": "A"},
            ],
            "events": [_ask_user_event(2)],
        }
        passed, rationale = provenance_judge(outputs)
        assert passed is True
        assert "run-level" in rationale

    def test_empty_ledger_no_questions_passes(self):
        outputs = {
            "interception_configured": True,
            "hook_answers_scope": "case",
            "hook_answers": [],
            "events": [],
        }
        passed, rationale = provenance_judge(outputs)
        assert passed is True

    def test_empty_ledger_with_questions_fails(self):
        # Empty-but-present ledger with observed questions = partial
        # provenance (catches swallowed best-effort writes).
        outputs = {
            "interception_configured": True,
            "hook_answers_scope": "case",
            "hook_answers": [],
            "events": [_ask_user_event(1)],
        }
        passed, rationale = provenance_judge(outputs)
        assert passed is False
        assert "partial provenance" in rationale


# ---------------------------------------------------------------------------
# LLM judge templates (rendering tests only, no LLM call)
# ---------------------------------------------------------------------------

class TestLLMJudgeTemplates:

    def test_no_harmful_content_renders(self):
        from jinja2 import Environment
        template_path = (Path(__file__).parent.parent
                         / "agent_eval" / "judges" / "safety"
                         / "no_harmful_content.md")
        raw = template_path.read_text()
        # Strip YAML frontmatter
        parts = raw.split("---", 2)
        template_text = parts[2] if len(parts) >= 3 else raw

        env = Environment()
        template = env.from_string(template_text)
        rendered = template.render(
            arguments={"categories": ["malware", "PII"]},
            outputs={
                "conversation": "Hello, I wrote a test.",
                "files": {"main.py": "print('hello')"},
            },
        )
        assert "malware" in rendered
        assert "Hello, I wrote a test." in rendered
        assert "main.py" in rendered

    def test_no_harmful_content_default_categories(self):
        from jinja2 import Environment
        template_path = (Path(__file__).parent.parent
                         / "agent_eval" / "judges" / "safety"
                         / "no_harmful_content.md")
        raw = template_path.read_text()
        parts = raw.split("---", 2)
        template_text = parts[2] if len(parts) >= 3 else raw

        env = Environment()
        template = env.from_string(template_text)
        rendered = template.render(arguments={}, outputs={
            "conversation": "test",
            "files": {},
        })
        assert "Dangerous instructions" in rendered
        assert "Malicious code" in rendered

    def test_output_completeness_renders(self):
        from jinja2 import Environment
        import json
        template_path = (Path(__file__).parent.parent
                         / "agent_eval" / "judges" / "quality"
                         / "output_completeness.md")
        raw = template_path.read_text()
        parts = raw.split("---", 2)
        template_text = parts[2] if len(parts) >= 3 else raw

        env = Environment()
        env.filters["tojson"] = lambda v: json.dumps(v, indent=2, default=str)
        template = env.from_string(template_text)
        rendered = template.render(
            arguments={"strictness": "high", "criteria": ["Has tests", "Has docs"]},
            outputs={"files": {"main.py": "code"}},
        )
        assert "high" in rendered
        assert "Has tests" in rendered
        assert "Has docs" in rendered
        assert "Every aspect" in rendered

    def test_output_completeness_default_strictness(self):
        from jinja2 import Environment
        import json
        template_path = (Path(__file__).parent.parent
                         / "agent_eval" / "judges" / "quality"
                         / "output_completeness.md")
        raw = template_path.read_text()
        parts = raw.split("---", 2)
        template_text = parts[2] if len(parts) >= 3 else raw

        env = Environment()
        env.filters["tojson"] = lambda v: json.dumps(v, indent=2, default=str)
        template = env.from_string(template_text)
        rendered = template.render(arguments={}, outputs={})
        assert "medium" in rendered
        assert "main requirements" in rendered
