"""Helpers for model-agnostic prompt execution.

Anthropic-backed direct calls remain the fast path for Claude-family models, but
some eval flows (synthetic dataset generation, plain prompt judges) need to work
with runner-managed model ids such as Cursor's ``gpt-5.4-medium``.  This module
provides a small runner-backed fallback for those cases.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from agent_eval.agent import RUNNERS
from agent_eval.events import extract_conversation_text, parse_stream_events


_ANTHROPIC_MODEL_ALIASES = {"opus", "sonnet", "haiku"}


def is_anthropic_model(model: Optional[str]) -> bool:
    """Best-effort classifier for Anthropic/Claude model ids.

    The direct Anthropic client can only serve Claude-family models.  Other model
    ids (for example Cursor's ``gpt-5.4-medium``) need to go through a runner.
    """
    value = (model or "").strip().lower()
    if not value:
        return False
    if value in _ANTHROPIC_MODEL_ALIASES:
        return True
    return "claude" in value or value.startswith("anthropic/")


def extract_runner_text(result) -> str:
    """Extract visible assistant text from a runner ``RunResult``.

    Prefers normalized event data when present, then falls back to parsing the
    runner stdout stream, and finally returns raw stdout as a last resort.
    """
    raw = getattr(result, "raw_output", None)
    if isinstance(raw, dict):
        events = raw.get("events")
        if isinstance(events, list):
            text = extract_conversation_text(events)
            if text:
                return text.strip()

    stdout = getattr(result, "stdout", "") or ""
    try:
        events = parse_stream_events(stdout)
    except Exception:  # pragma: no cover - defensive fallback
        events = []
    if events:
        text = extract_conversation_text(events)
        if text:
            return text.strip()

    # Cursor emits JSONL with assistant text nested under message.content.
    parts = []
    terminal_result = None
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "result" and isinstance(obj.get("result"), str):
            terminal_result = obj["result"]
        if obj.get("type") != "assistant":
            continue
        message = obj.get("message", {}) or {}
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content.strip():
                parts.append(content.strip())
            continue
        if isinstance(content, list):
            text_bits = [
                str(block.get("text", "")).strip()
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
                and str(block.get("text", "")).strip()
            ]
            if text_bits:
                parts.append("\n".join(text_bits))
                continue
        text_value = message.get("text")
        if isinstance(text_value, str) and text_value.strip():
            parts.append(text_value.strip())

    if terminal_result and terminal_result.strip():
        return terminal_result.strip()
    if parts:
        return "\n\n".join(parts).strip()
    return stdout.strip()


def run_prompt_via_runner(
    config,
    prompt: str,
    model: str,
    *,
    timeout_s: int = 600,
    max_budget_usd: float = 5.0,
    permissions: Optional[dict] = None,
    system_prompt: Optional[str] = None,
    workspace: Optional[Path] = None,
    staged_files: Optional[dict[str, bytes]] = None,
):
    """Execute a single prompt through the configured runner.

    Returns ``(RunResult, extracted_text, workspace_path)``.  When the workspace
    is created internally, the caller is responsible for cleaning it up.
    """
    if config.runner.type not in RUNNERS:
        raise RuntimeError(
            f"Unknown runner '{config.runner.type}'. Available: {sorted(RUNNERS)}")

    created_workspace = workspace is None
    workspace_path = Path(workspace) if workspace else Path(
        tempfile.mkdtemp(prefix="prompt-backend-"))
    workspace_path.mkdir(parents=True, exist_ok=True)

    # Keep prompt-only backends isolated even when the evaluated skill runs in
    # repo mode; the prompt already carries the information they need.
    cfg = copy.copy(config)
    cfg.runner = copy.copy(config.runner)
    cfg.runner.workspace_mode = None
    cfg.runner.system_prompt = ""
    # Prompt-only backends (LLM judges, synthetic generation) must not inherit
    # the skill agent's permission_mode. Cursor maps ``plan`` to ``--mode plan``,
    # which makes judges explore the workspace instead of emitting a verdict.
    cfg.runner.permission_mode = None
    cfg.runner.settings = dict(cfg.runner.settings or {})
    # These are independent judge/generation invocations, not the evaluated
    # skill. Do not make Cursor reject a top-level interception configuration
    # that does not apply to this prompt-only call.
    if hasattr(config, "inputs"):
        cfg.inputs = copy.copy(config.inputs)
        cfg.inputs.tools = []
    cfg.permissions = (permissions if permissions is not None
                       else {"allow": ["Read", "Grep", "Glob"]})

    try:
        _stage_prompt_files(workspace_path, staged_files)
        runner = RUNNERS[cfg.runner.type].from_config(
            cfg,
            log_prefix=None,
            permissions=cfg.permissions,
            effort=cfg.runner.effort,
        )
        result = runner.execute(
            target=None,
            args=prompt,
            workspace=workspace_path,
            model=model,
            system_prompt=system_prompt,
            max_budget_usd=max_budget_usd,
            timeout_s=timeout_s,
        )
        return result, extract_runner_text(result), workspace_path
    except Exception:
        if created_workspace:
            shutil.rmtree(workspace_path, ignore_errors=True)
        raise


def _stage_prompt_files(workspace: Path, staged_files: Optional[dict[str, bytes]]) -> None:
    """Write caller-provided prompt evidence under the isolated workspace."""
    if not staged_files:
        return
    root = workspace.resolve()
    for relative, content in staged_files.items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Prompt evidence path must be relative: {relative!r}")
        destination = (workspace / path).resolve()
        if destination != root and root not in destination.parents:
            raise ValueError(f"Prompt evidence path escapes workspace: {relative!r}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            destination.write_bytes(content)
        else:
            destination.write_text(str(content))
