# Architecture Test Template

**Category**: architecture  
**Purpose**: Verify agents can explain system design and component interactions from agentic documentation

---

## Test Case Structure

Each architecture test should verify that an agent:
1. Finds architecture documentation (in ai-docs/architecture/, CLAUDE.md, etc.)
2. Explains how components work together
3. Describes data flow and interactions
4. References architectural diagrams or descriptions

## Input Schema

```yaml
# input.yaml
prompt: "Question about system architecture or component relationships"
expected_components:
  - component1
  - component2
expected_interactions:
  - "component1 → component2: description"
expected_documentation:
  - path/to/architecture.md
  - path/to/component-docs.md
```

## Generation Instructions

For each test case:

1. **Select components** from `domain.components[]` that interact
2. **Generate an architecture question** (how does X work, how do components communicate, what's the data flow)
3. **Identify key interactions** between components
4. **Reference architecture documentation**

## Example

Given domain context:
```yaml
components:
  - machine-config-daemon
  - machine-config-controller
  - machine-config-server
documentation_structure:
  areas:
    - path: ai-docs/architecture/
      topics: [components, data-flow]
```

Generate:
```yaml
# input.yaml
prompt: |
  Can you explain how the machine config system applies configuration changes
  to nodes? What components are involved and how do they communicate?
expected_components:
  - machine-config-controller
  - machine-config-daemon
  - machine-config-server
expected_interactions:
  - "controller → daemon: pushes new configs via API"
  - "daemon → node: applies changes locally"
  - "daemon → server: reports status back"
expected_documentation:
  - ai-docs/architecture/components.md
  - ai-docs/architecture/data-flow.md
```

## Annotations (Optional)

```yaml
# annotations.yaml
category: architecture
focus: component-interaction  # or: data-flow, deployment, scaling
complexity: medium  # simple | medium | complex
```

## Validation Criteria

- `prompt` must ask about architecture, design, or component relationships
- `expected_components` must reference 2+ components from domain config
- `expected_interactions` should describe key communication patterns
- `expected_documentation` must reference architecture docs
- The question should require understanding the big picture, not just one component
