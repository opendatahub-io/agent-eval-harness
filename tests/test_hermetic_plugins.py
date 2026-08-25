"""Tests for hermetic plugin isolation (issue #193).

The operator's user-installed plugins (registered in
``~/.claude/plugins/installed_plugins.json``) load into every case session:
there is no ``enabledPlugins`` wildcard upstream and an absent entry means
enabled. In an ISOLATED workspace the harness therefore synthesizes the
denylist by default — ``enabledPlugins: {id: false}`` for every registry
entry — merged into the workspace settings before ``runner.settings`` so
explicit user entries win. The pseudo-entry ``"*"`` steers the policy
(``false`` forces hermeticity even in repo mode, ``true`` opts out) and is
stripped before settings.json is written.

All tests use a fixture registry under a temp HOME or an injected registry
path; none reads the real ``~/.claude``.
"""

import json
import sys
from pathlib import Path

import pytest

# Ensure agent_eval is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig
from agent_eval.tools.hermetic import (
    hermetic_enabled_plugins,
    installed_plugin_ids,
)
import workspace  # skills/eval-run/scripts (sys.path via conftest)


REGISTRY_PLUGINS = {
    "memsearch@user-marketplace": {"version": "1.0.0"},
    "clangd-lsp@claude-code-marketplace": {},
    "skill-creator@claude-code-marketplace": {},
}


def _write_registry(home, plugins=None, text=None):
    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        text if text is not None
        else json.dumps({"version": 2, "plugins": plugins or REGISTRY_PLUGINS}))
    return registry


def _config(directory, runner_yaml):
    p = directory / "eval.yaml"
    p.write_text("name: t\nexecution:\n  skill: s\nrunner:\n" + runner_yaml)
    return EvalConfig.from_yaml(p)


# ── Registry parsing ─────────────────────────────────────────────────


def test_installed_plugin_ids_reads_registry(tmp_path):
    registry = _write_registry(tmp_path)
    assert installed_plugin_ids(registry) == sorted(REGISTRY_PLUGINS)


def test_installed_plugin_ids_missing_file(tmp_path):
    assert installed_plugin_ids(tmp_path / "nope.json") == []


def test_installed_plugin_ids_malformed_json(tmp_path):
    registry = _write_registry(tmp_path, text="{not json")
    assert installed_plugin_ids(registry) == []


@pytest.mark.parametrize("payload", [
    "[]",                                     # top level is not an object
    json.dumps({"version": 3}),               # no "plugins" key at all
    json.dumps({"plugins": ["a@b", "c@d"]}),  # "plugins" is not a mapping
    json.dumps({"plugins": None}),
])
def test_installed_plugin_ids_tolerates_format_drift(tmp_path, payload):
    registry = _write_registry(tmp_path, text=payload)
    assert installed_plugin_ids(registry) == []


def test_registry_wildcard_key_is_rejected(tmp_path):
    """A registry containing the reserved "*" key must not smuggle the
    harness-only pseudo-entry into the synthesized denylist — the wildcard
    strip in _apply_runner_settings covers only the user's own settings."""
    registry = _write_registry(
        tmp_path, plugins={**REGISTRY_PLUGINS, "*": {}})
    assert "*" not in installed_plugin_ids(registry)
    assert "*" not in hermetic_enabled_plugins(registry)
    assert set(hermetic_enabled_plugins(registry)) == set(REGISTRY_PLUGINS)


def test_installed_plugin_ids_default_path_is_home_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_registry(tmp_path)
    assert installed_plugin_ids() == sorted(REGISTRY_PLUGINS)


def test_denylist_only_covers_registry_ids_never_inline_plugins(tmp_path):
    """Plugins passed via --plugin-dir register as ``<name>@inline`` and are
    NOT in installed_plugins.json — so the generated denylist is exactly the
    registry IDs and cannot disable an inline plugin under test."""
    registry = _write_registry(tmp_path)
    generated = hermetic_enabled_plugins(registry)
    assert set(generated) == set(REGISTRY_PLUGINS)
    assert not any(pid.endswith("@inline") for pid in generated)
    assert all(value is False for value in generated.values())


# ── Merge precedence ─────────────────────────────────────────────────


def test_isolated_workspace_is_hermetic_by_default(tmp_path, monkeypatch):
    """No plugin config at all: isolation is the workspace's contract."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_registry(tmp_path)
    config = _config(tmp_path, "  type: claude-code\n")

    settings = {}
    workspace._apply_runner_settings(settings, config)

    assert settings["enabledPlugins"] == {
        plugin_id: False for plugin_id in REGISTRY_PLUGINS}


def test_repo_mode_is_not_hermetic_by_default(tmp_path, monkeypatch):
    """workspace_mode: repo runs in the user's real environment."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_registry(tmp_path)
    config = _config(tmp_path, "  workspace_mode: repo\n")

    settings = {}
    workspace._apply_runner_settings(settings, config)

    assert "enabledPlugins" not in settings


def test_wildcard_false_forces_hermetic_in_repo_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_registry(tmp_path)
    config = _config(
        tmp_path,
        "  workspace_mode: repo\n"
        "  settings:\n"
        "    enabledPlugins:\n"
        "      \"*\": false\n")

    settings = {}
    workspace._apply_runner_settings(settings, config)

    enabled = settings["enabledPlugins"]
    assert "*" not in enabled, "the pseudo-entry must never reach settings.json"
    assert all(enabled[pid] is False for pid in REGISTRY_PLUGINS)


def test_wildcard_true_opts_out_of_hermeticity(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_registry(tmp_path)
    config = _config(
        tmp_path,
        "  settings:\n"
        "    enabledPlugins:\n"
        "      \"*\": true\n")

    settings = {}
    workspace._apply_runner_settings(settings, config)

    assert settings.get("enabledPlugins", {}) == {}, (
        "opt-out must synthesize nothing and strip the pseudo-entry")


def test_explicit_runner_settings_entry_wins_over_denylist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_registry(tmp_path)
    config = _config(
        tmp_path,
        "  settings:\n"
        "    enabledPlugins:\n"
        "      memsearch@user-marketplace: true\n")

    settings = {}
    workspace._apply_runner_settings(settings, config)

    enabled = settings["enabledPlugins"]
    assert enabled["memsearch@user-marketplace"] is True  # explicit re-enable
    assert enabled["clangd-lsp@claude-code-marketplace"] is False
    assert enabled["skill-creator@claude-code-marketplace"] is False
    assert "*" not in enabled


def test_config_object_not_mutated_by_wildcard_stripping(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_registry(tmp_path)
    config = _config(
        tmp_path,
        "  settings:\n"
        "    enabledPlugins:\n"
        "      \"*\": false\n")

    workspace._apply_runner_settings({}, config)

    assert config.runner.settings["enabledPlugins"]["*"] is False, (
        "stripping must operate on a copy — a second workspace build "
        "would otherwise see a different policy")


# ── End-to-end workspace settings.json generation ────────────────────


def _generate_workspace_settings(tmp_path, monkeypatch, runner_yaml):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _write_registry(home)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)  # _carry_over_permissions reads cwd/.claude
    config = _config(project, runner_yaml)
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace._setup_subagent_only_hook(ws, config)
    return json.loads((ws / ".claude" / "settings.json").read_text())


def test_workspace_settings_json_hermetic_by_default(tmp_path, monkeypatch):
    settings = _generate_workspace_settings(
        tmp_path, monkeypatch, "  type: claude-code\n")
    assert settings["enabledPlugins"] == {
        plugin_id: False for plugin_id in REGISTRY_PLUGINS}
    # Harness defaults still composed alongside the denylist.
    assert "SubagentStop" in settings["hooks"]


def test_workspace_settings_json_opt_out_strips_wildcard(tmp_path, monkeypatch):
    settings = _generate_workspace_settings(
        tmp_path, monkeypatch,
        "  settings:\n"
        "    enabledPlugins:\n"
        "      \"*\": true\n")
    assert settings.get("enabledPlugins", {}) == {}
    assert "SubagentStop" in settings["hooks"]
