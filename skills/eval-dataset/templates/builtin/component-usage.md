# Component Usage Test Template

**Category**: component-usage  
**Purpose**: Verify agents can explain how to use APIs/components with correct examples from agentic documentation

---

## Test Case Structure

Each component-usage test should verify that an agent:
1. Finds API/component documentation (in ai-docs/, CLAUDE.md, etc.)
2. Explains correct usage with examples
3. Includes required fields and proper structure
4. Provides working examples in the correct format (YAML, code, etc.)

## Input Schema

```yaml
# input.yaml
prompt: "Question about how to use a specific API or component"
expected_api: "APIName or ComponentName"
expected_example_type: yaml  # yaml | code | json | command
expected_fields:
  - field1
  - field2
expected_documentation:
  - path/to/api-docs.md
```

## Generation Instructions

For each test case:

1. **Select an API or component** from `domain.apis[]` or `domain.components[]`
2. **Generate a usage question** (how to configure, how to use, what fields are needed)
3. **Identify required fields** from the API documentation
4. **Specify example format** (YAML for k8s APIs, code for libraries, etc.)
5. **Reference API documentation**

## Example

Given domain context:
```yaml
apis:
  - name: MachineConfig
    documentation: ai-docs/domain/machineconfig.md
    example_type: yaml
```

Generate:
```yaml
# input.yaml
prompt: "How do I use MachineConfig to set kernel parameters on my nodes?"
expected_api: MachineConfig
expected_example_type: yaml
expected_fields:
  - apiVersion
  - kind
  - metadata
  - spec
  - kernelArguments
expected_documentation:
  - ai-docs/domain/machineconfig.md
```

## Annotations (Optional)

```yaml
# annotations.yaml
category: component-usage
api_type: kubernetes-crd  # or: library, cli-tool, rest-api
use_case: configuration  # or: basic-usage, advanced-features, troubleshooting
```

## Validation Criteria

- `prompt` must ask about a specific use case for the API/component
- `expected_api` must match an entry in domain.apis or domain.components
- `expected_example_type` must match the API's typical format
- `expected_fields` should include key required fields (not exhaustive)
- `expected_documentation` must reference actual API docs
