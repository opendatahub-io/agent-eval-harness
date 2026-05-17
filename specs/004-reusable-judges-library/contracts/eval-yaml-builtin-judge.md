# Contract: Builtin Judge in eval.yaml

## eval.yaml Schema Extension

```yaml
judges:
  - name: <builtin_judge_name>    # Required: matches a registered builtin judge
    type: builtin                  # Required: triggers builtin resolution
    description: <string>          # Optional: overrides the judge's default description
    condition: <python_expr>       # Optional: skip judge if expression returns False
    config:                        # Optional: passed as second argument to judge function
      <key>: <value>
```

## Examples

### Minimal (no config)
```yaml
judges:
  - name: no_harmful_content
    type: builtin
```

### With config
```yaml
judges:
  - name: cost_budget
    type: builtin
    config:
      max_cost_usd: 0.50
```

### With condition
```yaml
judges:
  - name: tool_call_validation
    type: builtin
    condition: "len(outputs.get('tool_calls', [])) > 0"
```

### Mixed with other judge types
```yaml
judges:
  - name: no_harmful_content
    type: builtin

  - name: has_output
    check: |
      content = outputs.get("main_content", "")
      return (len(content) > 0, f"Content length: {len(content)}")

  - name: quality
    prompt: "Rate the output quality 1-5."
    model: claude-sonnet-4-6

thresholds:
  no_harmful_content:
    min_pass_rate: 1.0
  quality:
    min_mean: 3.5
```

## Judge Function Contract

```python
def judge(outputs: dict, config: dict | None = None) -> tuple[bool, str]:
    """
    Args:
        outputs: Case record dict containing files, tool_calls, events,
                 annotations, execution metrics, and conversation text.
        config: Optional configuration dict from eval.yaml judge entry.
                None if no config specified.

    Returns:
        Tuple of (passed: bool, rationale: str).
        - passed: True if the case meets the judge's criteria
        - rationale: Human-readable explanation of the result
    """
```

## Error Conditions

| Condition | Error Message Pattern |
|-----------|----------------------|
| Unknown builtin name | `Unknown builtin judge '{name}'. Available: {sorted_names}` |
| Duplicate judge name | `Duplicate judge name '{name}' in eval.yaml` |
| Name collision across categories | `Builtin judge name collision: '{name}' found in both {cat1}/ and {cat2}/` |
