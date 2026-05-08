# Data Model: Structured Event Stream

## Entities

### Event (stored in `events.json`, loaded into `record["events"]`)

Each event is a dict with a `type` discriminator and type-specific fields:

#### Assistant Event
```json
{
  "type": "assistant",
  "text": "extracted assistant text (concatenated from text blocks)",
  "tools": [
    {
      "name": "Read",
      "id": "toolu_abc123",
      "input": {"file_path": "/path/to/file"}
    }
  ],
  "timestamp": "2026-05-06T10:00:00.000Z"
}
```

- `text`: Concatenated text from all `type: "text"` content blocks. Empty string if no text blocks.
- `tools`: List of tool_use blocks from the content array. Empty list if no tool calls.
- `id` on each tool: the `tool_use_id` for matching with tool results.
- `parent_tool_use_id` (optional): Present for subagent events. Links to the Agent tool call in the root conversation that spawned this subagent.
- `agent_id` (optional): Present for subagent events. Identifies which subagent produced this event.

#### Tool Result Event
```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_abc123",
  "tool_name": "Read",
  "content": "file contents here...",
  "is_error": false,
  "timestamp": "2026-05-06T10:00:01.000Z"
}
```

- `content`: The tool's output, capped at 50K chars (default). If truncated, ends with `"[truncated]"`.
- `tool_name`: Resolved by matching `tool_use_id` back to the preceding assistant event's tool call. Empty string if unresolvable.
- `is_error`: True if the tool call failed.

#### System Event
```json
{
  "type": "system",
  "subtype": "init",
  "model": "claude-opus-4-6",
  "timestamp": "2026-05-06T10:00:00.000Z"
}
```

- Preserved as-is from stream-json. Only `init` subtype is common.

#### Result Event
```json
{
  "type": "result",
  "cost_usd": 0.15,
  "num_turns": 5,
  "timestamp": null
}
```

- Extracted from the final `result` event. `timestamp` is null (result events don't have timestamps in stream-json).

#### Subagent Assistant Event

```json
{
  "type": "assistant",
  "text": "Subagent working on the task...",
  "tools": [{"name": "Read", "id": "toolu_sub_001", "input": {"file_path": "/sub/file"}}],
  "parent_tool_use_id": "toolu_agent_001",
  "agent_id": "agent-001",
  "timestamp": "2026-05-06T10:00:02.000Z"
}
```

- Same structure as root assistant events, plus `parent_tool_use_id` and `agent_id`.
- `parent_tool_use_id` links to the Agent tool_use block in the root conversation.
- `agent_id` identifies the subagent instance (from transcript filename or streamed metadata).

### Events File (`events.json`)

```json
[
  {"type": "system", "subtype": "init", "model": "claude-opus-4-6", "timestamp": "..."},
  {"type": "assistant", "text": "Let me delegate this.", "tools": [{"name": "Agent", "id": "toolu_agent_001", "input": {"prompt": "..."}}], "timestamp": "..."},
  {"type": "assistant", "text": "Subagent working...", "tools": [{"name": "Read", "id": "toolu_sub_001", "input": {"file_path": "/f"}}], "parent_tool_use_id": "toolu_agent_001", "agent_id": "agent-001", "timestamp": "..."},
  {"type": "tool_result", "tool_use_id": "toolu_sub_001", "tool_name": "Read", "content": "file content", "is_error": false, "parent_tool_use_id": "toolu_agent_001", "agent_id": "agent-001", "timestamp": "..."},
  {"type": "tool_result", "tool_use_id": "toolu_agent_001", "tool_name": "Agent", "content": "Subagent completed.", "is_error": false, "timestamp": "..."},
  {"type": "assistant", "text": "Here is the result.", "tools": [], "timestamp": "..."},
  {"type": "result", "cost_usd": 0.15, "num_turns": 5, "timestamp": null}
]
```

### Config Extension (`TracesConfig`)

```
traces:
  stdout: true              # Keep raw stdout.log on disk (debugging)
  stderr: true              # Keep raw stderr.log on disk
  events: true              # Parse JSONL into events.json (default: true)
  event_result_cap: 50000   # Max chars per tool result content
  metrics: true             # Capture run_result.json metrics
```

## Relationships

- Each **tool_result** event references a **tool call** in a preceding **assistant** event via `tool_use_id`
- The **events list** for a case combines the case's `stdout.log` content with events from `subagents/*.jsonl` transcript files (deduplicated by message ID)
- `record["events"]` replaces `record["stdout"]`, `record["tool_calls"]` is derived from events

## State Transitions

None. Events are immutable once written at collection time.
