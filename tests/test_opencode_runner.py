"""Tests for the OpenCode runner."""

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_eval.agent.opencode import OpenCodeRunner
from agent_eval.config import EvalConfig, OTelConfig, RunnerConfig


class TestOpenCodeRunnerConfig:

    def test_from_config(self, tmp_path):
        config_yaml = tmp_path / "eval.yaml"
        config_yaml.write_text("""
name: test
runner:
  type: opencode
  otel:
    enabled: true
""")
        config = EvalConfig.from_yaml(config_yaml)
        runner = OpenCodeRunner.from_config(config)
        assert runner.name == "opencode"

    def test_name_property(self):
        runner = OpenCodeRunner()
        assert runner.name == "opencode"


class TestOpenCodeRunnerEnv:

    def test_build_env_inherits_full_env(self):
        runner = OpenCodeRunner()
        with patch.dict("os.environ", {"PATH": "/bin", "HOME": "/home", "SECRET": "x"}):
            env = runner._build_env(otel_port=None, workspace=Path("/tmp"))
        assert "PATH" in env
        assert "HOME" in env
        assert "SECRET" in env

    def test_build_env_extra_env_literal(self):
        runner = OpenCodeRunner(env={"GCP_PROJECT": "my-project"})
        with patch.dict("os.environ", {"PATH": "/bin"}, clear=True):
            env = runner._build_env(otel_port=None, workspace=Path("/tmp"))
        assert env["GCP_PROJECT"] == "my-project"

    def test_build_env_extra_env_dollar_ref(self):
        runner = OpenCodeRunner(env={"GCP_PROJECT": "$MY_PROJECT"})
        with patch.dict("os.environ", {"PATH": "/bin", "MY_PROJECT": "resolved-project"}, clear=True):
            env = runner._build_env(otel_port=None, workspace=Path("/tmp"))
        assert env["GCP_PROJECT"] == "resolved-project"

    def test_build_env_extra_env_dollar_ref_missing(self):
        runner = OpenCodeRunner(env={"GCP_PROJECT": "$MISSING_VAR"})
        with patch.dict("os.environ", {"PATH": "/bin"}, clear=True):
            env = runner._build_env(otel_port=None, workspace=Path("/tmp"))
        assert "GCP_PROJECT" not in env

    def test_build_env_injects_otel(self):
        otel = OTelConfig(enabled=True, resource_attributes={"env": "eval"})
        runner = OpenCodeRunner(otel_config=otel)
        with patch.dict("os.environ", {"PATH": "/bin"}, clear=True):
            env = runner._build_env(otel_port=12345, workspace=Path("/tmp"))
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:12345"
        assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"
        assert env["OTEL_RESOURCE_ATTRIBUTES"] == "env=eval"

    def test_build_env_no_otel_when_no_port(self):
        otel = OTelConfig(enabled=True)
        runner = OpenCodeRunner(otel_config=otel)
        with patch.dict("os.environ", {"PATH": "/bin"}, clear=True):
            env = runner._build_env(otel_port=None, workspace=Path("/tmp"))
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env


class TestOpenCodeRunnerProgress:

    def test_extract_text_progress(self):
        import json
        line = json.dumps({"type": "text", "part": {"text": "Analyzing code..."}})
        assert OpenCodeRunner._extract_progress(line) == "Analyzing code..."

    def test_extract_tool_progress(self):
        import json
        line = json.dumps({"type": "tool_call", "name": "bash"})
        assert OpenCodeRunner._extract_progress(line) == "Tool: bash"

    def test_invalid_json_returns_empty(self):
        assert OpenCodeRunner._extract_progress("not json") == ""

    def test_long_text_skipped(self):
        import json
        line = json.dumps({"type": "text", "part": {"text": "x" * 200}})
        assert OpenCodeRunner._extract_progress(line) == ""


class TestOpenCodeRunnerRegistry:

    def test_registered_in_runners(self):
        from agent_eval.agent import RUNNERS
        assert "opencode" in RUNNERS
        assert RUNNERS["opencode"] is OpenCodeRunner
