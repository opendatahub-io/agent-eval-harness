"""Unit tests for BuiltinJudgeRegistry discovery and resolution."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.judges import BuiltinJudgeRegistry


class TestBuiltinJudgeRegistry:

    def test_discover_finds_all_judges(self):
        registry = BuiltinJudgeRegistry()
        registry.discover()
        names = registry.list_names()
        assert "cost_budget" in names
        assert "tool_call_validation" in names
        assert "no_harmful_content" in names
        assert "output_completeness" in names
        assert len(names) == 4

    def test_list_names_sorted(self):
        registry = BuiltinJudgeRegistry()
        registry.discover()
        names = registry.list_names()
        assert names == sorted(names)

    def test_get_python_judge(self):
        registry = BuiltinJudgeRegistry()
        registry.discover()
        entry = registry.get("cost_budget")
        assert entry.kind == "python"
        assert entry.category == "efficiency"
        assert entry.module is not None
        assert entry.function_name == "judge"

    def test_get_llm_judge(self):
        registry = BuiltinJudgeRegistry()
        registry.discover()
        entry = registry.get("no_harmful_content")
        assert entry.kind == "llm"
        assert entry.category == "safety"
        assert entry.prompt_path is not None
        assert entry.prompt_path.exists()

    def test_get_fqn(self):
        registry = BuiltinJudgeRegistry()
        registry.discover()
        entry = registry.get("efficiency/cost_budget")
        assert entry.kind == "python"
        assert entry.category == "efficiency"

    def test_get_fqn_wrong_category(self):
        registry = BuiltinJudgeRegistry()
        registry.discover()
        with pytest.raises(ValueError, match="not 'safety'"):
            registry.get("safety/cost_budget")

    def test_get_unknown_name(self):
        registry = BuiltinJudgeRegistry()
        registry.discover()
        with pytest.raises(ValueError, match="Unknown builtin judge 'nonexistent'"):
            registry.get("nonexistent")

    def test_unknown_name_lists_available(self):
        registry = BuiltinJudgeRegistry()
        registry.discover()
        with pytest.raises(ValueError, match="cost_budget") as exc_info:
            registry.get("nonexistent")
        msg = str(exc_info.value)
        assert "no_harmful_content" in msg
        assert "tool_call_validation" in msg

    def test_md_detected_as_llm(self):
        registry = BuiltinJudgeRegistry()
        registry.discover()
        entry = registry.get("output_completeness")
        assert entry.kind == "llm"
        assert entry.category == "quality"

    def test_py_detected_as_python(self):
        registry = BuiltinJudgeRegistry()
        registry.discover()
        entry = registry.get("tool_call_validation")
        assert entry.kind == "python"
        assert entry.category == "process"

    def test_name_collision_detection(self, tmp_path):
        """Simulate a name collision by adding a duplicate file."""
        import shutil
        registry = BuiltinJudgeRegistry()
        judges_dir = Path(__file__).parent.parent / "agent_eval" / "judges"

        # Create a temp copy of the judges dir with a collision
        test_judges = tmp_path / "judges"
        shutil.copytree(judges_dir, test_judges)
        # Add a duplicate cost_budget.py in safety/
        shutil.copy(
            test_judges / "efficiency" / "cost_budget.py",
            test_judges / "safety" / "cost_budget.py",
        )

        # Monkey-patch the registry to scan the test dir
        import agent_eval.judges as judges_module
        original_file = judges_module.__file__
        judges_module.__file__ = str(test_judges / "__init__.py")
        try:
            with pytest.raises(ValueError, match=r"name collision.*cost_budget"):
                registry.discover()
        finally:
            judges_module.__file__ = original_file
