"""Tests for synthetic test case generation."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

# Import after path setup
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "skills/eval-dataset/scripts"))
from generate_synthetic import (
    generate_synthetic,
    _extract_json_from_response,
    _generate_category_cases,
)
from agent_eval.prompts import BuiltinPromptRegistry, resolve_seed_prompt


class TestPromptResolution:
    """Test generation-prompt resolution (builtin / prompt_file / inline)."""

    def test_builtin_prompt_resolution(self):
        """Builtin generation prompts resolve to files under agent_eval/prompts/docs."""
        registry = BuiltinPromptRegistry()
        registry.discover()
        for name in ["navigation", "anti-pattern", "authoring", "component-usage", "architecture"]:
            entry = registry.get(f"docs/{name}")
            assert entry.path.exists()
            assert entry.path.name == f"{name}.md"
            assert "prompts/docs" in str(entry.path)

    def test_all_builtin_prompts_have_required_sections(self):
        """All builtin generation prompts have required documentation."""
        registry = BuiltinPromptRegistry()
        registry.discover()
        required_sections = [
            "Test Case Structure",
            "Input Schema",
            "Generation Instructions",
            "Example",
        ]
        for name in ["navigation", "anti-pattern", "authoring", "component-usage", "architecture"]:
            content = registry.get(f"docs/{name}").path.read_text()
            for section in required_sections:
                assert section in content, f"Prompt {name} missing section: {section}"

    def test_resolve_seed_prompt_dispatch(self):
        """resolve_seed_prompt dispatches on builtin / prompt_file / inline."""
        # builtin
        builtin_text = resolve_seed_prompt(
            {"category": "navigation", "builtin": "docs/navigation"}, ".")
        assert "Navigation Test Template" in builtin_text

        # inline
        assert resolve_seed_prompt(
            {"category": "x", "prompt": "inline text"}, ".") == "inline text"

        # prompt_file (relative to config_dir)
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "custom.md").write_text("project prompt body")
            text = resolve_seed_prompt(
                {"category": "x", "prompt_file": "custom.md"}, tmpdir)
            assert text == "project prompt body"

    def test_resolve_seed_prompt_missing_file(self):
        """A missing prompt_file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            resolve_seed_prompt(
                {"category": "x", "prompt_file": "does-not-exist.md"}, ".")


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


class TestSyntheticGeneration:
    """Test synthetic test case generation."""

    @pytest.fixture
    def sample_config(self):
        """Sample EvalConfig with a generation block."""
        from agent_eval.config import EvalConfig

        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {
                "path": "eval/dataset",
                "schema": "input.yaml with prompt field",
            },
            "generation": {
                "strategy": "synthetic",
                "context": {
                    "type": "test-repo",
                    "documentation_structure": {
                        "entry_point": "CLAUDE.md",
                        "areas": [
                            {"path": "ai-docs/workflows/", "topics": ["process"]}
                        ]
                    }
                },
                "seeds": [
                    {
                        "category": "navigation",
                        "builtin": "docs/navigation",
                        "count": 2,
                    }
                ],
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

    def test_config_has_generation(self, sample_config):
        """Test that sample config has generation fields."""
        assert len(sample_config.generation.seeds) == 1
        assert sample_config.generation.seeds[0].category == "navigation"
        assert sample_config.generation.seeds[0].builtin == "docs/navigation"
        assert sample_config.generation.seeds[0].count == 2
        assert sample_config.generation.context["type"] == "test-repo"

    def test_generate_synthetic_mocked(self, sample_config, monkeypatch):
        """Test generation with mocked API calls."""
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

            cases = generate_synthetic(
                config=sample_config,
                output_dir=output_dir,
                model="claude-opus-4-6",
                api_key="test-key",
            )

            # Verify cases were generated
            assert len(cases) == 2
            assert cases[0]["case_id"] == "case-001"
            assert cases[0]["category"] == "navigation"
            assert cases[0]["source"] == "docs/navigation"

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

    def test_no_seeds_raises_error(self, monkeypatch):
        """Test that config without generation seeds raises error."""
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
                with pytest.raises(ValueError, match="No generation seeds"):
                    generate_synthetic(
                        config=config,
                        output_dir=Path(tmpdir),
                        api_key="test-key",
                    )
        finally:
            Path(config_path).unlink()


class TestGenerationStrategy:
    """Test the generation.strategy provenance enum."""

    @staticmethod
    def _load(generation=None, execution=None):
        from agent_eval.config import EvalConfig
        config_data = {
            "name": "test-eval",
            "execution": execution or {"mode": "case", "skill": "x"},
            "dataset": {"path": "eval/dataset", "schema": "test"},
        }
        if generation is not None:
            config_data["generation"] = generation
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name
        try:
            return EvalConfig.from_yaml(config_path)
        finally:
            Path(config_path).unlink()

    def test_default_strategy_is_skill(self):
        """No generation block → strategy defaults to 'skill'."""
        config = self._load()
        assert config.generation.strategy == "skill"
        assert config.generation.seeds == []

    def test_explicit_skill_strategy(self):
        config = self._load(generation={"strategy": "skill"})
        assert config.generation.strategy == "skill"

    def test_from_traces_strategy_needs_no_seeds(self):
        config = self._load(
            generation={"strategy": "from-traces"},
            execution={"mode": "case", "prompt": "{{ input.prompt }}"})
        assert config.generation.strategy == "from-traces"
        assert config.generation.seeds == []

    def test_unknown_strategy_rejected(self):
        with pytest.raises(ValueError, match="generation.strategy must be one of"):
            self._load(generation={"strategy": "bogus"})

    def test_seeds_require_synthetic(self):
        with pytest.raises(ValueError, match="seeds are only valid with strategy: synthetic"):
            self._load(generation={
                "strategy": "from-traces",
                "seeds": [{"category": "n", "builtin": "docs/navigation", "count": 1}],
            })


class TestCLIIntegration:
    """Test CLI dry-run functionality."""

    def test_dry_run_no_api_key_required(self):
        """Test that dry-run works without API key."""
        from agent_eval.config import EvalConfig

        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {"path": "eval/dataset", "schema": "test"},
            "generation": {
                "strategy": "synthetic",
                "seeds": [
                    {"category": "navigation", "builtin": "docs/navigation", "count": 2}
                ],
            },
            "outputs": [{"path": "output", "schema": "test"}],
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)

            # Dry run should work
            assert len(config.generation.seeds) == 1
            total_cases = sum(s.count for s in config.generation.seeds)
            assert total_cases == 2

        finally:
            Path(config_path).unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
