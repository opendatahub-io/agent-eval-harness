"""Structured event parser for Claude Code, Codex, and Cursor JSONL output.

Parses JSONL stdout into a flat list of typed event dicts suitable for
judge consumption via ``outputs["events"]``.
"""

import json
import re
import shlex
from pathlib import Path

DEFAULT_RESULT_CAP = 50000


def extract_read_calls(events, include_subagents=True, include_grep=True):
    """Extract file access tool calls from parsed events for documentation tracking.

    Tracks Read tool calls and optionally Grep tool calls (which also read
    file contents). Generic Bash commands are not parsed here because their
    file targets are ambiguous. Codex's JSONL translator does annotate
    conservative, explicit file-reader commands with ``read_paths``; those
    structured paths count.

    Args:
        events: List of event dicts from parse_stream_events().
        include_subagents: If True (default), include reads from subagent events.
            Set to False to only return top-level reads.
        include_grep: If True (default), also count Grep tool calls as file
            reads. Grep searches file contents, so the agent has effectively
            consulted those files.

    Returns:
        List of dicts with {file_path, timestamp, ...} for each file access.
    """
    if not events:
        return []

    read_calls = []

    for event in events:
        if event.get("type") != "assistant":
            continue

        if not include_subagents and event.get("parent_tool_use_id"):
            continue

        timestamp = event.get("timestamp")

        for tool in event.get("tools", []):
            if not isinstance(tool, dict):
                continue
            name = tool.get("name", "")
            tool_input = tool.get("input", {})
            if not isinstance(tool_input, dict):
                continue

            if name == "Read":
                # Claude Code uses file_path; Cursor readToolCall uses path.
                file_path = tool_input.get("file_path") or tool_input.get("path") or ""
                if not file_path:
                    continue
                read_calls.append({
                    "file_path": file_path,
                    "timestamp": timestamp,
                    "offset": tool_input.get("offset"),
                    "limit": tool_input.get("limit"),
                    "pages": tool_input.get("pages"),
                })

            elif name == "Grep" and include_grep:
                path = tool_input.get("path", "")
                if path and path != ".":
                    read_calls.append({
                        "file_path": path,
                        "timestamp": timestamp,
                    })

            # Codex annotates explicit file-reader Bash. Cursor Grep results
            # may also stash matched files here; Glob only lists names and does
            # not prove that their contents were read.
            if name == "Bash" or (include_grep and name == "Grep"):
                extra_paths = tool_input.get("read_paths")
                if isinstance(extra_paths, list):
                    for path in extra_paths:
                        if isinstance(path, str) and path:
                            read_calls.append({
                                "file_path": path,
                                "timestamp": timestamp,
                            })

    return read_calls


def parse_stream_events(stdout_text, result_cap=DEFAULT_RESULT_CAP):
    """Parse JSONL text into structured event dicts.

    Understands Claude Code stream-json (``assistant``/``user``/
    ``result``/``system``), Codex ``exec --json`` (``item.completed``/
    ``turn.completed``), and Cursor Agent ``tool_call`` lines; all are
    translated into the same flat schema.

    Args:
        stdout_text: Raw JSONL text from the agent CLI's stdout.
        result_cap: Max characters per tool result/input string value.

    Returns:
        List of event dicts ordered chronologically.
    """
    if not stdout_text:
        return []

    # Cursor and Claude both include ``session_id`` on assistant events.  Do
    # not use that field alone to select Cursor's delta/result handling: doing
    # so moves ordinary Claude turns to the end of the conversation and can
    # replace the conversation with a terminal result.  Cursor's stream has
    # either a tool_call event or the lean assistant/result shapes below.
    objects = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            objects.append(obj)

    cursor_stream = any(_looks_like_cursor_object(obj) for obj in objects)
    events = []
    tool_id_to_name = {}
    codex_turns = 0
    codex_turn_timestamp = None

    for obj in objects:
        event_type = obj.get("type")
        if event_type == "assistant":
            event = _parse_assistant_event(obj, result_cap)
            if event:
                if cursor_stream and obj.get("session_id") is not None:
                    event["_cursor_delta"] = True
                for tool in event.get("tools", []):
                    tool_id_to_name[tool["id"]] = tool["name"]
                events.append(event)

        elif event_type == "user":
            tool_results = _parse_user_tool_results(
                obj, tool_id_to_name, result_cap)
            events.extend(tool_results)

        elif event_type == "result":
            event = _parse_result_event(obj, cursor_stream=cursor_stream)
            if event:
                events.append(event)

        elif event_type == "system":
            event = _parse_system_event(obj)
            if event:
                events.append(event)

        elif event_type == "tool_call":
            cursor_events = _parse_cursor_tool_call(obj, result_cap)
            for event in cursor_events:
                if event.get("type") == "assistant":
                    for tool in event.get("tools", []):
                        tool_id_to_name[tool["id"]] = tool["name"]
            events.extend(cursor_events)

        elif event_type == "item.completed":
            events.extend(_parse_codex_event(obj, result_cap))

        elif event_type in {"turn.completed", "turn_completed"}:
            # Codex emits one of these per turn. Fold them into a single
            # trailing result event so every transcript keeps the
            # one-result-per-run shape consumers expect from Claude streams.
            codex_turns += 1
            codex_turn_timestamp = obj.get("timestamp")

    if codex_turns:
        events.append({
            "type": "result",
            "cost_usd": None,
            "num_turns": codex_turns,
            "timestamp": codex_turn_timestamp,
        })
    return events


def _looks_like_cursor_object(obj):
    """Return whether a raw stream object has Cursor-specific structure."""
    event_type = obj.get("type")
    if event_type == "tool_call":
        return True
    if obj.get("session_id") is None:
        return False
    if event_type == "assistant":
        message = obj.get("message")
        # Claude assistant messages carry message id/model/usage fields;
        # Cursor's documented stream assistant message is intentionally lean.
        return isinstance(message, dict) and not any(
            key in message for key in ("id", "model", "usage"))
    if event_type == "result":
        # Cursor result events have duration/request metadata rather than the
        # Claude usage/cost fields.  request_id is optional, duration_ms is not
        # on the documented successful result shape.
        return (isinstance(obj.get("result"), str)
                and ("request_id" in obj or "duration_ms" in obj)
                and "total_cost_usd" not in obj)
    return False


_CURSOR_TOOL_NAMES = {
    "readToolCall": "Read",
    "grepToolCall": "Grep",
    "globToolCall": "Glob",
    "writeToolCall": "Write",
    "editToolCall": "Edit",
    "bashToolCall": "Bash",
    "shellToolCall": "Shell",
    "taskToolCall": "Task",
    "createPlanToolCall": "CreatePlan",
    "askQuestionToolCall": "AskQuestion",
    "unknownToolCall": "Unknown",
}

_CURSOR_FUNCTION_TOOL_KINDS = {
    "read": "readToolCall",
    "readtoolcall": "readToolCall",
    "grep": "grepToolCall",
    "greptoolcall": "grepToolCall",
    "glob": "globToolCall",
    "globtoolcall": "globToolCall",
    "write": "writeToolCall",
    "writetoolcall": "writeToolCall",
    "edit": "editToolCall",
    "edittoolcall": "editToolCall",
    "bash": "bashToolCall",
    "bashtoolcall": "bashToolCall",
    "shell": "shellToolCall",
    "shelltoolcall": "shellToolCall",
    "task": "taskToolCall",
    "tasktoolcall": "taskToolCall",
}


def _event_timestamp(obj):
    """Prefer ISO ``timestamp``; Cursor uses numeric ``timestamp_ms``."""
    ts = obj.get("timestamp")
    if ts is not None:
        return ts
    return obj.get("timestamp_ms")


def _normalize_cursor_path(path):
    if not isinstance(path, str):
        return ""
    path = path.strip()
    if path.startswith("./"):
        path = path[2:]
    return path


def _cursor_tool_body(tool_call):
    """Return ``(kind, body)`` for a Cursor tool payload.

    Cursor has emitted both native ``readToolCall`` payloads and a generic
    ``function`` payload in different CLI versions. Keep the latter visible
    instead of silently losing the completed call.
    """
    if not isinstance(tool_call, dict):
        return None, None
    for key, body in tool_call.items():
        if isinstance(key, str) and key.endswith("ToolCall") and isinstance(body, dict):
            return key, body
    function = tool_call.get("function")
    if isinstance(function, dict):
        function_name = function.get("name")
        if not isinstance(function_name, str) or not function_name.strip():
            kind = "unknownToolCall"
        else:
            normalized_name = function_name.strip()
            kind = _CURSOR_FUNCTION_TOOL_KINDS.get(
                normalized_name.lower(),
                (normalized_name if normalized_name.endswith("ToolCall")
                 else f"{normalized_name}ToolCall"),
            )
        raw_args = function.get("arguments", function.get("args"))
        if isinstance(raw_args, dict):
            args = raw_args
        elif isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            args = parsed if isinstance(parsed, dict) else {"arguments": raw_args}
        elif raw_args is None:
            args = {}
        else:
            args = {"arguments": raw_args}
        return kind, {
            "args": args,
            "result": function.get("result", tool_call.get("result")),
        }
    return None, None


def _cursor_grep_hit_paths(result):
    """Collect matched file paths from a Cursor grepToolCall result."""
    paths = []
    if not isinstance(result, dict):
        return paths
    success = result.get("success")
    if not isinstance(success, dict):
        success = result

    workspace_results = success.get("workspaceResults")
    if isinstance(workspace_results, dict):
        for ws_data in workspace_results.values():
            if not isinstance(ws_data, dict):
                continue
            files = ws_data.get("files")
            file_list = []
            if isinstance(files, dict):
                file_list = files.get("files") or []
            elif isinstance(files, list):
                file_list = files
            for item in file_list:
                if isinstance(item, str):
                    normalized = _normalize_cursor_path(item)
                    if normalized:
                        paths.append(normalized)
            content = ws_data.get("content")
            if isinstance(content, dict):
                for match in content.get("matches") or []:
                    if not isinstance(match, dict):
                        continue
                    normalized = _normalize_cursor_path(match.get("file"))
                    if normalized:
                        paths.append(normalized)

    for item in success.get("files") or []:
        if isinstance(item, str):
            normalized = _normalize_cursor_path(item)
            if normalized:
                paths.append(normalized)

    seen = set()
    unique = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _parse_cursor_tool_call(obj, result_cap):
    """Translate one Cursor ``tool_call`` line into the flat event schema.

    Cursor emits ``type: tool_call`` with ``readToolCall`` / ``grepToolCall`` /
    etc. payloads, separate from ``assistant`` text turns. Only ``completed``
    events are kept so ``started`` + ``completed`` pairs are not double-counted.
    """
    if obj.get("subtype") != "completed":
        return []

    tool_call = obj.get("tool_call")
    kind, body = _cursor_tool_body(tool_call)
    if not kind:
        return []

    tool_name = _CURSOR_TOOL_NAMES.get(kind)
    if not tool_name:
        stem = kind[:-8] if kind.endswith("ToolCall") else kind
        tool_name = stem[:1].upper() + stem[1:] if stem else kind

    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    result = body.get("result")
    function = tool_call.get("function") if isinstance(tool_call, dict) else {}
    tool_id = str(
        obj.get("call_id")
        or (tool_call.get("toolCallId") if isinstance(tool_call, dict) else None)
        or (function.get("call_id") if isinstance(function, dict) else None)
        or ""
    )
    timestamp = _event_timestamp(obj)

    tool_input = dict(args)
    if tool_name in {"Read", "Write", "Edit"}:
        path = (args.get("path") or args.get("file_path")
                or args.get("filePath"))
        if not path and isinstance(result, dict):
            success = result.get("success")
            if isinstance(success, dict):
                path = success.get("path") or success.get("file_path")
        if path:
            tool_input.setdefault("file_path", path)
            if tool_name == "Read":
                tool_input.setdefault("path", path)
        if tool_name == "Read" and isinstance(result, dict):
            success = result.get("success")
            if isinstance(success, dict):
                read_range = success.get("readRange") or {}
                if isinstance(read_range, dict) and "startLine" in read_range:
                    tool_input.setdefault("offset", read_range.get("startLine"))
    elif tool_name == "Grep":
        hits = _cursor_grep_hit_paths(result)
        if hits:
            tool_input["read_paths"] = hits
    elif tool_name == "Glob":
        files = []
        if isinstance(result, dict):
            success = result.get("success")
            if isinstance(success, dict):
                files = [
                    _normalize_cursor_path(item)
                    for item in (success.get("files") or [])
                    if isinstance(item, str)
                ]
                files = [item for item in files if item]
        if files:
            tool_input["files"] = files

    tool_input = _cap_values(tool_input, result_cap)
    if isinstance(result, dict):
        tool_output = json.dumps(result, ensure_ascii=False, default=str)
        is_error = bool(result.get("error") or result.get("failure"))
    else:
        tool_output = "" if result is None else str(result)
        is_error = False
    truncated = _truncate_string(_sanitize_text(tool_output), result_cap)
    assistant = {
        "type": "assistant", "text": "", "timestamp": timestamp,
        "tools": [{"name": tool_name, "id": tool_id, "input": tool_input}],
    }
    result_event = {
        "type": "tool_result", "tool_use_id": tool_id,
        "tool_name": tool_name, "content": truncated["value"],
        "is_error": is_error, "timestamp": timestamp,
    }
    if truncated.get("truncated"):
        result_event["truncated"] = True
        result_event["original_length"] = truncated["original_length"]
    return [assistant, result_event]


def _parse_codex_event(obj, result_cap):
    """Translate one Codex ``item.completed`` event into the flat schema."""
    item = obj.get("item")
    if not isinstance(item, dict):
        return []
    item_type = item.get("type")
    item_id = str(item.get("id") or "")
    timestamp = obj.get("timestamp")

    if item_type == "agent_message":
        text = item.get("text")
        return [{
            "type": "assistant",
            "text": text if isinstance(text, str) else "",
            "tools": [],
            "timestamp": timestamp,
            **({"_msg_id": item_id} if item_id else {}),
        }]

    if item_type == "reasoning":
        text = item.get("text")
        if not isinstance(text, str):
            text = item.get("summary")
        return [{
            "type": "assistant", "text": "", "tools": [],
            "thinking": text if isinstance(text, str) else "",
            "timestamp": timestamp,
        }]

    tool_name = ""
    tool_input = {}
    tool_output = ""
    is_error = False
    if item_type == "command_execution":
        tool_name = "Bash"
        command = item.get("command", "")
        tool_input = {"command": command}
        read_paths = _codex_command_read_paths(command)
        if read_paths:
            tool_input["read_paths"] = read_paths
        tool_output = item.get("aggregated_output", "")
        exit_code = item.get("exit_code")
        is_error = (isinstance(exit_code, int) and not isinstance(exit_code, bool)
                    and exit_code != 0)
    elif item_type == "mcp_tool_call":
        server = item.get("server") or item.get("server_name") or "mcp"
        name = item.get("tool") or item.get("name") or "tool"
        tool_name = f"mcp__{server}__{name}"
        arguments = item.get("arguments", {})
        tool_input = arguments if isinstance(arguments, dict) else {
            "arguments": arguments}
        tool_output = item.get("result") or item.get("error") or ""
        is_error = bool(item.get("error"))
    elif item_type == "collab_tool_call":
        tool_name = str(item.get("tool") or "collaboration")
        tool_input = {
            key: item[key] for key in ("prompt", "receiver_thread_ids")
            if key in item
        }
        tool_output = item.get("message") or item.get("status") or ""
        is_error = item.get("status") == "failed"
    elif item_type == "web_search":
        tool_name = "WebSearch"
        tool_input = {"query": item.get("query", "")}
        tool_output = item.get("result") or ""
    elif item_type == "file_change":
        tool_name = "Edit"
        changes = item.get("changes", [])
        tool_input = {"changes": changes}
        # Surface the first changed path as file_path so the shared tool
        # trace / files-written extraction render a path instead of "?".
        if (isinstance(changes, list) and changes
                and isinstance(changes[0], dict)
                and isinstance(changes[0].get("path"), str)):
            tool_input["file_path"] = changes[0]["path"]
        tool_output = item.get("status") or ""
        is_error = item.get("status") == "failed"
    else:
        return []

    tool_input = _cap_values(tool_input, result_cap)
    if not isinstance(tool_output, str):
        tool_output = json.dumps(tool_output, ensure_ascii=False, default=str)
    truncated = _truncate_string(_sanitize_text(tool_output), result_cap)
    assistant = {
        "type": "assistant", "text": "", "timestamp": timestamp,
        "tools": [{"name": tool_name, "id": item_id, "input": tool_input}],
    }
    result = {
        "type": "tool_result", "tool_use_id": item_id,
        "tool_name": tool_name, "content": truncated["value"],
        "is_error": is_error, "timestamp": timestamp,
    }
    if truncated.get("truncated"):
        result["truncated"] = True
        result["original_length"] = truncated["original_length"]
    return [assistant, result]


def _codex_command_read_paths(command) -> list[str]:
    """Extract paths from simple, explicit file-reader shell commands.

    This is deliberately narrow: it recognizes the command shapes Codex emits
    for direct ``sed``/``cat``/``head``/``tail`` reads, but does not guess about
    pipelines, substitutions, scripts, or arbitrary commands.
    """
    if not isinstance(command, str) or not command:
        return []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    if not tokens:
        return []

    shell = Path(tokens[0]).name
    if shell in {"bash", "sh", "zsh", "dash"}:
        if not any(flag.startswith("-") and "c" in flag
                   for flag in tokens[1:-1]):
            return []
        try:
            tokens = shlex.split(tokens[-1])
        except ValueError:
            return []
        if not tokens:
            return []

    for token in tokens:
        if "$(" in token or "`" in token or token.startswith(("<(", ">(")):
            return []
    tokens = _strip_shell_redirections(tokens)
    # After redirections are gone, any remaining operator character means a
    # pipeline or compound command. Check inside tokens, not just for exact
    # matches: shlex does not split on unquoted operators without whitespace,
    # so ``cat a.md|head`` yields the single token ``a.md|head``.
    if any(ch in token for token in tokens for ch in "|;&"):
        return []
    if not tokens:
        return []
    command_name = Path(tokens[0]).name

    if command_name == "cat":
        return [token for token in tokens[1:]
                if token and not token.startswith("-")]

    if command_name == "sed":
        index = 1
        program_seen = False
        paths = []
        while index < len(tokens):
            token = tokens[index]
            if token in {"-e", "--expression"}:
                program_seen = True
                index += 2
                continue
            if token in {"-f", "--file"}:
                if index + 1 < len(tokens):
                    paths.append(tokens[index + 1])
                program_seen = True
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            if not program_seen:
                program_seen = True
            else:
                paths.append(token)
            index += 1
        return paths

    if command_name in {"head", "tail"}:
        paths = []
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token in {"-n", "--lines", "-c", "--bytes"}:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            paths.append(token)
            index += 1
        return paths

    return []


# Matches redirection operators whether detached (``>``, ``2>``) or fused to
# their target (``>out``, ``2>/dev/null``, ``2>&1``, ``&>log``, ``<in``).
_REDIRECT_OPERATOR = re.compile(r"(\d*|&)(>>?|<)")


def _strip_shell_redirections(tokens):
    """Drop redirections so ``cat notes.md 2>/dev/null`` still counts notes.md.

    A detached operator consumes its following target token; a fused form is
    dropped alone. ``<`` sources are dropped too — conservative, per the
    narrow-parse policy above.
    """
    stripped = []
    index = 0
    while index < len(tokens):
        match = _REDIRECT_OPERATOR.match(tokens[index])
        if match:
            index += 2 if match.end() == len(tokens[index]) else 1
            continue
        stripped.append(tokens[index])
        index += 1
    return stripped


def _extract_content_blocks(content_blocks, result_cap):
    """Split an assistant message's content blocks into (text, thinking, tools).

    Only string values are collected, so a null or non-string ``text`` /
    ``thinking`` from a non-Anthropic provider is skipped rather than raising
    and aborting the whole parse. A
    ``redacted_thinking`` block contributes a marker so a fully-redacted turn
    isn't mistaken for an absence of reasoning. Multiple thinking blocks are
    joined with newlines to preserve their boundaries.
    """
    text_parts = []
    thinking_parts = []
    tools = []

    if not isinstance(content_blocks, list):
        return "", "", tools

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            value = block.get("text")
            if isinstance(value, str) and value:
                text_parts.append(value)
        elif block_type == "thinking":
            value = block.get("thinking")
            if isinstance(value, str) and value:
                thinking_parts.append(value)
        elif block_type == "redacted_thinking":
            thinking_parts.append("[redacted thinking]")
        elif block_type == "tool_use":
            tool_input = _cap_values(block.get("input", {}), result_cap)
            tools.append({
                "name": block.get("name", ""),
                "id": block.get("id", ""),
                "input": tool_input,
            })

    return "".join(text_parts), "\n".join(thinking_parts), tools


def _parse_assistant_event(obj, result_cap):
    message = obj.get("message", {})
    if not isinstance(message, dict):
        return None
    content_blocks = message.get("content", [])
    if isinstance(content_blocks, str):
        content_blocks = [{"type": "text", "text": content_blocks}]
    elif not isinstance(content_blocks, list):
        content_blocks = []
    timestamp = _event_timestamp(obj)

    text, thinking, tools = _extract_content_blocks(content_blocks, result_cap)

    event = {
        "type": "assistant",
        "text": text,
        "tools": tools,
        "timestamp": timestamp,
    }
    if thinking:
        event["thinking"] = thinking

    msg_id = message.get("id")
    if msg_id:
        event["_msg_id"] = msg_id

    parent_tool_use_id = obj.get("parent_tool_use_id")
    if parent_tool_use_id:
        event["parent_tool_use_id"] = parent_tool_use_id
        agent_id = obj.get("agent_id")
        if agent_id:
            event["agent_id"] = agent_id

    return event


def _parse_user_tool_results(obj, tool_id_to_name, result_cap):
    """Extract tool_result events from a user message."""
    message = obj.get("message", {})
    if not isinstance(message, dict):
        return []
    content = message.get("content", [])
    timestamp = obj.get("timestamp")
    results = []

    if not isinstance(content, list):
        return results

    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue

        tool_use_id = block.get("tool_use_id", "")
        raw_content = block.get("content", "")

        if isinstance(raw_content, list):
            text_parts = []
            for sub in raw_content:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    text_parts.append(sub.get("text", ""))
                elif isinstance(sub, str):
                    text_parts.append(sub)
            raw_content = "".join(text_parts)
        elif not isinstance(raw_content, str):
            raw_content = str(raw_content)

        raw_content = _sanitize_text(raw_content)
        truncated_meta = _truncate_string(raw_content, result_cap)

        event = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "tool_name": tool_id_to_name.get(tool_use_id, ""),
            "content": truncated_meta["value"],
            "is_error": bool(block.get("is_error", False)),
            "timestamp": timestamp,
        }

        if truncated_meta.get("truncated"):
            event["truncated"] = True
            event["original_length"] = truncated_meta["original_length"]

        parent_tool_use_id = obj.get("parent_tool_use_id")
        if parent_tool_use_id:
            event["parent_tool_use_id"] = parent_tool_use_id
            agent_id = obj.get("agent_id")
            if agent_id:
                event["agent_id"] = agent_id

        results.append(event)

    return results


def _parse_result_event(obj, *, cursor_stream=False):
    event = {
        "type": "result",
        "cost_usd": obj.get("total_cost_usd"),
        "num_turns": obj.get("num_turns"),
        "timestamp": _event_timestamp(obj),
    }
    # Cursor's result event is the authoritative complete assistant response;
    # retaining it avoids reconstructing JSON/text from many deltas.
    if (cursor_stream and obj.get("session_id") is not None
            and isinstance(obj.get("result"), str)):
        event["_cursor_result_text"] = obj["result"]
    return event


def _parse_system_event(obj):
    event = {
        "type": "system",
        "subtype": obj.get("subtype", ""),
        "timestamp": obj.get("timestamp"),
    }
    if obj.get("subtype") == "init":
        event["model"] = obj.get("model", "")
    return event


def _cap_values(input_dict, cap):
    """Cap string values in a tool input dict, adding truncation metadata."""
    if not isinstance(input_dict, dict):
        return input_dict
    result = {}
    for key, value in input_dict.items():
        if isinstance(value, str):
            meta = _truncate_string(value, cap)
            result[key] = meta["value"]
            if meta.get("truncated"):
                result.setdefault("_truncated", {})[key] = {
                    "truncated": True,
                    "original_length": meta["original_length"],
                }
        elif isinstance(value, dict):
            result[key] = _cap_values(value, cap)
        else:
            result[key] = value
    return result


def _truncate_string(value, cap):
    if len(value) <= cap:
        return {"value": value}
    return {
        "value": value[:cap] + "[truncated]",
        "truncated": True,
        "original_length": len(value),
    }


def _sanitize_text(text):
    if isinstance(text, bytes):
        try:
            return text.decode("utf-8")
        except UnicodeDecodeError:
            return f"(binary content, {len(text)} bytes)"
    return text


def merge_subagent_transcripts(events, subagent_dir, result_cap=DEFAULT_RESULT_CAP):
    """Merge subagent transcript events into the main event list.

    Reads ``subagents/*.jsonl`` transcript files, converts them to event
    dicts with ``agent_id`` derived from the transcript filename, deduplicates
    by message ID against events already in the list, and inserts in
    chronological order.

    Args:
        events: Existing event list (modified in place and returned).
        subagent_dir: Path to directory containing subagent JSONL transcripts.
        result_cap: Max characters per tool input string value.

    Returns:
        The merged event list (same reference as input).
    """
    subagent_path = Path(subagent_dir)
    if not subagent_path.is_dir():
        return events

    seen_msg_ids = _collect_message_ids(events)
    # Map for backfilling richer fields onto an already-seen copy: an inline
    # stdout copy of a subagent message carries no thinking block, so when the
    # transcript copy (which does) is deduped by _msg_id we still recover its
    # chain-of-thought instead of dropping it under all-or-nothing dedup.
    events_by_msg_id = {e.get("_msg_id"): e for e in events
                        if e.get("type") == "assistant" and e.get("_msg_id")}
    new_events = []

    for transcript in sorted(subagent_path.iterdir()):
        if not transcript.is_file() or transcript.suffix != ".jsonl":
            continue
        agent_id = transcript.stem

        try:
            text = transcript.read_text()
        except OSError:
            continue

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            msg = obj.get("message", {})
            msg_id = msg.get("id")
            if msg_id and msg_id in seen_msg_ids:
                existing = events_by_msg_id.get(msg_id)
                if (existing is not None and not existing.get("thinking")
                        and msg.get("role") == "assistant"):
                    parsed = _parse_transcript_assistant(
                        obj, agent_id, result_cap)
                    if parsed and parsed.get("thinking"):
                        existing["thinking"] = parsed["thinking"]
                continue
            if msg_id:
                seen_msg_ids.add(msg_id)

            if msg.get("role") == "assistant":
                event = _parse_transcript_assistant(obj, agent_id, result_cap)
                if event:
                    new_events.append(event)
                    if event.get("_msg_id"):
                        events_by_msg_id[event["_msg_id"]] = event

    if new_events:
        events.extend(new_events)
        # str() guards the comparison: transcripts are agent-influenced, and
        # one numeric timestamp among ISO strings must not crash the merge.
        events.sort(key=lambda e: (0, str(e["timestamp"]))
                     if e.get("timestamp") else (1, ""))

    return events


def _collect_message_ids(events):
    """Collect all message IDs from parsed events for deduplication."""
    ids = set()
    for event in events:
        if event["type"] == "assistant":
            msg_id = event.get("_msg_id")
            if msg_id:
                ids.add(msg_id)
    return ids


def _parse_transcript_assistant(obj, agent_id, result_cap=DEFAULT_RESULT_CAP):
    message = obj.get("message", {})
    content_blocks = message.get("content", [])
    timestamp = obj.get("timestamp")

    text, thinking, tools = _extract_content_blocks(content_blocks, result_cap)

    parent_tool_use_id = obj.get("parent_tool_use_id")

    event = {
        "type": "assistant",
        "text": text,
        "tools": tools,
        "timestamp": timestamp,
        "agent_id": agent_id,
    }
    if thinking:
        event["thinking"] = thinking

    msg_id = message.get("id")
    if msg_id:
        event["_msg_id"] = msg_id

    if parent_tool_use_id:
        event["parent_tool_use_id"] = parent_tool_use_id

    return event


def extract_tool_trace(events, include_subagents=True):
    """Render a human-readable chronological trace of tool calls from events.

    Produces a formatted log showing each tool invocation with its key
    inputs, suitable for LLM judges that need to evaluate agent behavior
    (navigation, tool usage patterns) rather than just textual output.

    Args:
        events: List of event dicts from parse_stream_events().
        include_subagents: If True (default), include tool calls from
            subagent events.

    Returns:
        Formatted string with one line per tool call.
    """
    if not events:
        return ""

    lines = []
    step = 0

    for event in events:
        if event.get("type") != "assistant":
            continue
        if not include_subagents and event.get("parent_tool_use_id"):
            continue

        is_subagent = bool(event.get("parent_tool_use_id"))
        prefix = "  [subagent] " if is_subagent else ""

        for tool in event.get("tools", []):
            step += 1
            name = tool.get("name", "unknown")
            tool_input = tool.get("input", {})

            detail = _format_tool_input(name, tool_input)
            lines.append(f"{prefix}{step}. {name}: {detail}")

    return "\n".join(lines)


def _format_tool_input(name, tool_input):
    """Format tool input for human-readable trace output."""
    if name == "Read":
        path = tool_input.get("file_path", "?")
        parts = [path]
        if tool_input.get("offset"):
            parts.append(f"offset={tool_input['offset']}")
        if tool_input.get("limit"):
            parts.append(f"limit={tool_input['limit']}")
        return ", ".join(parts)

    if name == "Bash":
        cmd = tool_input.get("command", "?")
        if len(cmd) > 200:
            cmd = cmd[:200] + "..."
        return cmd

    if name == "Agent":
        desc = tool_input.get("description", "")
        prompt_text = tool_input.get("prompt", "")
        if desc:
            return f'"{desc}"'
        if prompt_text:
            summary = prompt_text[:100] + "..." if len(prompt_text) > 100 else prompt_text
            return summary
        return "(no description)"

    if name in ("Edit", "Write"):
        return tool_input.get("file_path", "?")

    if name in ("Glob", "Grep"):
        return tool_input.get("pattern", tool_input.get("query", "?"))

    if name == "WebFetch":
        return tool_input.get("url", "?")

    if name == "WebSearch":
        return tool_input.get("query", "?")

    if name == "Skill":
        return tool_input.get("skill", "?")

    # Fallback: show first key-value pair
    for k, v in tool_input.items():
        v_str = str(v)
        if len(v_str) > 100:
            v_str = v_str[:100] + "..."
        return f"{k}={v_str}"
    return "(no input)"


# Judge-facing guard: cap the reasoning-inclusive conversation so an unusually
# verbose run can't overflow a judge prompt and silently error the judge call.
# Generous enough (~100K tokens) that normal cases never reach it.
CONVERSATION_THINKING_CAP = 400000


def extract_conversation_text(events, include_thinking=False):
    """Extract root-level assistant conversation from events.

    Filters out subagent events (those with parent_tool_use_id) and, for each
    remaining assistant turn, emits its visible text.

    With ``include_thinking=True`` each turn's extended-thinking
    (chain-of-thought) is emitted, labeled ``[thinking]``, before its visible
    text — this lets a reasoning-quality judge grade the actual thought process
    rather than the terse inter-tool narration. The default is text-only, so the
    plain ``{{ conversation }}`` variable (consumed by other judges, e.g. the
    safety judge that grades visible output) keeps its original semantics. The
    reasoning-inclusive form is capped at ``CONVERSATION_THINKING_CAP`` chars
    with a truncation marker.
    """
    parts = []
    cursor_deltas = []
    cursor_result = None
    saw_cursor = False
    for event in events:
        if event.get("type") == "result" and "_cursor_result_text" in event:
            saw_cursor = True
            cursor_result = event.get("_cursor_result_text")
            continue
        if event.get("type") != "assistant":
            continue
        if event.get("parent_tool_use_id"):
            continue
        if include_thinking:
            thinking = event.get("thinking", "")
            if thinking:
                parts.append(f"[thinking]\n{thinking}")
        text = event.get("text", "")
        if text:
            if event.get("_cursor_delta"):
                saw_cursor = True
                cursor_deltas.append(text)
            else:
                parts.append(text)
    if saw_cursor:
        cursor_text = cursor_result if cursor_result is not None else "".join(cursor_deltas)
        if cursor_text:
            parts.append(cursor_text)
    rendered = "\n\n".join(parts)
    if include_thinking and len(rendered) > CONVERSATION_THINKING_CAP:
        rendered = (rendered[:CONVERSATION_THINKING_CAP]
                    + "\n\n[conversation truncated]")
    return rendered
