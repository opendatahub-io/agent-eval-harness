# Navigation Test Template

**Category**: navigation  
**Purpose**: Verify agents can find and navigate to relevant agentic documentation (CLAUDE.md, ai-docs/, etc.)

---

## Test Case Structure

Each navigation test should verify that an agent can:
1. Locate the correct agentic documentation files
2. Navigate through the documentation structure
3. Find specific information to answer a question

## Input Schema

```yaml
# input.yaml
prompt: "User question requiring documentation lookup"
expected_files:
  - path/to/doc1.md
  - path/to/doc2.md
expected_mentions:
  - keyword1
  - keyword2
```

## Generation Instructions

For each test case:

1. **Select a topic** from `domain.documentation_structure.areas[*].topics`
2. **Generate a user question** that requires finding documentation on that topic
3. **Identify which files** contain the answer (from actual repo structure)
4. **List keywords** that should appear in the agent's response

## Example

Given domain context:
```yaml
documentation_structure:
  entry_point: CLAUDE.md
  areas:
    - path: ai-docs/workflows/
      topics: [enhancement-process, testing-workflow]
```

Generate:
```yaml
# input.yaml
prompt: "How do I create a new enhancement proposal?"
expected_files:
  - CLAUDE.md
  - ai-docs/workflows/enhancement-process.md
expected_mentions:
  - enhancement
  - proposal
  - template
```

## Annotations (Optional)

```yaml
# annotations.yaml
category: navigation
difficulty: easy  # easy | medium | hard
topic: enhancement-process
```

## Validation Criteria

- `prompt` must be a natural question (not a direct command)
- `expected_files` must reference real files from the repository
- At least one expected_file should contain the answer
- `expected_mentions` should be 2-5 keywords
