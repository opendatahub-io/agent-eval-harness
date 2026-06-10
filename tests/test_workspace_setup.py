"""Tests for runner-specific workspace setup."""

import json
from pathlib import Path

import pytest

from agent_eval.agent.base import EvalRunner
from agent_eval.agent.claude_code import ClaudeCodeRunner
from agent_eval.agent.opencode import OpenCodeRunner
from agent_eval.config import EvalConfig


def _make_config(tmp_path, yaml_text):
    """Write eval.yaml and return parsed config."""
    p = tmp_path / "eval.yaml"
    p.write_text(yaml_text)
    return EvalConfig.from_yaml(p)


class TestBaseRunnerNoOp:

    def test_setup_workspace_is_noop(self, tmp_path):
        """Base EvalRunner.setup_workspace does nothing."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, "name: test\n")

        # Can't instantiate ABC directly, so check via a subclass
        # that doesn't override setup_workspace
        from agent_eval.agent.cli_runner import CliRunner
        runner = CliRunner(command="echo test")
        runner.setup_workspace(ws, config, project_root=tmp_path)

        # No files created
        assert not (ws / ".claude").exists()
        assert not (ws / "opencode.json").exists()


class TestClaudeCodeWorkspaceSetup:

    def test_writes_settings_json(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, "name: test\n")

        runner = ClaudeCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        settings_path = ws / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        assert "hooks" in settings

    def test_subagent_hook_configured(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, "name: test\n")

        runner = ClaudeCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        settings = json.loads((ws / ".claude" / "settings.json").read_text())
        assert "SubagentStop" in settings.get("hooks", {})

    def test_tool_hooks_when_inputs_tools(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, """
name: test
inputs:
  tools:
    - match: AskUserQuestion interactions
      prompt: Answer yes
""")

        runner = ClaudeCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        settings = json.loads((ws / ".claude" / "settings.json").read_text())
        assert "PreToolUse" in settings.get("hooks", {})
        assert (ws / "tool_handlers.yaml").exists()

    def test_tool_hooks_copies_interceptor(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        # Create a fake interceptor source
        interceptor = tmp_path / "tools.py"
        interceptor.write_text("# interceptor")

        config = _make_config(tmp_path, """
name: test
inputs:
  tools:
    - match: Bash commands
      prompt: Allow all
""")

        runner = ClaudeCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path,
                               interceptor_src=interceptor)

        assert (ws / "hooks" / "tools.py").exists()
        assert (ws / "hooks" / "tools.py").read_text() == "# interceptor"

    def test_project_root_in_additional_dirs(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, "name: test\n")

        runner = ClaudeCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        settings = json.loads((ws / ".claude" / "settings.json").read_text())
        dirs = settings.get("permissions", {}).get("additionalDirectories", [])
        assert str(tmp_path.resolve()) in dirs

    def test_harness_permissions_merged(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, """
name: test
permissions:
  allow:
    - Skill
    - Agent
""")

        runner = ClaudeCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        settings = json.loads((ws / ".claude" / "settings.json").read_text())
        allow = settings.get("permissions", {}).get("allow", [])
        assert "Skill" in allow
        assert "Agent" in allow

    def test_env_injected(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, """
name: test
execution:
  env:
    MY_VAR: hello
""")

        runner = ClaudeCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        settings = json.loads((ws / ".claude" / "settings.json").read_text())
        assert settings.get("env", {}).get("MY_VAR") == "hello"

    def test_runner_settings_applied(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, """
name: test
runner:
  type: claude-code
  settings:
    custom_key: custom_value
""")

        runner = ClaudeCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        settings = json.loads((ws / ".claude" / "settings.json").read_text())
        assert settings.get("custom_key") == "custom_value"


class TestOpenCodeWorkspaceSetup:

    def test_writes_opencode_json(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, "name: test\nrunner:\n  type: opencode\n")

        runner = OpenCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        assert (ws / "opencode.json").exists()
        data = json.loads((ws / "opencode.json").read_text())
        assert "$schema" in data

    def test_denies_task(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, "name: test\nrunner:\n  type: opencode\n")

        runner = OpenCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        data = json.loads((ws / "opencode.json").read_text())
        assert data["permission"]["task"] == "deny"

    def test_translates_deny_permissions(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, """
name: test
runner:
  type: opencode
permissions:
  deny:
    - mcp__atlassian
""")

        runner = OpenCodeRunner(permissions={"deny": ["mcp__atlassian"]})
        runner.setup_workspace(ws, config, project_root=tmp_path)

        data = json.loads((ws / "opencode.json").read_text())
        assert data["permission"]["mcp__atlassian"] == "deny"

    def test_includes_otel(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, """
name: test
runner:
  type: opencode
  otel:
    enabled: true
""")

        from agent_eval.config import OTelConfig
        runner = OpenCodeRunner(otel_config=OTelConfig(enabled=True))
        runner.setup_workspace(ws, config, project_root=tmp_path)

        data = json.loads((ws / "opencode.json").read_text())
        assert data.get("experimental", {}).get("openTelemetry") is True

    def test_no_otel_when_disabled(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, "name: test\nrunner:\n  type: opencode\n")

        runner = OpenCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        data = json.loads((ws / "opencode.json").read_text())
        assert "experimental" not in data

    def test_no_claude_settings(self, tmp_path):
        """OpenCode should NOT write .claude/settings.json."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        config = _make_config(tmp_path, "name: test\nrunner:\n  type: opencode\n")

        runner = OpenCodeRunner()
        runner.setup_workspace(ws, config, project_root=tmp_path)

        assert not (ws / ".claude" / "settings.json").exists()
