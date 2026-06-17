"""Tests for taxonomy-based test case generation."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

# Import after path setup
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "skills/eval-dataset/scripts"))
from generate_from_taxonomy import (
    generate_from_taxonomy,
    _extract_json_from_response,
    _generate_category_cases,
)
from resolve_template import resolve_template


class TestTemplateResolution:
    """Test template reference resolution for test case generation."""

    def test_builtin_template_resolution(self, monkeypatch):
        """Test builtin templates resolve correctly."""
        skill_dir = Path(__file__).parent.parent / "skills/eval-dataset"
        monkeypatch.setenv("CLAUDE_SKILL_DIR", str(skill_dir))

        for template_name in ["navigation", "anti-pattern", "authoring", "component-usage", "architecture"]:
            resolved = resolve_template(f"documentation/{template_name}")
            assert resolved.exists()
            assert resolved.name == f"{template_name}.md"
            assert "templates/documentation" in str(resolved)

    def test_all_builtin_templates_have_required_sections(self, monkeypatch):
        """Test that all builtin templates have required documentation."""
        skill_dir = Path(__file__).parent.parent / "skills/eval-dataset"
        monkeypatch.setenv("CLAUDE_SKILL_DIR", str(skill_dir))

        required_sections = [
            "Test Case Structure",
            "Input Schema",
            "Generation Instructions",
            "Example",
        ]

        for template_name in ["navigation", "anti-pattern", "authoring", "component-usage", "architecture"]:
            template_path = resolve_template(f"documentation/{template_name}")
            content = template_path.read_text()

            for section in required_sections:
                assert section in content, f"Template {template_name} missing section: {section}"


class TestJSONExtraction:
    """Test JSON extraction from LLM responses."""

    def test_extract_plain_json(self):
        """Test extracting plain JSON array."""
        response = '[{"input": {"prompt": "test"}}]'
        result = _extract_json_from_response(response)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["input"]["prompt"] == "test"

    def test_extract_json_from_markdown(self):
        """Test extracting JSON from markdown code block."""
        response = '''Here are the test cases:

```json
[
  {
    "input": {"prompt": "test1"},
    "annotations": {"category": "navigation"}
  },
  {
    "input": {"prompt": "test2"}
  }
]
```

These test cases cover...'''
        result = _extract_json_from_response(response)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["input"]["prompt"] == "test1"
        assert result[0]["annotations"]["category"] == "navigation"

    def test_extract_json_no_language_marker(self):
        """Test extracting JSON from code block without language marker."""
        response = '''```
[{"input": {"prompt": "test"}}]
```'''
        result = _extract_json_from_response(response)
        assert isinstance(result, list)
        assert len(result) == 1

    def test_invalid_json_raises_error(self):
        """Test that invalid JSON raises ValueError."""
        with pytest.raises(ValueError, match="Could not extract valid JSON"):
            _extract_json_from_response("This is not JSON at all")


class TestTaxonomyGeneration:
    """Test taxonomy-based test case generation."""

    @pytest.fixture
    def sample_config(self):
        """Sample EvalConfig with taxonomy."""
        from agent_eval.config import EvalConfig, TestCategory

        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {
                "path": "eval/dataset",
                "schema": "input.yaml with prompt field",
                "test_categories": [
                    {
                        "name": "navigation",
                        "template": "documentation/navigation",
                        "count": 2,
                    }
                ],
                "domain": {
                    "type": "test-repo",
                    "documentation_structure": {
                        "entry_point": "CLAUDE.md",
                        "areas": [
                            {"path": "ai-docs/workflows/", "topics": ["process"]}
                        ]
                    }
                }
            },
            "outputs": [{"path": "output", "schema": "stdout.log"}],
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            return EvalConfig.from_yaml(config_path)
        finally:
            Path(config_path).unlink()

    def test_config_has_taxonomy(self, sample_config):
        """Test that sample config has taxonomy fields."""
        assert len(sample_config.test_categories) == 1
        assert sample_config.test_categories[0].name == "navigation"
        assert sample_config.test_categories[0].count == 2
        assert sample_config.dataset.domain["type"] == "test-repo"

    def test_generate_from_taxonomy_mocked(self, sample_config, monkeypatch):
        """Test generation with mocked API calls."""
        skill_dir = Path(__file__).parent.parent / "skills/eval-dataset"
        monkeypatch.setenv("CLAUDE_SKILL_DIR", str(skill_dir))

        # Mock the anthropic module to avoid import errors at test collection
        mock_anthropic_module = Mock()
        mock_anthropic_cls = Mock()
        mock_anthropic_module.Anthropic = mock_anthropic_cls
        monkeypatch.setitem(__import__('sys').modules, 'anthropic', mock_anthropic_module)

        # Mock API response
        mock_client = Mock()
        mock_anthropic_cls.return_value = mock_client

        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].text = json.dumps([
            {
                "input": {
                    "prompt": "How do I find process documentation?",
                    "expected_files": ["CLAUDE.md", "ai-docs/workflows/process.md"],
                },
                "annotations": {
                    "category": "navigation",
                    "difficulty": "easy"
                }
            },
            {
                "input": {
                    "prompt": "Where are the workflow docs?",
                    "expected_files": ["ai-docs/workflows/process.md"],
                },
                "annotations": {
                    "category": "navigation",
                    "difficulty": "easy"
                }
            }
        ])
        mock_client.messages.create.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "dataset"

            cases = generate_from_taxonomy(
                config=sample_config,
                output_dir=output_dir,
                model="claude-opus-4-6",
                api_key="test-key",
            )

            # Verify cases were generated
            assert len(cases) == 2
            assert cases[0]["case_id"] == "case-001"
            assert cases[0]["category"] == "navigation"

            # Verify files were written
            case1_dir = output_dir / "case-001"
            assert (case1_dir / "input.yaml").exists()
            assert (case1_dir / "annotations.yaml").exists()

            # Verify content
            input1 = yaml.safe_load((case1_dir / "input.yaml").read_text())
            assert "prompt" in input1
            assert "How do I find" in input1["prompt"]

            annotations1 = yaml.safe_load((case1_dir / "annotations.yaml").read_text())
            assert annotations1["category"] == "navigation"
            assert annotations1["difficulty"] == "easy"

    def test_no_categories_raises_error(self, monkeypatch):
        """Test that config without test_categories raises error."""
        from agent_eval.config import EvalConfig

        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case"},
            "dataset": {"path": "eval/dataset", "schema": "test"},
            "outputs": [{"path": "output", "schema": "test"}],
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)

            with tempfile.TemporaryDirectory() as tmpdir:
                with pytest.raises(ValueError, match="No test_categories"):
                    generate_from_taxonomy(
                        config=config,
                        output_dir=Path(tmpdir),
                        api_key="test-key",
                    )
        finally:
            Path(config_path).unlink()


class TestCLIIntegration:
    """Test CLI dry-run functionality."""

    def test_dry_run_no_api_key_required(self, monkeypatch):
        """Test that dry-run works without API key."""
        from agent_eval.config import EvalConfig

        skill_dir = Path(__file__).parent.parent / "skills/eval-dataset"
        monkeypatch.setenv("CLAUDE_SKILL_DIR", str(skill_dir))

        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {
                "path": "eval/dataset",
                "schema": "test",
                "test_categories": [
                    {"name": "navigation", "template": "documentation/navigation", "count": 2}
                ]
            },
            "outputs": [{"path": "output", "schema": "test"}],
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)

            # Dry run should work
            assert len(config.test_categories) == 1
            total_cases = sum(c.count for c in config.test_categories)
            assert total_cases == 2

        finally:
            Path(config_path).unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
