"""Convert OTel spans to canonical event format for judges.

Each agent runtime emits different OTel span schemas. SpanMapper
implementations translate runtime-specific spans into the flat event
dicts that parse_stream_events() produces, so judges never change.
"""

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _nano_to_iso(nano) -> str:
    """Convert nanosecond Unix timestamp to ISO 8601."""
    try:
        ts = int(nano) / 1e9
        return (datetime.fromtimestamp(ts, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
    except (TypeError, ValueError, OSError):
        return ""


def _get_attr(attributes: list, key: str, default=None):
    """Extract a value from an OTLP attributes list by key."""
    for attr in (attributes or []):
        if attr.get("key") == key:
            val = attr.get("value", {})
            for vtype in ("stringValue", "intValue", "doubleValue", "boolValue"):
                if vtype in val:
                    return val[vtype]
            return default
    return default


def _flatten_spans(resource_spans: list[dict]) -> list[dict]:
    """Extract all spans from an OTLP ResourceSpans list, sorted by start time."""
    spans = []
    for rs in resource_spans:
        resource_attrs = (rs.get("resource") or {}).get("attributes", [])
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                span["_resource_attrs"] = resource_attrs
                spans.append(span)
    spans.sort(key=lambda s: int(s.get("startTimeUnixNano", 0)))
    return spans


class SpanMapper(ABC):
    """Convert OTLP JSON spans to canonical event dicts."""

    @abstractmethod
    def map_spans(self, resource_spans: list[dict],
                  api_bodies_dir: Optional[Path] = None) -> list[dict]:
        """OTLP JSON ResourceSpans list -> canonical event dicts.

        Output format matches events.py::parse_stream_events():
        - {"type": "system", "subtype": "init", "model": ..., "timestamp": ...}
        - {"type": "assistant", "text": ..., "tools": [...], "timestamp": ...}
        - {"type": "tool_result", "tool_use_id": ..., "tool_name": ..., ...}
        - {"type": "result", "cost_usd": ..., "num_turns": ..., "timestamp": ...}
        """

    @abstractmethod
    def extract_usage(self, resource_spans: list[dict]) -> dict:
        """Extract execution metrics from spans.

        Returns dict with keys:
        - token_usage: {"input": N, "output": N, "cache_read": N, "cache_create": N}
        - cost_usd: float
        - num_turns: int
        - per_model_usage: {model: {input, output, ...}}
        - per_model_turns: {model: int}
        - models_used: [str]
        - resolved_model: str (first model seen)
        """


class ClaudeCodeSpanMapper(SpanMapper):
    """Maps claude_code.* OTel spans to canonical event dicts.

    Claude Code span hierarchy::

        claude_code.interaction (root)
          claude_code.llm_request (API call: tokens, cost, model)
          claude_code.tool (tool invocation: name, input, output)
          claude_code.hook (hook execution)
    """

    def map_spans(self, resource_spans, api_bodies_dir=None):
        spans = _flatten_spans(resource_spans)
        if not spans:
            return []

        api_bodies = {}
        if api_bodies_dir and api_bodies_dir.is_dir():
            api_bodies = self._load_api_bodies(api_bodies_dir)

        events = []
        span_by_id = {s.get("spanId"): s for s in spans}
        tool_names = {}  # tool_use_id -> tool_name

        # Emit system/init from first interaction span
        for span in spans:
            if span.get("name") == "claude_code.interaction":
                first_model = None
                for s in spans:
                    if s.get("name") == "claude_code.llm_request":
                        first_model = _get_attr(s.get("attributes", []), "model")
                        if first_model:
                            break
                events.append({
                    "type": "system",
                    "subtype": "init",
                    "model": first_model or "",
                    "timestamp": _nano_to_iso(span.get("startTimeUnixNano")),
                })
                break

        for span in spans:
            name = span.get("name", "")
            attrs = span.get("attributes", [])
            ts = _nano_to_iso(span.get("startTimeUnixNano"))
            parent_id = span.get("parentSpanId")
            agent_id = _get_attr(attrs, "agent_id")
            parent_agent_id = _get_attr(attrs, "parent_agent_id")

            if name == "claude_code.llm_request":
                # Extract assistant text from API body logs if available
                request_id = _get_attr(attrs, "request_id") or ""
                text = api_bodies.get(request_id, "")
                has_tool_call = _get_attr(attrs, "response.has_tool_call")

                if text or not has_tool_call:
                    event = {
                        "type": "assistant",
                        "text": text,
                        "tools": [],
                        "timestamp": ts,
                        "_msg_id": _get_attr(attrs, "gen_ai.response.id") or span.get("spanId"),
                    }
                    if agent_id and parent_agent_id:
                        event["agent_id"] = agent_id
                        parent_span = span_by_id.get(parent_id)
                        if parent_span:
                            event["parent_tool_use_id"] = parent_span.get("spanId")
                    events.append(event)

            elif name == "claude_code.tool":
                tool_name = _get_attr(attrs, "tool_name") or ""
                span_id = span.get("spanId", "")
                tool_names[span_id] = tool_name

                tool_input = self._extract_tool_content(span, "input")
                tool_output = self._extract_tool_content(span, "output")

                # Assistant event with tool_use
                tool_event = {
                    "type": "assistant",
                    "text": "",
                    "tools": [{
                        "name": tool_name,
                        "id": span_id,
                        "input": tool_input if isinstance(tool_input, dict) else {"content": tool_input},
                    }],
                    "timestamp": ts,
                    "_msg_id": f"tool-{span_id}",
                }
                if agent_id and parent_agent_id:
                    tool_event["agent_id"] = agent_id
                    parent_span = span_by_id.get(parent_id)
                    if parent_span:
                        tool_event["parent_tool_use_id"] = parent_span.get("spanId")
                events.append(tool_event)

                # Tool result event
                exec_span = self._find_child_span(spans, span_id, "claude_code.tool.execution")
                is_error = False
                if exec_span:
                    success = _get_attr(exec_span.get("attributes", []), "success")
                    is_error = success is False or str(success).lower() == "false"

                result_content = tool_output if isinstance(tool_output, str) else json.dumps(tool_output) if tool_output else ""
                result_ts = _nano_to_iso(span.get("endTimeUnixNano"))

                result_event = {
                    "type": "tool_result",
                    "tool_use_id": span_id,
                    "tool_name": tool_name,
                    "content": result_content,
                    "is_error": is_error,
                    "timestamp": result_ts,
                }
                if agent_id and parent_agent_id:
                    result_event["agent_id"] = agent_id
                    result_event["parent_tool_use_id"] = tool_event.get("parent_tool_use_id")
                events.append(result_event)

        # Synthesize result event from aggregated usage
        usage = self.extract_usage(resource_spans)
        events.append({
            "type": "result",
            "cost_usd": usage.get("cost_usd"),
            "num_turns": usage.get("num_turns"),
            "timestamp": None,
        })

        return events

    def extract_usage(self, resource_spans):
        spans = _flatten_spans(resource_spans)
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_create = 0
        per_model = {}
        turn_ids = set()
        models_seen = set()
        first_model = None

        for span in spans:
            if span.get("name") != "claude_code.llm_request":
                continue
            attrs = span.get("attributes", [])
            model = _get_attr(attrs, "model") or ""
            if model:
                models_seen.add(model)
                if not first_model:
                    first_model = model

            inp = int(_get_attr(attrs, "input_tokens") or 0)
            out = int(_get_attr(attrs, "output_tokens") or 0)
            cache_r = int(_get_attr(attrs, "cache_read_tokens") or 0)
            cache_c = int(_get_attr(attrs, "cache_creation_tokens") or 0)

            total_input += inp
            total_output += out
            total_cache_read += cache_r
            total_cache_create += cache_c

            if model:
                if model not in per_model:
                    per_model[model] = {
                        "input": 0, "output": 0,
                        "cache_read": 0, "cache_create": 0,
                        "turns": set(),
                    }
                per_model[model]["input"] += inp
                per_model[model]["output"] += out
                per_model[model]["cache_read"] += cache_r
                per_model[model]["cache_create"] += cache_c

            msg_id = _get_attr(attrs, "gen_ai.response.id") or span.get("spanId")
            # Only count as turn if this is a response with output
            if out > 0 and msg_id:
                turn_ids.add(msg_id)
                if model and model in per_model:
                    per_model[model]["turns"].add(msg_id)

        per_model_usage = {}
        per_model_turns = {}
        for m, data in per_model.items():
            per_model_usage[m] = {
                "input": data["input"],
                "output": data["output"],
                "cache_read": data["cache_read"],
                "cache_create": data["cache_create"],
            }
            per_model_turns[m] = len(data["turns"])

        return {
            "token_usage": {
                "input": total_input,
                "output": total_output,
                "cache_read": total_cache_read,
                "cache_create": total_cache_create,
            },
            "cost_usd": None,  # OTel spans don't carry cost directly
            "num_turns": len(turn_ids),
            "per_model_usage": per_model_usage or None,
            "per_model_turns": per_model_turns or None,
            "models_used": sorted(models_seen) if models_seen else None,
            "resolved_model": first_model,
        }

    @staticmethod
    def _extract_tool_content(span, kind):
        """Extract tool input or output from span events.

        With OTEL_LOG_TOOL_CONTENT=1, Claude Code emits a 'tool.output'
        span event with attributes containing tool I/O.
        """
        for event in span.get("events", []):
            if event.get("name") == "tool.output":
                event_attrs = event.get("attributes", [])
                content = _get_attr(event_attrs, f"tool.{kind}")
                if content:
                    try:
                        return json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        return content
        # Fallback: check span attributes for OTEL_LOG_TOOL_DETAILS
        attrs = span.get("attributes", [])
        if kind == "input":
            for key in ("full_command", "file_path", "skill_name"):
                val = _get_attr(attrs, key)
                if val:
                    return val
        return None

    @staticmethod
    def _find_child_span(spans, parent_id, name):
        """Find a child span by name under the given parent."""
        for s in spans:
            if s.get("parentSpanId") == parent_id and s.get("name") == name:
                return s
        return None

    @staticmethod
    def _load_api_bodies(bodies_dir: Path) -> dict:
        """Load API response bodies from OTEL_LOG_RAW_API_BODIES=file:<dir>.

        Returns {request_id: assistant_text} for responses that contain text.
        """
        bodies = {}
        for f in sorted(bodies_dir.glob("*response*.json")):
            try:
                data = json.loads(f.read_text())
                req_id = data.get("id", "")
                content = data.get("content", [])
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                if text_parts:
                    bodies[req_id] = "\n".join(text_parts)
            except (json.JSONDecodeError, OSError):
                continue
        return bodies


class OpenCodeSpanMapper(SpanMapper):
    """Maps OpenCode (anomalyco/opencode) AI SDK OTel spans to canonical events.

    OpenCode uses two OTel layers:
    - Infrastructure: opencode.run.* spans (session, turn, lifecycle)
    - AI SDK: ai.streamText, ai.generateText, ai.toolCall spans
    """

    def map_spans(self, resource_spans, api_bodies_dir=None):
        spans = _flatten_spans(resource_spans)
        if not spans:
            return []

        events = []

        # Find first model from AI SDK spans
        first_model = None
        for span in spans:
            if span.get("name", "").startswith("ai."):
                first_model = _get_attr(span.get("attributes", []), "gen_ai.request.model")
                if first_model:
                    break

        if first_model:
            events.append({
                "type": "system",
                "subtype": "init",
                "model": first_model,
                "timestamp": _nano_to_iso(spans[0].get("startTimeUnixNano")),
            })

        for span in spans:
            name = span.get("name", "")
            attrs = span.get("attributes", [])
            ts = _nano_to_iso(span.get("startTimeUnixNano"))

            if name in ("ai.streamText", "ai.generateText", "ai.generateObject"):
                text = _get_attr(attrs, "gen_ai.completion") or ""
                event = {
                    "type": "assistant",
                    "text": text,
                    "tools": [],
                    "timestamp": ts,
                    "_msg_id": span.get("spanId"),
                }
                events.append(event)

            elif name == "ai.toolCall":
                tool_name = _get_attr(attrs, "ai.toolCall.name") or _get_attr(attrs, "gen_ai.tool.name") or ""
                tool_id = _get_attr(attrs, "ai.toolCall.id") or span.get("spanId")
                tool_input_raw = _get_attr(attrs, "ai.toolCall.args") or _get_attr(attrs, "gen_ai.tool.args") or "{}"

                try:
                    tool_input = json.loads(tool_input_raw)
                except (json.JSONDecodeError, TypeError):
                    tool_input = {"content": tool_input_raw}

                events.append({
                    "type": "assistant",
                    "text": "",
                    "tools": [{"name": tool_name, "id": tool_id, "input": tool_input}],
                    "timestamp": ts,
                    "_msg_id": f"tool-{tool_id}",
                })

                tool_result = _get_attr(attrs, "ai.toolCall.result") or _get_attr(attrs, "gen_ai.tool.result") or ""
                status = span.get("status", {})
                is_error = status.get("code") == 2  # STATUS_CODE_ERROR

                events.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "tool_name": tool_name,
                    "content": tool_result if isinstance(tool_result, str) else json.dumps(tool_result),
                    "is_error": is_error,
                    "timestamp": _nano_to_iso(span.get("endTimeUnixNano")),
                })

        usage = self.extract_usage(resource_spans)
        events.append({
            "type": "result",
            "cost_usd": usage.get("cost_usd"),
            "num_turns": usage.get("num_turns"),
            "timestamp": None,
        })

        return events

    def extract_usage(self, resource_spans):
        spans = _flatten_spans(resource_spans)
        total_input = 0
        total_output = 0
        per_model = {}
        turn_ids = set()
        models_seen = set()
        first_model = None

        for span in spans:
            name = span.get("name", "")
            if not name.startswith("ai."):
                continue
            attrs = span.get("attributes", [])

            model = _get_attr(attrs, "gen_ai.request.model") or ""
            if model:
                models_seen.add(model)
                if not first_model:
                    first_model = model

            inp = int(_get_attr(attrs, "gen_ai.usage.input_tokens") or
                      _get_attr(attrs, "gen_ai.usage.prompt_tokens") or 0)
            out = int(_get_attr(attrs, "gen_ai.usage.output_tokens") or
                      _get_attr(attrs, "gen_ai.usage.completion_tokens") or 0)

            total_input += inp
            total_output += out

            if model:
                if model not in per_model:
                    per_model[model] = {"input": 0, "output": 0, "turns": set()}
                per_model[model]["input"] += inp
                per_model[model]["output"] += out

            if out > 0 and name in ("ai.streamText", "ai.generateText", "ai.generateObject"):
                turn_ids.add(span.get("spanId"))
                if model and model in per_model:
                    per_model[model]["turns"].add(span.get("spanId"))

        per_model_usage = {}
        per_model_turns = {}
        for m, data in per_model.items():
            per_model_usage[m] = {"input": data["input"], "output": data["output"]}
            per_model_turns[m] = len(data["turns"])

        return {
            "token_usage": {"input": total_input, "output": total_output},
            "cost_usd": None,
            "num_turns": len(turn_ids),
            "per_model_usage": per_model_usage or None,
            "per_model_turns": per_model_turns or None,
            "models_used": sorted(models_seen) if models_seen else None,
            "resolved_model": first_model,
        }


def parse_opencode_events(stdout_text: str) -> list[dict]:
    """Parse OpenCode JSON stdout into canonical event dicts.

    Fallback when OTel spans aren't available (OpenCode has an upstream bug
    where process.exit() kills spans before the BatchSpanProcessor flushes).

    OpenCode --format json emits: step_start, text, tool_call, tool_result,
    step_finish events.
    """
    events = []
    for line in stdout_text.splitlines():
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        etype = obj.get("type", "")
        part = obj.get("part", {})
        ts_ms = obj.get("timestamp")
        ts = ""
        if ts_ms:
            try:
                from datetime import datetime, timezone
                ts = (datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
            except (TypeError, ValueError, OSError):
                pass

        if etype == "text":
            events.append({
                "type": "assistant",
                "text": part.get("text", ""),
                "tools": [],
                "timestamp": ts,
                "_msg_id": part.get("id", ""),
            })

        elif etype == "tool_call":
            tool_name = part.get("tool", "") if isinstance(part, dict) else ""
            tool_id = part.get("id", "")
            tool_input = part.get("input", {})
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except (json.JSONDecodeError, TypeError):
                    tool_input = {"content": tool_input}
            events.append({
                "type": "assistant",
                "text": "",
                "tools": [{"name": tool_name, "id": tool_id, "input": tool_input}],
                "timestamp": ts,
                "_msg_id": f"tool-{tool_id}",
            })

        elif etype == "tool_result":
            events.append({
                "type": "tool_result",
                "tool_use_id": part.get("toolCallID", part.get("id", "")),
                "tool_name": part.get("tool", ""),
                "content": part.get("output", part.get("text", "")),
                "is_error": part.get("state") == "error",
                "timestamp": ts,
            })

        elif etype == "step_finish":
            cost = part.get("cost", 0)
            tokens = part.get("tokens", {})
            events.append({
                "type": "result",
                "cost_usd": cost or None,
                "num_turns": 1,
                "timestamp": ts,
            })

    return events


SPAN_MAPPERS = {
    "claude-code": ClaudeCodeSpanMapper,
    "opencode": OpenCodeSpanMapper,
}


def get_span_mapper(runner_type: str) -> SpanMapper:
    """Return the SpanMapper for a given runner type."""
    cls = SPAN_MAPPERS.get(runner_type, ClaudeCodeSpanMapper)
    return cls()
