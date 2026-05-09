"""Structured event parser for Claude Code stream-json output.

Parses JSONL stdout into a flat list of typed event dicts suitable for
judge consumption via ``outputs["events"]``.
"""

import json
from pathlib import Path

DEFAULT_RESULT_CAP = 50000


def parse_stream_events(stdout_text, result_cap=DEFAULT_RESULT_CAP):
    """Parse JSONL stream-json text into structured event dicts.

    Args:
        stdout_text: Raw JSONL text from Claude Code stdout.
        result_cap: Max characters per tool result/input string value.

    Returns:
        List of event dicts ordered chronologically.
    """
    if not stdout_text:
        return []

    events = []
    tool_id_to_name = {}

    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        event_type = obj.get("type")
        if event_type == "assistant":
            event = _parse_assistant_event(obj, result_cap)
            if event:
                for tool in event.get("tools", []):
                    tool_id_to_name[tool["id"]] = tool["name"]
                events.append(event)

        elif event_type == "user":
            tool_results = _parse_user_tool_results(
                obj, tool_id_to_name, result_cap)
            events.extend(tool_results)

        elif event_type == "result":
            event = _parse_result_event(obj)
            if event:
                events.append(event)

        elif event_type == "system":
            event = _parse_system_event(obj)
            if event:
                events.append(event)

    return events


def _parse_assistant_event(obj, result_cap):
    message = obj.get("message", {})
    content_blocks = message.get("content", [])
    timestamp = obj.get("timestamp")

    text_parts = []
    tools = []

    for block in content_blocks:
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "tool_use":
            tool_input = _cap_values(block.get("input", {}), result_cap)
            tools.append({
                "name": block.get("name", ""),
                "id": block.get("id", ""),
                "input": tool_input,
            })

    event = {
        "type": "assistant",
        "text": "".join(text_parts),
        "tools": tools,
        "timestamp": timestamp,
    }

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


def _parse_result_event(obj):
    return {
        "type": "result",
        "cost_usd": obj.get("total_cost_usd"),
        "num_turns": obj.get("num_turns"),
        "timestamp": None,
    }


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
                continue
            if msg_id:
                seen_msg_ids.add(msg_id)

            if msg.get("role") == "assistant":
                event = _parse_transcript_assistant(obj, agent_id, result_cap)
                if event:
                    new_events.append(event)

    if new_events:
        events.extend(new_events)
        events.sort(key=lambda e: (0, e["timestamp"])
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

    text_parts = []
    tools = []

    for block in content_blocks:
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "tool_use":
            tool_input = _cap_values(block.get("input", {}), result_cap)
            tools.append({
                "name": block.get("name", ""),
                "id": block.get("id", ""),
                "input": tool_input,
            })

    parent_tool_use_id = obj.get("parent_tool_use_id")

    event = {
        "type": "assistant",
        "text": "".join(text_parts),
        "tools": tools,
        "timestamp": timestamp,
        "agent_id": agent_id,
    }

    msg_id = message.get("id")
    if msg_id:
        event["_msg_id"] = msg_id

    if parent_tool_use_id:
        event["parent_tool_use_id"] = parent_tool_use_id

    return event


def extract_conversation_text(events):
    """Extract root-level assistant text from events.

    Filters out subagent events (those with parent_tool_use_id) and
    concatenates assistant text blocks.
    """
    parts = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        if event.get("parent_tool_use_id"):
            continue
        text = event.get("text", "")
        if text:
            parts.append(text)
    return "\n\n".join(parts)
