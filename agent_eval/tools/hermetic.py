"""Hermetic plugin isolation for eval case sessions (issue #193).

Claude Code loads the operator's user-installed plugins — registered in
``~/.claude/plugins/installed_plugins.json`` and toggled via ``enabledPlugins``
in settings — into every session, eval case sessions included. There is no
``enabledPlugins`` wildcard upstream (anthropics/claude-code#20873) and an
absent entry means *enabled*, so a hand-maintained denylist rots the moment
the operator installs a new plugin.

The harness instead synthesizes the denylist at workspace-setup time:
enumerate every installed plugin and emit ``enabledPlugins: {id: false}`` for
each, merged into the case workspace's ``.claude/settings.json`` *before* the
user's own ``runner.settings`` — so an explicit
``runner.settings.enabledPlugins`` entry still wins. This is the DEFAULT for
isolated workspaces (isolation is the workspace's contract); the
``enabledPlugins`` pseudo-entry ``"*"`` steers it explicitly — ``"*": false``
forces hermeticity, ``"*": true`` opts out — and is stripped before
settings.json is written, since upstream has no wildcard. Plugins the harness
passes via ``--plugin-dir`` register as ``<name>@inline`` and never appear in
the registry, so the generated denylist cannot touch them.
"""

import json
from pathlib import Path


def installed_plugins_registry() -> Path:
    """Path to the operator's user-level plugin registry."""
    return Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def installed_plugin_ids(registry_path: Path | str | None = None) -> list[str]:
    """IDs (``<name>@<marketplace>``) of the operator's installed plugins.

    Reads ``~/.claude/plugins/installed_plugins.json`` (format:
    ``{"version": 2, "plugins": {"<name>@<marketplace>": ...}}``) unless
    ``registry_path`` overrides the location (tests). A missing, unreadable,
    or unparseable registry yields ``[]`` — hermetic mode then has nothing to
    disable, which matches reality. Format drift is tolerated defensively:
    if ``"plugins"`` is a dict its keys are the IDs, anything else counts as
    no plugins.
    """
    path = (Path(registry_path) if registry_path is not None
            else installed_plugins_registry())
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    plugins = raw.get("plugins")
    if not isinstance(plugins, dict):
        return []
    return sorted(str(plugin_id) for plugin_id in plugins)


def hermetic_enabled_plugins(registry_path: Path | str | None = None) -> dict:
    """The synthesized ``enabledPlugins`` denylist: every installed plugin off."""
    return {plugin_id: False for plugin_id in installed_plugin_ids(registry_path)}
