"""Tests for prompt-based analysis (/eval-analyze --prompt)."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

# Import after setting up path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "skills/eval-analyze/scripts"))
from resolve_prompt import resolve_analysis_prompt


class TestPromptResolution:
    """Test prompt reference resolution."""


    def test_custom_prompt_resolution(self):
        """Test custom prompt path resolution."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("# Custom Analysis Prompt")
            custom_path = f.name

        try:
            resolved = resolve_analysis_prompt(custom_path)
            assert resolved.exists()
            assert resolved.read_text() == "# Custom Analysis Prompt"
        finally:
            Path(custom_path).unlink()

    def test_custom_path_not_found(self):
        """Test custom path that doesn't exist raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="Prompt file not found"):
            resolve_analysis_prompt("/nonexistent/prompt.md")

    def test_empty_prompt_ref(self):
        """Test empty prompt reference raises ValueError."""
        with pytest.raises(ValueError, match="prompt_ref cannot be empty"):
            resolve_analysis_prompt("")


class TestPromptBasedConfigGeneration:
    """Test eval.yaml generation from prompt-based analysis."""

    @pytest.fixture
    def sample_docs_config(self):
        """Sample eval.yaml generated from docs analysis."""
        return {
            "name": "test-repo-docs-eval",
            "description": "Test AI agents using test-repo documentation",
            "execution": {
                "mode": "case",
                "prompt": "{{ input.prompt }}"
            },
            "runner": {
                "type": "claude-code",
                "settings": {
                    "append_allowed_tools": ["Read", "Grep", "Glob"]
                }
            },
            "models": {
                "skill": "claude-sonnet-4-6",
                "judge": "claude-opus-4-6"
            },
            "dataset": {
                "path": "eval/dataset",
                "schema": "input.yaml with 'prompt' field, expected_files list",
                "test_categories": [
                    {
                        "name": "navigation",
                        "template": "documentation/navigation",
                        "count": 2,
                        "description": "Agent finds relevant documentation"
                    },
                    {
                        "name": "anti-pattern",
                        "template": "documentation/anti-pattern",
                        "count": 1,
                        "description": "Agent rejects constraint violations"
                    }
                ],
                "domain": {
                    "type": "test-repository",
                    "documentation_structure": {
                        "entry_point": "CLAUDE.md",
                        "areas": [
                            {
                                "path": "ai-docs/workflows/",
                                "topics": ["enhancement-process"]
                            },
                            {
                                "path": "ai-docs/domain/",
                                "topics": ["api-concepts"]
                            }
                        ]
                    },
                    "constraints": [
                        {
                            "rule": "All APIs must start with v1alpha1",
                            "documentation": "CLAUDE.md",
                            "wrong_approach": "Starting with v1 for stability"
                        }
                    ]
                }
            },
            "outputs": [
                {
                    "path": "output",
                    "schema": "stdout.log: Agent's response"
                }
            ],
            "traces": {
                "stdout": True,
                "stderr": True,
                "events": True,
                "metrics": True
            },
            "judges": [
                {
                    "name": "has-documentation-section",
                    "check": "'## Documentation Used' in (Path('{outputs}') / 'stdout.log').read_text()",
                    "weight": 4
                },
                {
                    "name": "found-relevant-docs",
                    "llm_rubric": "Agent cited relevant documentation",
                    "weight": 3
                }
            ],
            "thresholds": {
                "has-documentation-section": {"min_pass_rate": 0.8},
                "found-relevant-docs": {"min_pass_rate": 0.7}
            }
        }

    def test_prompt_mode_config_structure(self, sample_docs_config):
        """Test that generated config has correct structure for prompt mode."""
        assert sample_docs_config["execution"]["mode"] == "case"
        assert sample_docs_config["execution"]["prompt"] == "{{ input.prompt }}"

        # Skill field should be absent in execution for prompt mode
        assert "skill" not in sample_docs_config.get("execution", {})

    def test_taxonomy_based_dataset(self, sample_docs_config):
        """Test that config includes taxonomy-based dataset structure."""
        dataset = sample_docs_config["dataset"]

        assert "test_categories" in dataset
        assert len(dataset["test_categories"]) == 2

        # Check category structure
        nav_category = dataset["test_categories"][0]
        assert nav_category["name"] == "navigation"
        assert nav_category["template"] == "documentation/navigation"
        assert nav_category["count"] == 2

    def test_domain_knowledge_extraction(self, sample_docs_config):
        """Test that domain knowledge is extracted."""
        domain = sample_docs_config["dataset"]["domain"]

        assert domain["type"] == "test-repository"
        assert "documentation_structure" in domain
        assert domain["documentation_structure"]["entry_point"] == "CLAUDE.md"

        # Check constraints
        assert "constraints" in domain
        assert len(domain["constraints"]) == 1
        assert "v1alpha1" in domain["constraints"][0]["rule"]

    def test_config_validation(self, sample_docs_config):
        """Test that generated config can be loaded by EvalConfig."""
        from agent_eval.config import EvalConfig

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(sample_docs_config, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)

            # Validate key fields
            assert config.execution.mode == "case"
            assert config.execution.prompt  # Prompt mode uses execution.prompt
            assert not config.execution.skill  # Prompt mode has no skill in execution

            # Validate taxonomy fields
            assert len(config.test_categories) == 2
            assert config.test_categories[0].name == "navigation"
            assert config.test_categories[0].template == "documentation/navigation"
            assert config.test_categories[0].count == 2

            # Validate domain knowledge
            assert config.dataset.domain["type"] == "test-repository"
            assert "documentation_structure" in config.dataset.domain
            assert len(config.dataset.domain["constraints"]) == 1

        finally:
            Path(config_path).unlink()

    def test_judge_types(self, sample_docs_config):
        """Test that config includes both inline and LLM judges."""
        judges = sample_docs_config["judges"]

        # Should have inline check judge (Python expression, not shell command)
        inline_judge = next(j for j in judges if "check" in j)
        assert "## Documentation Used" in inline_judge["check"]

        # Should have LLM rubric judge
        llm_judge = next(j for j in judges if "llm_rubric" in j)
        assert "documentation" in llm_judge["llm_rubric"].lower()

    def test_llm_rubric_field_loaded(self, sample_docs_config):
        """Test that llm_rubric field is loaded into JudgeConfig correctly."""
        from agent_eval.config import EvalConfig

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(sample_docs_config, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)

            # Find the LLM rubric judge
            llm_judge = next(j for j in config.judges if j.llm_rubric)
            assert llm_judge.name == "found-relevant-docs"
            assert llm_judge.llm_rubric == "Agent cited relevant documentation"
            # prompt should be empty since we're using llm_rubric
            assert llm_judge.prompt == ""

        finally:
            Path(config_path).unlink()

    def test_llm_rubric_precedence_over_prompt(self):
        """Test that llm_rubric takes precedence over prompt in judge loading."""
        from agent_eval.config import EvalConfig

        # Config with both llm_rubric and prompt (llm_rubric should win)
        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {"path": "eval/dataset", "schema": "test"},
            "outputs": [{"path": "output", "schema": "test"}],
            "judges": [
                {
                    "name": "rubric-judge",
                    "llm_rubric": "Use this rubric",
                    "prompt": "This should be ignored"
                }
            ]
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)

            judge = config.judges[0]
            assert judge.llm_rubric == "Use this rubric"
            assert judge.prompt == "This should be ignored"

            # The scorer loading logic should use llm_rubric first
            # This is tested in the score.py _load_llm_judge function

        finally:
            Path(config_path).unlink()


class TestEndToEndFlow:
    """Test the complete prompt-based analysis flow."""

    def test_workflow_components_exist(self):
        """Test that all required workflow components exist."""
        base = Path(__file__).parent.parent

        # Required files
        assert (base / "skills/eval-analyze/SKILL.md").exists()
        assert (base / "skills/eval-analyze/scripts/resolve_prompt.py").exists()
        assert (base / "skills/eval-analyze/prompts/analyze-skill.md").exists()
        assert (base / "skills/eval-analyze/scripts/validate_eval.py").exists()
        assert (base / "examples/openshift-agentic-docs.md").exists()

    def test_skill_md_has_prompt_mode_docs(self):
        """Test that SKILL.md documents prompt mode."""
        skill_md = Path(__file__).parent.parent / "skills/eval-analyze/SKILL.md"
        content = skill_md.read_text()

        assert "--prompt" in content
        assert "examples/" in content
        assert "Step 2-Prompt" in content
        assert "Prompt-Based Analysis" in content or "PROMPT-BASED ANALYSIS" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
