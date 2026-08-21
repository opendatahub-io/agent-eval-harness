"""Tool interception configuration for headless agent evaluation.

Generates the artifacts that let an agent run headlessly with tool calls
auto-answered or denied:

- ``tool_handlers.yaml`` — per-tool handler config (match patterns, prompts)
- ``.claude/settings.json`` — PreToolUse hooks wiring (Claude Code specific;
  other agents ignore it)
- ``hooks/tools.py`` — the runtime interceptor script (copied from the harness)

Used by both the local workspace setup (``skills/eval-run/scripts/workspace.py``)
and Harbor task generation (``agent_eval/harbor/tasks.py``). The runtime
interceptor itself lives at ``skills/eval-run/scripts/tools.py``.
"""

import json
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from agent_eval.tools.permissions import compile_permission_rules

import yaml

if TYPE_CHECKING:
    from agent_eval.config import EvalConfig

_KNOWN_TOOLS = [
    "AskUserQuestion", "Bash", "Read", "Write", "Edit",
    "Glob", "Grep", "Agent", "Skill",
]

_INTERCEPTOR_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "skills" / "eval-run" / "scripts" / "tools.py"
)

#: Explicit wall-clock timeout (seconds) for every generated PreToolUse hook
#: entry. Sized to the worst case of one AskUserQuestion batch: ~30s primary
#: LLM answer + ~15s calibration shadow per question, times a small
#: question-batch factor (a batch carries a handful of questions) ≈ 120s.
#: The interceptor enforces its own in-hook deadline budget BELOW this bound
#: (see the mirrored constants in skills/eval-run/scripts/tools.py) so
#: optional calls degrade to a ledger-recorded skip instead of an external
#: kill — a killed PreToolUse hook is silent pass-through.
HOOK_TIMEOUT_SECONDS = 120


def extract_tool_patterns(match_text: str) -> list[str]:
    """Extract tool name patterns from a natural-language match description.

    Looks for known tool names and ``mcp__*`` patterns. This is a heuristic —
    eval-run's agent can refine these to concrete patterns at runtime.
    """
    patterns: list[str] = []
    for tool in _KNOWN_TOOLS:
        if tool.lower() in match_text.lower():
            patterns.append(tool)
    for m in re.finditer(r"(mcp__\w+(?:__\w+)*(?:\*)?)", match_text):
        patterns.append(m.group(1))
    if not patterns and ("script" in match_text.lower() or "api" in match_text.lower()):
        patterns.append("Bash")
    return patterns or ["*"]


def build_handlers(config: "EvalConfig") -> tuple[dict, set[str]]:
    """Build the tool-interception handler config from ``config.inputs.tools``.

    Returns ``(handler_data, hook_matchers)`` where ``handler_data`` is the dict
    to write as ``tool_handlers.yaml`` and ``hook_matchers`` is the set of tool
    name patterns that need PreToolUse hooks.
    """
    handlers: list[dict] = []
    hook_matchers: set[str] = set()
    for tool_cfg in config.inputs.tools:
        handler: dict = {"match": tool_cfg.match}
        patterns = extract_tool_patterns(tool_cfg.match)
        handler["patterns"] = patterns
        if tool_cfg.prompt:
            handler["prompt"] = tool_cfg.prompt
        if tool_cfg.prompt_file:
            handler["prompt_file"] = tool_cfg.prompt_file
        handlers.append(handler)
        hook_matchers.update(patterns)

    handler_data: dict = {"handlers": handlers}
    if config.models.hook:
        handler_data["hook_model"] = config.models.hook
    return handler_data, hook_matchers


def _patterns_hit_ask_user(patterns) -> bool:
    """True when a handler's patterns would match AskUserQuestion at runtime.

    Mirrors tools.py ``_find_handler``: exact name, or a trailing-``*``
    prefix pattern (the bare ``*`` wildcard prefix-matches every tool).
    """
    for pattern in patterns or []:
        if pattern == "AskUserQuestion":
            return True
        if (isinstance(pattern, str) and pattern.endswith("*")
                and "AskUserQuestion".startswith(pattern[:-1])):
            return True
    return False


def merge_handler_knobs(handler_data: dict, config: "EvalConfig") -> dict:
    """Post-load merge of harness-owned runtime knobs onto ``handler_data``.

    Applied to the handler config REGARDLESS of source — the heuristic
    :func:`build_handlers` output AND a pre-resolved ``tool_handlers.yaml``
    (whose load path bypasses ``build_handlers`` entirely). eval.yaml owns
    the runtime knobs (``hook_model`` from ``models.hook``, ``calibration``
    from ``inputs.tools``); the resolved file owns patterns / input_filters /
    env_checks / case_overrides (+ their ``case_overrides_source`` / per-entry
    ``source`` provenance), which flow through untouched.

    - ``hook_model``: ``setdefault`` from ``config.models.hook`` — an
      explicit value in the resolved file wins. Deliberate behavior fix,
      announced on stderr: resolved files that omitted ``hook_model``
      previously fell back to the interceptor's hardcoded haiku default even
      when eval.yaml set ``models.hook``.
    - ``calibration``: joined onto handlers by exact ``match`` text; when a
      calibration-enabled tool config joins nothing (the eval-run agent
      rewrote the match text), it falls back to every handler whose patterns
      would match AskUserQuestion, with a stderr warning naming the
      unjoined match.
    """
    handlers = handler_data.get("handlers") or []
    by_match = {h.get("match"): h for h in handlers if isinstance(h, dict)}
    for tool_cfg in config.inputs.tools:
        if not getattr(tool_cfg, "calibration", False):
            continue
        joined = by_match.get(tool_cfg.match)
        if joined is not None:
            joined["calibration"] = True
            continue
        fallback = [h for h in handlers if isinstance(h, dict)
                    and _patterns_hit_ask_user(h.get("patterns"))]
        for h in fallback:
            h["calibration"] = True
        print(
            f"tool_handlers.yaml: no handler matches inputs.tools entry "
            f"{tool_cfg.match!r} — applying calibration: true to "
            f"{len(fallback)} AskUserQuestion-matching handler(s)",
            file=sys.stderr)
    if config.models.hook and "hook_model" not in handler_data:
        handler_data["hook_model"] = config.models.hook
        print(
            f"tool_handlers.yaml: resolved file lacked hook_model — "
            f"supplied {config.models.hook!r} from models.hook (previously "
            "the interceptor silently fell back to its hardcoded default)",
            file=sys.stderr)
    return handler_data


def build_settings_hooks(hook_matchers: set[str], hooks_command: str) -> dict:
    """Build the ``.claude/settings.json`` PreToolUse hooks block.

    Every hook entry carries an explicit ``timeout`` of
    :data:`HOOK_TIMEOUT_SECONDS` — sized to the interceptor's worst-case
    LLM work per AskUserQuestion batch, and paired with the in-hook
    deadline budget in tools.py that skips optional calls before the CLI
    would kill the hook (silent pass-through).

    Args:
        hook_matchers: tool name patterns to intercept.
        hooks_command: the shell command each hook runs (path to the interceptor).

    Returns a settings dict with ``{"hooks": {"PreToolUse": [...]}}``.
    """
    settings: dict = {"hooks": {"PreToolUse": []}}
    for matcher in sorted(hook_matchers):
        settings["hooks"]["PreToolUse"].append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": hooks_command,
                       "timeout": HOOK_TIMEOUT_SECONDS}],
        })
    return settings


def generate_interception(
    target_dir: Path,
    config: "EvalConfig",
    hooks_command: str,
    resolved_handlers_path: Path | None = None,
) -> set[str]:
    """Generate all tool-interception artifacts into ``target_dir``.

    Creates ``hooks/tools.py``, ``tool_handlers.yaml``, and
    ``.claude/settings.json`` (with PreToolUse hooks). Returns the set of
    hook matchers (for callers that need to extend the settings further).

    If ``resolved_handlers_path`` points at an existing ``tool_handlers.yaml``
    (e.g. one generated by ``/eval-analyze`` with LLM-resolved ``input_filters``,
    ``env_checks``, ``case_overrides``), it is used as-is — the heuristic
    :func:`build_handlers` is skipped. This lets ``/eval-analyze`` do the LLM
    work once, producing a resolved file alongside ``eval.yaml``, which task
    generation and Harbor bundle unchanged. ``/eval-run`` Step 3a can still
    refine the workspace copy at execution time.

    Either way, :func:`merge_handler_knobs` then stamps the harness-owned
    runtime knobs (``hook_model``, ``calibration``) onto the handler data —
    the resolved-file branch would otherwise bypass them entirely.

    Args:
        target_dir: workspace or ``environment/`` dir to write into.
        config: EvalConfig with ``inputs.tools`` populated.
        hooks_command: the shell command for the PreToolUse hook
            (e.g. ``python3 /workspace/hooks/tools.py``).
        resolved_handlers_path: optional path to a pre-resolved
            ``tool_handlers.yaml`` (from ``/eval-analyze``).
    """
    if not config.inputs.tools:
        return set()

    # Copy interceptor script
    hooks_dir = target_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    if not _INTERCEPTOR_SCRIPT.is_file():
        raise FileNotFoundError(
            f"Tool interceptor script not found: {_INTERCEPTOR_SCRIPT}")
    shutil.copy2(_INTERCEPTOR_SCRIPT, hooks_dir / "tools.py")

    # Handler config: prefer a pre-resolved file (from /eval-analyze, with
    # LLM-resolved input_filters/env_checks/case_overrides), fall back to
    # heuristic extraction from eval.yaml's natural-language match text.
    if resolved_handlers_path and resolved_handlers_path.is_file():
        handler_data = yaml.safe_load(resolved_handlers_path.read_text()) or {}
        hook_matchers: set[str] = set()
        for h in handler_data.get("handlers", []):
            hook_matchers.update(h.get("patterns", []))
    else:
        handler_data, hook_matchers = build_handlers(config)
    # Harness-owned knobs ride eval.yaml, whatever produced the handlers.
    handler_data = merge_handler_knobs(handler_data, config)
    (target_dir / "tool_handlers.yaml").write_text(
        yaml.safe_dump(handler_data, sort_keys=False))

    # .claude/settings.json hooks
    claude_dir = target_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = build_settings_hooks(hook_matchers, hooks_command)

    # Carry permissions from eval.yaml, compiling path-based rules into valid
    # Claude Code patterns (see agent_eval/tools/permissions.py) so the task
    # package never ships raw {path, tools} dicts (which are invalid rules).
    allow = (config.permissions or {}).get("allow")
    deny = (config.permissions or {}).get("deny")
    if allow or deny:
        perms: dict = {}
        if allow:
            perms["allow"] = compile_permission_rules(allow)
        if deny:
            perms["deny"] = compile_permission_rules(deny, harden_bash=True)
        settings["permissions"] = perms

    # Inject execution.env
    if config.execution.env:
        settings["env"] = {k: str(v) for k, v in config.execution.env.items()
                           if v is not None}

    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2))

    return hook_matchers
