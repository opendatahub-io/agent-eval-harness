"""Tests for OpenAICompatibleRunner and RUNNERS registry integrity."""

import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent import RUNNERS, EvalRunner, RunResult
from agent_eval.agent.base import EvalRunner as BaseRunner, RunResult as BaseResult
from agent_eval.agent.claude_code import ClaudeCodeRunner
from agent_eval.agent.openai_compatible import OpenAICompatibleRunner


# ═══════════════════════════════════════════════════════════════════════════════
# PART 1: Registry and backward-compatibility tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunnersRegistry:
    """Verify RUNNERS dict and old runners remain intact."""

    def test_claude_code_registered(self):
        assert "claude-code" in RUNNERS
        assert RUNNERS["claude-code"] is ClaudeCodeRunner

    def test_openai_compatible_registered(self):
        assert "openai-compatible" in RUNNERS
        assert RUNNERS["openai-compatible"] is OpenAICompatibleRunner

    def test_all_runners_are_subclasses(self):
        for key, cls in RUNNERS.items():
            assert issubclass(cls, BaseRunner), f"{key} is not an EvalRunner subclass"

    def test_exports_complete(self):
        from agent_eval.agent import __all__
        assert "EvalRunner" in __all__
        assert "RunResult" in __all__
        assert "ClaudeCodeRunner" in __all__
        assert "OpenAICompatibleRunner" in __all__
        assert "RUNNERS" in __all__

    def test_claude_code_runner_instantiates(self):
        runner = ClaudeCodeRunner()
        assert runner.name == "claude-code"

    def test_openai_compatible_runner_name(self):
        runner = OpenAICompatibleRunner(base_url="http://localhost:8000")
        assert runner.name == "openai-compatible"


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2: OpenAICompatibleRunner unit tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestOpenAICompatibleInit:
    """Test constructor validation and defaults."""

    def test_requires_base_url(self, monkeypatch):
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        with pytest.raises(ValueError, match="requires base_url"):
            OpenAICompatibleRunner()

    def test_accepts_base_url_arg(self):
        runner = OpenAICompatibleRunner(base_url="http://localhost:8000")
        assert runner._base_url == "http://localhost:8000"

    def test_reads_base_url_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "http://env-host:9000")
        runner = OpenAICompatibleRunner()
        assert runner._base_url == "http://env-host:9000"

    def test_arg_overrides_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "http://env-host:9000")
        runner = OpenAICompatibleRunner(base_url="http://arg-host:8000")
        assert runner._base_url == "http://arg-host:8000"

    def test_reads_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        runner = OpenAICompatibleRunner(base_url="http://localhost:8000")
        assert runner._api_key == "sk-test-123"

    def test_api_key_arg_overrides_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        runner = OpenAICompatibleRunner(
            base_url="http://localhost:8000", api_key="sk-arg")
        assert runner._api_key == "sk-arg"

    def test_default_params(self):
        runner = OpenAICompatibleRunner(base_url="http://localhost:8000")
        assert runner._max_tokens == 512
        assert runner._temperature == 0.3
        assert runner._system_prompt is None
        assert runner._default_model == ""

    def test_custom_params(self):
        runner = OpenAICompatibleRunner(
            base_url="http://localhost:8000",
            default_model="mistral-7b",
            system_prompt="You are helpful.",
            max_tokens=1024,
            temperature=0.7,
        )
        assert runner._default_model == "mistral-7b"
        assert runner._system_prompt == "You are helpful."
        assert runner._max_tokens == 1024
        assert runner._temperature == 0.7


# ═══════════════════════════════════════════════════════════════════════════════
# PART 3: run_skill() with a real local HTTP mock server
# ═══════════════════════════════════════════════════════════════════════════════


def _make_completion_response(content="Hello world", prompt_tokens=10,
                              completion_tokens=5, model="test-model"):
    """Build a standard OpenAI-compatible completion response."""
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class _MockHandler(BaseHTTPRequestHandler):
    """HTTP handler that returns configurable responses."""

    response_body = _make_completion_response()
    response_code = 200
    last_request_body = None
    last_request_headers = None

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        _MockHandler.last_request_body = json.loads(body)
        _MockHandler.last_request_headers = dict(self.headers)

        self.send_response(_MockHandler.response_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(_MockHandler.response_body).encode())

    def log_message(self, format, *args):
        pass  # Suppress noisy HTTP logging


@pytest.fixture
def mock_server():
    """Start a local HTTP server and yield its base URL."""
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(autouse=True)
def reset_handler():
    """Reset mock handler state between tests."""
    _MockHandler.response_body = _make_completion_response()
    _MockHandler.response_code = 200
    _MockHandler.last_request_body = None
    _MockHandler.last_request_headers = None


class TestRunSkillSuccess:
    """Tests for successful completion calls."""

    def test_basic_completion(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="review",
            args="Please review this code",
            workspace=tmp_path,
            model="test-model",
        )
        assert result.exit_code == 0
        assert result.stdout == "Hello world"
        assert result.stderr == ""
        assert result.num_turns == 1
        assert result.resolved_model == "test-model"
        assert result.duration_s > 0

    def test_response_written_to_artifacts(self, mock_server, tmp_path):
        _MockHandler.response_body = _make_completion_response(
            content="Review looks good!")
        runner = OpenAICompatibleRunner(base_url=mock_server)
        runner.run_skill(
            skill_name="review", args="code here",
            workspace=tmp_path, model="m",
        )
        artifact = tmp_path / "artifacts" / "response.md"
        assert artifact.exists()
        assert artifact.read_text() == "Review looks good!"

    def test_run_result_json_written(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(base_url=mock_server)
        runner.run_skill(
            skill_name="review", args="code",
            workspace=tmp_path, model="gpt-4",
        )
        result_file = tmp_path / "run_result.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text())
        assert data["exit_code"] == 0
        assert data["model"] == "gpt-4"
        assert data["num_turns"] == 1
        assert data["duration_s"] >= 0

    def test_stdout_log_written(self, mock_server, tmp_path):
        _MockHandler.response_body = _make_completion_response(
            content="response text")
        runner = OpenAICompatibleRunner(base_url=mock_server)
        runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        log = tmp_path / "stdout.log"
        assert log.exists()
        assert "response text" in log.read_text()

    def test_token_usage_extracted(self, mock_server, tmp_path):
        _MockHandler.response_body = _make_completion_response(
            prompt_tokens=42, completion_tokens=17)
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.token_usage == {"input": 42, "output": 17}

    def test_token_usage_in_run_result_json(self, mock_server, tmp_path):
        _MockHandler.response_body = _make_completion_response(
            prompt_tokens=100, completion_tokens=50)
        runner = OpenAICompatibleRunner(base_url=mock_server)
        runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        data = json.loads((tmp_path / "run_result.json").read_text())
        assert data["token_usage"] == {"input": 100, "output": 50}

    def test_no_usage_field_returns_none(self, mock_server, tmp_path):
        body = _make_completion_response()
        del body["usage"]
        _MockHandler.response_body = body
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.token_usage is None


class TestRunSkillRequestFormat:
    """Verify the HTTP request sent to the endpoint."""

    def test_sends_user_message(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(base_url=mock_server)
        runner.run_skill(
            skill_name="review", args="Review this diff",
            workspace=tmp_path, model="m",
        )
        body = _MockHandler.last_request_body
        messages = body["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Review this diff"

    def test_sends_system_prompt_from_constructor(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(
            base_url=mock_server, system_prompt="Be concise")
        runner.run_skill(
            skill_name="s", args="prompt",
            workspace=tmp_path, model="m",
        )
        messages = _MockHandler.last_request_body["messages"]
        assert len(messages) == 2
        assert messages[0] == {"role": "system", "content": "Be concise"}
        assert messages[1]["role"] == "user"

    def test_run_skill_system_prompt_overrides_constructor(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(
            base_url=mock_server, system_prompt="Default system")
        runner.run_skill(
            skill_name="s", args="prompt",
            workspace=tmp_path, model="m",
            system_prompt="Override system",
        )
        messages = _MockHandler.last_request_body["messages"]
        assert messages[0]["content"] == "Override system"

    def test_model_sent_in_request(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(base_url=mock_server)
        runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="mistral-7b-instruct",
        )
        assert _MockHandler.last_request_body["model"] == "mistral-7b-instruct"

    def test_default_model_used_when_arg_empty(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(
            base_url=mock_server, default_model="llama-3-8b")
        runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="",
        )
        assert _MockHandler.last_request_body["model"] == "llama-3-8b"

    def test_max_tokens_and_temperature_sent(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(
            base_url=mock_server, max_tokens=2048, temperature=0.9)
        runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        body = _MockHandler.last_request_body
        assert body["max_tokens"] == 2048
        assert body["temperature"] == 0.9

    def test_authorization_header_sent(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(
            base_url=mock_server, api_key="sk-secret-key")
        runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert _MockHandler.last_request_headers["Authorization"] == "Bearer sk-secret-key"

    def test_no_auth_header_when_no_key(self, mock_server, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        runner = OpenAICompatibleRunner(base_url=mock_server)
        runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert "Authorization" not in _MockHandler.last_request_headers


class TestRunSkillURLConstruction:
    """Test URL path appending logic."""

    def test_appends_path_to_bare_host(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        # Should succeed — means it hit mock_server/v1/chat/completions
        assert result.exit_code == 0

    def test_does_not_double_append_full_path(self, mock_server, tmp_path):
        full_url = f"{mock_server}/v1/chat/completions"
        runner = OpenAICompatibleRunner(base_url=full_url)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.exit_code == 0

    def test_trailing_slash_stripped(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(base_url=f"{mock_server}/")
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.exit_code == 0


class TestRunSkillErrors:
    """Test error handling paths."""

    def test_http_error_returns_nonzero_exit(self, mock_server, tmp_path):
        _MockHandler.response_code = 500
        _MockHandler.response_body = {"error": "Internal server error"}
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.exit_code == 1
        assert "HTTP 500" in result.stderr

    def test_http_401_reports_auth_error(self, mock_server, tmp_path):
        _MockHandler.response_code = 401
        _MockHandler.response_body = {"error": "Unauthorized"}
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.exit_code == 1
        assert "401" in result.stderr

    def test_http_429_reports_rate_limit(self, mock_server, tmp_path):
        _MockHandler.response_code = 429
        _MockHandler.response_body = {"error": "Rate limit exceeded"}
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.exit_code == 1
        assert "429" in result.stderr

    def test_connection_error_returns_nonzero_exit(self, tmp_path):
        runner = OpenAICompatibleRunner(
            base_url="http://127.0.0.1:1")  # nothing listening
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
            timeout_s=3,
        )
        assert result.exit_code == 1
        assert "Connection" in result.stderr or "error" in result.stderr.lower()

    def test_error_written_to_artifact(self, mock_server, tmp_path):
        _MockHandler.response_code = 503
        _MockHandler.response_body = {"error": "Service unavailable"}
        runner = OpenAICompatibleRunner(base_url=mock_server)
        runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        error_file = tmp_path / "artifacts" / "error.txt"
        assert error_file.exists()
        assert "503" in error_file.read_text()
        # No response.md when errored
        assert not (tmp_path / "artifacts" / "response.md").exists()

    def test_run_result_json_on_error(self, mock_server, tmp_path):
        _MockHandler.response_code = 500
        runner = OpenAICompatibleRunner(base_url=mock_server)
        runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        data = json.loads((tmp_path / "run_result.json").read_text())
        assert data["exit_code"] == 1
        assert data["num_turns"] == 1

    def test_empty_response_text_treated_as_error(self, mock_server, tmp_path):
        body = _make_completion_response(content="")
        _MockHandler.response_body = body
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        # Empty content → exit_code 0 but no response.md written
        # (because the condition is `exit_code == 0 and response_text`)
        assert not (tmp_path / "artifacts" / "response.md").exists()


class TestRunSkillReturnType:
    """Verify RunResult fields and types."""

    def test_returns_run_result_instance(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert isinstance(result, RunResult)

    def test_duration_is_positive_float(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert isinstance(result.duration_s, float)
        assert result.duration_s > 0

    def test_cost_usd_is_none(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        # OpenAI-compatible endpoints don't report cost
        assert result.cost_usd is None

    def test_optional_fields_are_none(self, mock_server, tmp_path):
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.models_used is None
        assert result.per_model_usage is None
        assert result.per_model_turns is None
        assert result.permission_denials is None
        assert result.raw_output is None


# ═══════════════════════════════════════════════════════════════════════════════
# PART 4: Integration with EvalRunner ABC
# ═══════════════════════════════════════════════════════════════════════════════


class TestABCCompliance:
    """Verify OpenAICompatibleRunner fully implements EvalRunner."""

    def test_is_subclass_of_eval_runner(self):
        assert issubclass(OpenAICompatibleRunner, EvalRunner)

    def test_implements_name_property(self):
        runner = OpenAICompatibleRunner(base_url="http://localhost:8000")
        assert isinstance(runner.name, str)
        assert runner.name == "openai-compatible"

    def test_implements_run_skill_method(self):
        assert callable(getattr(OpenAICompatibleRunner, "run_skill", None))

    def test_run_skill_signature_matches_abc(self):
        """Ensure the runner accepts all parameters defined by the ABC."""
        import inspect
        abc_sig = inspect.signature(EvalRunner.run_skill)
        impl_sig = inspect.signature(OpenAICompatibleRunner.run_skill)
        abc_params = set(abc_sig.parameters.keys())
        impl_params = set(impl_sig.parameters.keys())
        assert abc_params == impl_params, (
            f"Signature mismatch: ABC has {abc_params - impl_params} extra, "
            f"impl has {impl_params - abc_params} extra"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PART 5: Edge cases and robustness
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_large_response_handled(self, mock_server, tmp_path):
        large_content = "x" * 100_000
        _MockHandler.response_body = _make_completion_response(
            content=large_content)
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.exit_code == 0
        assert len(result.stdout) == 100_000

    def test_unicode_response(self, mock_server, tmp_path):
        _MockHandler.response_body = _make_completion_response(
            content="你好世界 🌍 مرحبا")
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.exit_code == 0
        assert "你好世界" in result.stdout

    def test_special_chars_in_prompt(self, mock_server, tmp_path):
        prompt = 'Review this: `def f(): "quoted" & <html>`'
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args=prompt,
            workspace=tmp_path, model="m",
        )
        assert result.exit_code == 0
        sent = _MockHandler.last_request_body["messages"][-1]["content"]
        assert sent == prompt

    def test_workspace_created_if_not_exists(self, mock_server, tmp_path):
        workspace = tmp_path / "deep" / "nested" / "workspace"
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=workspace, model="m",
        )
        assert result.exit_code == 0
        assert (workspace / "artifacts" / "response.md").exists()
        assert (workspace / "run_result.json").exists()

    def test_multiline_response(self, mock_server, tmp_path):
        content = "Line 1\nLine 2\nLine 3\n"
        _MockHandler.response_body = _make_completion_response(content=content)
        runner = OpenAICompatibleRunner(base_url=mock_server)
        result = runner.run_skill(
            skill_name="s", args="a",
            workspace=tmp_path, model="m",
        )
        assert result.exit_code == 0
        assert (tmp_path / "artifacts" / "response.md").read_text() == content
