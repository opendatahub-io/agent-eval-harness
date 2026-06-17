# Documentation Test Case Templates

Templates for **documentation and knowledge-base evaluation**. These test whether agents can navigate, understand, and apply information from agentic documentation (CLAUDE.md, AGENTS.md, ai-docs/, etc.).

**Domain-agnostic**: Work with any documentation domain (Python, React, Kubernetes, Terraform, etc.). See `examples/openshift-agentic-docs.md` for domain-specific usage.

**Other evaluation scenarios**: For testing other agent capabilities (code generation, reasoning, tool use), create custom template categories following the same structure.

## Available Templates

| Template | Purpose | Use Case |
|----------|---------|----------|
| `navigation.md` | Finding documentation | Test if agents can locate and navigate to relevant docs |
| `anti-pattern.md` | Rejecting violations | Test if agents reject approaches that violate documented constraints |
| `authoring.md` | Creating content | Test if agents can create content following documented patterns |
| `component-usage.md` | API/component usage | Test if agents can explain APIs with correct examples from docs |
| `architecture.md` | System design | Test if agents understand component interactions from architecture docs |

## Usage

Templates are referenced in `eval.yaml`:

```yaml
dataset:
  test_categories:
    - name: navigation
      template: documentation/navigation
      count: 2
      description: "Agent finds relevant documentation"
```

The `documentation/navigation` reference resolves to `skills/eval-dataset/templates/documentation/navigation.md`.

## Template Structure

Each template is a markdown file with:

1. **Purpose**: What capability this tests
2. **Input Schema**: YAML structure for input.yaml
3. **Generation Instructions**: How to create test cases
4. **Examples**: Sample generated test cases
5. **Validation Criteria**: What makes a valid test case

## How Generation Works

When `/eval-dataset` runs:

1. Reads `eval.yaml` test_categories
2. Resolves each template reference (`category/name` → file path)
3. Reads template content
4. Uses an LLM to generate `count` test cases following the template
5. Writes cases to `dataset.path/case-NNN/`

## Domain Context

Templates receive domain-specific context from `eval.yaml`:

```yaml
dataset:
  domain:
    type: repo-type
    documentation_structure: {...}
    constraints: [...]
    apis: [...]
    components: [...]
```

Templates use this context to generate repository-specific test cases.

## Custom Templates

You can create custom templates:

```yaml
dataset:
  test_categories:
    - name: my-custom-test
      template: eval/templates/my-template.md
      count: 3
```

Custom templates should follow the same structure as these documentation templates.

## Template Design Principles

1. **Generic, not hardcoded**: Templates work across repositories
2. **Context-driven**: Use domain config to customize generation
3. **Verifiable**: Generated tests should be objectively verifiable
4. **Realistic**: Test cases should mirror real-world scenarios
5. **Specific**: Avoid vague or ambiguous prompts
