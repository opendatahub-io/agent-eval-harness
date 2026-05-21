# Quickstart: Using Built-in Judges

## Add a built-in judge to your eval.yaml

```yaml
judges:
  - name: safety_check
    builtin: no_harmful_content

  - name: budget_check
    builtin: cost_budget
    arguments:
      max_cost_usd: 0.50

  - name: tool_validation
    builtin: tool_call_validation

  - name: completeness
    builtin: output_completeness
    model: claude-sonnet-4-6
    arguments:
      strictness: high

thresholds:
  safety_check:
    min_pass_rate: 1.0
  budget_check:
    min_pass_rate: 1.0
```

FQN references also work: `builtin: safety/no_harmful_content`

## Available judges

| Name | Category | Type | What it checks |
|------|----------|------|----------------|
| `no_harmful_content` | safety | LLM | Agent output for harmful or dangerous content |
| `tool_call_validation` | process | Python | Tool calls complete successfully without errors |
| `cost_budget` | efficiency | Python | Execution cost within configurable threshold |
| `output_completeness` | quality | LLM | Output completeness and coverage via LLM evaluation |

## Arguments work everywhere

The `arguments` field passes parameters to any judge type:

```yaml
judges:
  # Builtin Python judge
  - name: budget
    builtin: cost_budget
    arguments:
      max_cost_usd: 0.50

  # External code judge
  - name: custom
    module: eval.judges.custom
    function: judge
    arguments:
      threshold: 0.8

  # Inline check judge
  - name: size_check
    check: |
      limit = arguments.get("max_chars", 10000)
      return (len(outputs.get("main_content", "")) <= limit, "ok")
    arguments:
      max_chars: 5000

  # Prompt file judge
  - name: quality
    prompt_file: judges/quality.md
    arguments:
      focus: completeness
```

## Customize a built-in judge

Copy the judge file and reference it using the standard field for its type:

**Python judges** (copy `.py`, use `module`/`function`):
```yaml
judges:
  - name: my_custom_cost_check
    module: eval.judges.cost_budget
    function: judge
    arguments:
      max_cost_usd: 2.00
```

**LLM judges** (copy `.md`, use `prompt_file`):
```yaml
judges:
  - name: my_custom_completeness
    prompt_file: eval/judges/output_completeness.md
    model: claude-sonnet-4-6
    arguments:
      strictness: low
```
