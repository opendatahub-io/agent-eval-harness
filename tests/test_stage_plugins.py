"""Tests for staging plugin dirs inside the case workspace (stage_plugins).

Passing ``--plugin-dir <real project path>`` puts that path verbatim in every
session's system context (the stream-json init event registers the plugin
under it). Bash is not path-gated in isolated workspaces, so a case agent can
follow the leaked path out of its throwaway workspace and into the real repo
— observed in the 2026-08-19 epic-creator eval run, where two of 30 case
dispatchers ran pipeline phases inside the real project. ``stage_plugins``
copies the plugin's discoverable content into the workspace and passes THAT
path instead.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent.claude_code import ClaudeCodeRunner, stage_plugin_dir
from agent_eval.config import EvalConfig


def make_plugin(root, name="epic-creator", manifest=..., skills=("my-skill",)):
    """Create a minimal plugin: manifest + skills/<name>/SKILL.md."""
    plugin = root / name
    (plugin / ".claude-plugin").mkdir(parents=True)
    if manifest is ...:
        manifest = {"name": name}
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    for skill in skills:
        skill_dir = plugin / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n")
    return plugin


class TestStagePluginDir:
    """Unit tests for the staging helper."""

    def test_copies_manifest_and_skill_root(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert staged == ws / ".staged-plugins" / "epic-creator"
        assert (staged / ".claude-plugin" / "plugin.json").is_file()
        # The staged tree must remain discoverable: a SKILL.md under the
        # staged skill root is what Claude Code's plugin loader looks for.
        assert (staged / "skills" / "my-skill" / "SKILL.md").is_file()

    def test_copies_optional_dirs(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        (plugin / "commands").mkdir()
        (plugin / "commands" / "go.md").write_text("cmd")
        (plugin / "agents").mkdir()
        (plugin / "agents" / "helper.md").write_text("agent")
        (plugin / "hooks").mkdir()
        (plugin / "hooks" / "hooks.json").write_text("{}")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert (staged / "commands" / "go.md").is_file()
        assert (staged / "agents" / "helper.md").is_file()
        assert (staged / "hooks" / "hooks.json").is_file()

    def test_skips_git_node_modules_and_unlisted_files(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        # Bulk inside a copied tree is pruned by ignore patterns...
        (plugin / "skills" / ".git").mkdir()
        (plugin / "skills" / ".git" / "config").write_text("x")
        (plugin / "skills" / "node_modules" / "pkg").mkdir(parents=True)
        (plugin / "skills" / "node_modules" / "pkg" / "i.js").write_text("x")
        # ...and content outside the discovery set is never copied at all.
        (plugin / ".git").mkdir()
        (plugin / ".git" / "config").write_text("x")
        (plugin / "README.md").write_text("readme")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert not (staged / "skills" / ".git").exists()
        assert not (staged / "skills" / "node_modules").exists()
        assert not (staged / ".git").exists()
        assert not (staged / "README.md").exists()

    def test_manifest_declared_skill_root(self, tmp_path):
        plugin = make_plugin(
            tmp_path / "plugins",
            manifest={"name": "p", "skills": "custom-skills"},
            skills=(),
        )
        alpha = plugin / "custom-skills" / "alpha"
        alpha.mkdir(parents=True)
        (alpha / "SKILL.md").write_text("# alpha\n")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert (staged / "custom-skills" / "alpha" / "SKILL.md").is_file()

    def test_idempotent_per_workspace(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        sentinel = staged / "sentinel.txt"
        sentinel.write_text("kept")
        again = stage_plugin_dir(plugin, ws)
        assert again == staged
        assert sentinel.read_text() == "kept", "second call must not recopy"

    def test_symlinks_are_materialized_not_reproduced(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        outside = tmp_path / "outside.txt"
        outside.write_text("content")
        (plugin / "skills" / "my-skill" / "link.txt").symlink_to(outside)
        (plugin / "skills" / "my-skill" / "dangling.txt").symlink_to(
            tmp_path / "does-not-exist")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        copied = staged / "skills" / "my-skill" / "link.txt"
        assert copied.is_file() and not copied.is_symlink()
        assert copied.read_text() == "content"
        assert not (staged / "skills" / "my-skill" / "dangling.txt").exists()

    def test_plugin_without_skills_is_tolerated(self, tmp_path):
        # A Claude plugin may ship only commands/agents/hooks; staging must
        # not fail a configuration the unstaged path would have accepted.
        plugin = make_plugin(tmp_path / "plugins", skills=())
        (plugin / "commands").mkdir()
        (plugin / "commands" / "go.md").write_text("cmd")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert (staged / ".claude-plugin" / "plugin.json").is_file()
        assert (staged / "commands" / "go.md").is_file()


class TestClaudeCodeRunnerStaging:
    """Integration through ClaudeCodeRunner.execute with a stub `claude` on
    PATH that dumps its argv into the workspace (its cwd)."""

    JSON_OK = ('{"type":"result","subtype":"success","is_error":false,'
               '"num_turns":3,"total_cost_usd":0.1,"result":"done"}\n')

    def _stub_claude(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        stub = bindir / "claude"
        stub.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > argv.txt\n"
            "cat > /dev/null\n"
            f"cat <<'JSON_EOF'\n{self.JSON_OK}JSON_EOF\n"
        )
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    def _argv(self, ws):
        return (ws / "argv.txt").read_text().splitlines()

    def _plugin_dir_args(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "--plugin-dir"]

    def test_staged_path_replaces_real_path(self, tmp_path, monkeypatch):
        self._stub_claude(tmp_path, monkeypatch)
        plugin = make_plugin(tmp_path / "plugins")
        ws = tmp_path / "ws"
        ws.mkdir()
        runner = ClaudeCodeRunner(plugin_dirs=[str(plugin)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        argv = self._argv(ws)
        [staged] = self._plugin_dir_args(argv)
        assert staged.startswith(str(ws)), (
            "--plugin-dir must point inside the workspace")
        assert str(plugin) not in "\n".join(argv), (
            "the real plugin path must never enter the session")
        assert (Path(staged) / "skills" / "my-skill" / "SKILL.md").is_file()

    def test_staging_is_the_default_behavior(self, tmp_path, monkeypatch):
        """No opt-in flag: isolation is the harness's contract, so any plugin
        living outside the workspace is always staged."""
        self._stub_claude(tmp_path, monkeypatch)
        plugin = make_plugin(tmp_path / "plugins")
        ws = tmp_path / "ws"
        ws.mkdir()
        runner = ClaudeCodeRunner(plugin_dirs=[str(plugin)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        [staged] = self._plugin_dir_args(self._argv(ws))
        assert staged.startswith(str(ws))
        assert (ws / ".staged-plugins").exists()

    def test_plugin_inside_workspace_passes_through(self, tmp_path, monkeypatch):
        """A plugin already inside the workspace discloses nothing outside the
        sandbox and is passed through unchanged — this is also what makes
        workspace_mode: repo safe (the workspace IS the project there, and
        staging would write .staged-plugins/ into the user's repo)."""
        self._stub_claude(tmp_path, monkeypatch)
        ws = tmp_path / "ws"
        ws.mkdir()
        plugin = make_plugin(ws / "vendored")
        runner = ClaudeCodeRunner(plugin_dirs=[str(plugin)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        assert self._plugin_dir_args(self._argv(ws)) == [str(plugin.resolve())]
        assert not (ws / ".staged-plugins").exists()

    def test_plugin_root_scripts_are_staged(self, tmp_path, monkeypatch):
        """Skills commonly run ${CLAUDE_PLUGIN_ROOT}/scripts/... at runtime; a
        staged copy without scripts/ would break them."""
        self._stub_claude(tmp_path, monkeypatch)
        plugin = make_plugin(tmp_path / "plugins")
        (plugin / "scripts").mkdir()
        (plugin / "scripts" / "helper.py").write_text("print('hi')\n")
        ws = tmp_path / "ws"
        ws.mkdir()
        runner = ClaudeCodeRunner(plugin_dirs=[str(plugin)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        [staged] = self._plugin_dir_args(self._argv(ws))
        assert (Path(staged) / "scripts" / "helper.py").is_file()

    def test_duplicate_plugin_basenames_fail_loud(self, tmp_path, monkeypatch):
        self._stub_claude(tmp_path, monkeypatch)
        first = make_plugin(tmp_path / "a", name="pkg")
        second = make_plugin(tmp_path / "b", name="pkg")
        ws = tmp_path / "ws"
        ws.mkdir()
        runner = ClaudeCodeRunner(plugin_dirs=[str(first), str(second)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == -1
        assert "same directory name" in result.stderr
