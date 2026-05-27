# Authoring Test Template

**Category**: authoring  
**Purpose**: Verify agents can create content following patterns and templates from agentic documentation

---

## Test Case Structure

Each authoring test should verify that an agent:
1. Finds relevant agentic documentation and templates
2. Follows documented structure and patterns
3. Applies constraints and best practices
4. Produces content in the correct format

## Input Schema

```yaml
# input.yaml
prompt: "Request to create new content (document, config, code, etc.)"
expected_structure:
  - section1
  - section2
  - section3
expected_patterns:
  - pattern1
  - pattern2
expected_documentation:
  - path/to/template.md
  - path/to/guidelines.md
```

## Generation Instructions

For each test case:

1. **Select a content type** that has documented templates (enhancement proposal, API definition, configuration, etc.)
2. **Generate a creation request** with specific fictional requirements
3. **Identify the structure** the output should follow (from templates)
4. **List patterns** that should appear (naming conventions, required fields, etc.)
5. **Reference template documentation**

## Example

Given domain context:
```yaml
documentation_structure:
  areas:
    - path: ai-docs/workflows/
      topics: [enhancement-process]
    - path: ai-docs/templates/
      topics: [enhancement-template]
```

Generate:
```yaml
# input.yaml
prompt: |
  I want to propose a new feature for automatic rollback of failed deployments.
  Help me create an enhancement proposal following the project template.
expected_structure:
  - Title
  - Summary
  - Motivation
  - Proposal
  - Design Details
  - Implementation History
expected_patterns:
  - "Title format: Enhancement [Number]: [Brief Description]"
  - "## Summary section with 2-3 sentences"
  - "Graduation criteria section"
expected_documentation:
  - ai-docs/workflows/enhancement-process.md
  - ai-docs/templates/enhancement-template.md
```

## Annotations (Optional)

```yaml
# annotations.yaml
category: authoring
content_type: enhancement-proposal  # or: api-definition, config, etc.
complexity: medium  # simple | medium | complex
```

## Validation Criteria

- `prompt` must request creation of specific content
- `expected_structure` should list major sections/components
- `expected_patterns` should be verifiable (not subjective)
- `expected_documentation` must reference templates or guidelines
- The fictional scenario should be realistic but clearly fictional
