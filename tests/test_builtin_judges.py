"""Unit tests for individual builtin judge implementations."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.judges.process.tool_call_validation import judge as tool_call_judge
from agent_eval.judges.efficiency.cost_budget import judge as cost_budget_judge


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

    def test_missing_cost_data_abstains(self):
        """An unknown cost is not an overspend: the judge abstains (value
        None → the case is skipped for this judge, not failed)."""
        outputs = {}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is None
        assert "cost unavailable" in rationale and "abstained" in rationale

    def test_none_cost_data_abstains(self):
        outputs = {"cost_usd": None}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is None
        assert "cost_source: missing" in rationale

    def test_unavailable_cost_source_abstains_even_with_a_number(self):
        outputs = {"cost_usd": 0.10, "cost_source": "unavailable"}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is None
        assert "cost_source: unavailable" in rationale

    def test_estimated_cost_is_judged_and_labelled(self):
        outputs = {"cost_usd": 1.50, "cost_source": "runner-estimate"}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is False
        assert "exceeds" in rationale and "estimate, not billed spend" in rationale
        outputs = {"cost_usd": 0.10, "cost_source": "runner:claude-code"}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is True and "estimate" in rationale

    def test_billed_cost_carries_no_estimate_label(self):
        outputs = {"cost_usd": 0.10, "cost_source": "openrouter:generation"}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is True and "estimate" not in rationale

    def test_exact_budget(self):
        outputs = {"cost_usd": 1.0}
        passed, rationale = cost_budget_judge(outputs)
        assert passed is True


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
