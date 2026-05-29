"""Validates that tool calls completed successfully without errors.

Required fields: tool_calls, events
Failure means: One or more tool calls had error results or missing responses.
"""


def judge(outputs, **kwargs):
    tool_calls = outputs.get("tool_calls", [])
    if not tool_calls:
        return (True, "No tool calls to validate")

    errors = []
    for i, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        result = call.get("result", call.get("output", ""))
        if isinstance(result, dict) and result.get("error"):
            errors.append(f"Tool '{call.get('name', f'#{i}')}': {result['error']}")
        elif isinstance(result, str) and "error" in result.lower()[:50]:
            errors.append(f"Tool '{call.get('name', f'#{i}')}': {result[:100]}")

    if errors:
        return (False, f"Tool call errors: {'; '.join(errors)}")
    return (True, f"All {len(tool_calls)} tool calls completed successfully")
