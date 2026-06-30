# Execution Modes: execution.prompt vs execution.arguments

The eval harness supports two execution modes, controlled by which field is set in `eval.yaml`:

## Skill Mode (execution.skill + execution.arguments)

**Use when**: Testing skill implementations (SKILL.md + scripts)

```yaml
execution:
  mode: case
  skill: rfe.create          # Skill to invoke
  arguments: "{prompt}"       # Template for skill arguments
```

**What happens**:
- Claude Code invokes: `/rfe.create {prompt}` (after template resolution)
- The skill reads its SKILL.md and executes its logic
- Useful for testing if a skill's implementation works correctly

## Prompt Mode (execution.prompt)

**Use when**: Testing agent capabilities directly (documentation, patterns, APIs)

```yaml
execution:
  mode: case
  prompt: |                  # Direct prompt template (no skill wrapper)
    {{ input.prompt }}
    
    CRITICAL REQUIREMENTS:
    - Read documentation before answering
    - List files you consulted
```

**What happens**:
- Claude Code receives the prompt directly (no `/skill` wrapper)
- The agent processes it as a normal user message
- Useful for testing if agents can USE documentation, not if skills WORK

## Why Both Fields Exist

**execution.arguments**: Arguments passed TO a skill (skill mode only)
- Resolved per-case from input.yaml
- Example: `"--priority {priority} {prompt}"`
- Only used when `execution.skill` is set

**execution.prompt**: The complete prompt FOR the agent (prompt mode only)
- Replaces the skill invocation entirely
- Example: `"{{ input.prompt }}\n\nCRITICAL: Read docs first"`
- Only used when `execution.skill` is NOT set

The harness auto-detects mode based on which field is populated.

## Template Syntax

Both fields support two syntaxes (auto-detected):

**Simple** (backward compatible):
```yaml
arguments: "{prompt} --priority {priority?}"
```
- `{field}` → required
- `{field?}` → optional (empty string if missing)

**Jinja2** (recommended for complex templates):
```yaml
prompt: |
  {{ input.prompt }}
  
  {% if input.context %}
  Context: {{ input.context }}
  {% endif %}
```
- Full Jinja2 expressions supported
- Access fields via `{{ input.field }}`
- Auto-detected by presence of `{{`

## Common Pitfall

❌ **Don't do this**:
```yaml
execution:
  prompt: "placeholder"      # Non-empty prompt to trigger prompt mode
  arguments: "{prompt}"       # Actual template in wrong field
```

✓ **Do this instead**:
```yaml
execution:
  prompt: "{prompt}"          # Prompt IS the template
  # arguments not needed in prompt mode
```

The prompt field should contain the complete template, not a placeholder.
