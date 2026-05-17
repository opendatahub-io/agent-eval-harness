# Quickstart: Using Built-in Judges

## Add a built-in judge to your eval.yaml

```yaml
judges:
  - name: no_harmful_content
    type: builtin

  - name: cost_budget
    type: builtin
    config:
      max_cost_usd: 0.50

  - name: tool_call_validation
    type: builtin

thresholds:
  no_harmful_content:
    min_pass_rate: 1.0
  cost_budget:
    min_pass_rate: 1.0
```

## Available judges

| Name | Category | What it checks |
|------|----------|----------------|
| `no_harmful_content` | safety | Agent output for harmful or dangerous content |
| `tool_call_validation` | process | Tool calls complete successfully without errors |
| `cost_budget` | efficiency | Execution cost within configurable threshold |

## Customize a built-in judge

1. Copy the judge file from `agent_eval/judges/<category>/<name>.py` to your project
2. Modify as needed
3. Reference it as a standard code judge:

```yaml
judges:
  - name: my_custom_cost_check
    module: eval.judges.cost_budget
    function: judge
```
