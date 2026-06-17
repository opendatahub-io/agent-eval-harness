# Anti-Pattern Test Template

**Category**: anti-pattern  
**Purpose**: Verify agents reject approaches that violate constraints documented in agentic documentation

---

## Test Case Structure

Each anti-pattern test should verify that an agent:
1. Recognizes a constraint violation in the user's request
2. Rejects the proposed approach
3. Explains why it violates constraints (referencing agentic docs)
4. Suggests the correct approach

## Input Schema

```yaml
# input.yaml
prompt: "User request proposing a constraint-violating approach"
expected_rejection: true
expected_constraint: "The constraint that should be cited"
expected_documentation:
  - path/to/constraint-doc.md
correct_approach: "Brief description of the right way"
```

## Generation Instructions

For each test case:

1. **Select a constraint** from `domain.constraints[]`
2. **Generate a user request** that proposes violating that constraint
3. **Reference the documentation** where the constraint is defined
4. **Describe the correct approach** that follows the constraint

## Example

Given domain context:
```yaml
constraints:
  - rule: "All APIs must start with v1alpha1"
    documentation: ai-docs/practices/api-evolution.md
    wrong_approach: "Starting with v1 for stability"
```

Generate:
```yaml
# input.yaml
prompt: |
  I want to create a new API for my feature. Since this is production-ready,
  I'd like to start with v1 to show stability. Can you help me design it?
expected_rejection: true
expected_constraint: "All APIs must start with v1alpha1"
expected_documentation:
  - ai-docs/practices/api-evolution.md
correct_approach: "Start with v1alpha1, graduate to v1 later"
```

## Annotations (Optional)

```yaml
# annotations.yaml
category: anti-pattern
constraint_type: api-versioning  # or: security, architecture, process
severity: high  # high | medium | low
```

## Validation Criteria

- `prompt` must explicitly request a constraint-violating approach
- `expected_rejection` must be true
- `expected_constraint` must match a constraint from domain config
- `expected_documentation` must reference real files
- `correct_approach` should be concise (1-2 sentences)
