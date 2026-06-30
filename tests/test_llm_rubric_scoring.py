"""Tests for llm_rubric field in scoring infrastructure."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

# Import config
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_eval.config import EvalConfig

# Import scoring functions
sys.path.insert(0, str(Path(__file__).parent.parent / "skills/eval-run/scripts"))


class TestLLMRubricJudgeLoading:
    """Test that llm_rubric field is correctly loaded and used in scoring."""

    @pytest.fixture
    def config_with_llm_rubric(self):
        """Config with llm_rubric judge."""
        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {"path": "eval/dataset", "schema": "test"},
            "outputs": [{"path": "output", "schema": "test"}],
            "models": {"judge": "claude-opus-4-6"},
            "judges": [
                {
                    "name": "llm-rubric-judge",
                    "llm_rubric": "Evaluate if the agent found relevant documentation."
                }
            ]
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)
            yield config
        finally:
            Path(config_path).unlink()

    @pytest.fixture
    def config_with_both_fields(self):
        """Config with both llm_rubric and prompt (rubric should take precedence)."""
        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {"path": "eval/dataset", "schema": "test"},
            "outputs": [{"path": "output", "schema": "test"}],
            "models": {"judge": "claude-opus-4-6"},
            "judges": [
                {
                    "name": "precedence-judge",
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
            yield config
        finally:
            Path(config_path).unlink()

    def test_llm_rubric_judge_is_recognized(self, config_with_llm_rubric):
        """Test that judge with llm_rubric field is loaded as LLM judge."""
        import score

        # Mock the Anthropic client to avoid actual API calls
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client'):
                judges = score.load_judges(config_with_llm_rubric)

        assert len(judges) == 1
        name, scorer, condition, judge_type, samples = judges[0]
        assert name == "llm-rubric-judge"
        assert callable(scorer)
        assert condition == ""

    def test_llm_rubric_prompt_is_used(self, config_with_llm_rubric):
        """Test that llm_rubric content is used as the prompt."""
        import score

        judge_config = config_with_llm_rubric.judges[0]

        # Mock the Anthropic client
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].text = "true"
        mock_client.messages.create.return_value = mock_response

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client', return_value=mock_client):
                scorer = score._load_llm_judge(judge_config, config_with_llm_rubric)

                # Run scorer with sample outputs
                result = scorer(outputs={"files": {"output.txt": "test content"}})

        # Verify the API was called with the llm_rubric prompt
        mock_client.messages.create.assert_called_once()
        call_args = mock_client.messages.create.call_args
        messages = call_args[1]['messages']

        # The prompt should contain the llm_rubric text
        assert any("relevant documentation" in msg['content'] for msg in messages)

    def test_llm_rubric_takes_precedence_over_prompt(self, config_with_both_fields):
        """Test that llm_rubric is used instead of prompt when both are present."""
        import score

        judge_config = config_with_both_fields.judges[0]

        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].text = "true"
        mock_client.messages.create.return_value = mock_response

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client', return_value=mock_client):
                scorer = score._load_llm_judge(judge_config, config_with_both_fields)

                result = scorer(outputs={"files": {"output.txt": "test"}})

        # Verify the rubric prompt was used, not the regular prompt
        call_args = mock_client.messages.create.call_args
        messages = call_args[1]['messages']

        # Should contain "Use this rubric" not "This should be ignored"
        message_text = str(messages)
        assert "Use this rubric" in message_text
        assert "This should be ignored" not in message_text

    def test_llm_rubric_in_judge_type_detection(self):
        """Test that judge type detection includes llm_rubric."""
        import score
        from agent_eval.config import JudgeConfig

        # Judge with only llm_rubric
        jc = JudgeConfig(name="test", llm_rubric="Test rubric")

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client'):
                # Should be detected as LLM judge
                config_data = {
                    "name": "test",
                    "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
                    "dataset": {"path": ".", "schema": "test"},
                    "outputs": [{"path": "output", "schema": "test"}],
                    "models": {"judge": "claude-opus-4-6"},
                    "judges": [
                        {"name": "test", "llm_rubric": "Test rubric"}
                    ]
                }

                with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                    yaml.dump(config_data, f)
                    config_path = f.name

                try:
                    config = EvalConfig.from_yaml(config_path)
                    judges = score.load_judges(config)
                    assert len(judges) == 1
                finally:
                    Path(config_path).unlink()

    def test_llm_rubric_auto_appends_conversation(self, config_with_llm_rubric):
        """Test that llm_rubric automatically gets {{ conversation }} appended."""
        import score

        judge_config = config_with_llm_rubric.judges[0]

        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].text = '{"score": 5, "rationale": "test"}'
        mock_client.messages.create.return_value = mock_response

        # Create outputs with events for conversation extraction
        outputs = {
            "events": [
                {
                    "type": "assistant",
                    "text": "I found the documentation at /ai-docs/domain/machineconfig.md"
                }
            ],
            "files": {}
        }

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client', return_value=mock_client):
                scorer = score._load_llm_judge(judge_config, config_with_llm_rubric)
                result = scorer(outputs=outputs)

        # Verify the API was called with both rubric and conversation
        call_args = mock_client.messages.create.call_args
        messages = call_args[1]['messages']
        message_content = messages[0]['content']

        # Should contain both the rubric and the agent's response
        assert "relevant documentation" in message_content
        assert "Agent Response to Evaluate" in message_content
        assert "I found the documentation" in message_content

    def test_llm_rubric_no_duplicate_conversation_template(self):
        """Test that {{ conversation }} is not appended if already present."""
        import score
        from agent_eval.config import JudgeConfig, EvalConfig, ExecutionConfig, ModelsConfig

        config = EvalConfig(
            name="test",
            execution=ExecutionConfig(mode="case", prompt="{{ input.prompt }}"),
            models=ModelsConfig(judge="claude-opus-4-6"),
        )

        jc = JudgeConfig(
            name="test",
            llm_rubric="Evaluate based on {{ conversation }}"
        )

        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].text = '{"score": 5, "rationale": "test"}'
        mock_client.messages.create.return_value = mock_response

        outputs = {
            "events": [{"type": "assistant", "text": "Test response"}],
            "files": {}
        }

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client', return_value=mock_client):
                scorer = score._load_llm_judge(jc, config)
                result = scorer(outputs=outputs)

        call_args = mock_client.messages.create.call_args
        message_content = call_args[1]['messages'][0]['content']

        # Should contain the conversation template result, but not duplicated
        assert message_content.count("Test response") == 1
        # Should not have "Agent Response to Evaluate" heading (not auto-appended)
        assert "Agent Response to Evaluate" not in message_content


class TestLLMRubricErrorHandling:
    """Test error handling for llm_rubric judges."""

    def test_empty_llm_rubric_falls_back_to_prompt(self):
        """Test that empty llm_rubric falls back to prompt field."""
        import score
        from agent_eval.config import JudgeConfig, EvalConfig, ExecutionConfig, ModelsConfig

        config = EvalConfig(
            name="test",
            execution=ExecutionConfig(mode="case", prompt="{{ input.prompt }}"),
            models=ModelsConfig(judge="claude-opus-4-6"),
        )

        jc = JudgeConfig(
            name="test",
            llm_rubric="",  # Empty
            prompt="Use this prompt instead"
        )

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client'):
                scorer = score._load_llm_judge(jc, config)
                assert scorer is not None

    def test_missing_both_llm_rubric_and_prompt_raises_error(self):
        """Test that missing both llm_rubric and prompt raises error."""
        import score
        from agent_eval.config import JudgeConfig, EvalConfig, ExecutionConfig, ModelsConfig

        config = EvalConfig(
            name="test",
            execution=ExecutionConfig(mode="case", prompt="{{ input.prompt }}"),
            models=ModelsConfig(judge="claude-opus-4-6"),
        )

        jc = JudgeConfig(
            name="test",
            llm_rubric="",
            prompt="",
            prompt_file=""
        )

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with pytest.raises(ValueError, match="requires prompt, llm_rubric, or prompt_file"):
                score._load_llm_judge(jc, config)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
