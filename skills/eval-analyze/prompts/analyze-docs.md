# Documentation Analysis Prompt for eval-analyze

You are analyzing a repository's **agentic documentation** (CLAUDE.md, AGENTS.md, ai-docs/, etc.) to generate an evaluation configuration (`eval.yaml`) for testing whether AI agents can effectively navigate and use this documentation.

**What to analyze**: Agentic documentation written FOR agents (not manual user docs like README.md or end-user guides).

**Context**: This prompt is invoked via `/eval-analyze --prompt builtin:docs`. It generates a prompt-mode eval config (not a skill eval).

**Goal**: Test if agents can:
- Navigate and find relevant information in the agentic docs
- Apply documented patterns to new scenarios
- Reject approaches that violate documented constraints
- Correctly use APIs and components from documentation alone

---

## Step 1: Discover Documentation Structure

### 1.1 Find Entry Point

Look for the main entry point (usually `CLAUDE.md` or `AGENTS.md`):

```bash
find . -maxdepth 1 \( -name "CLAUDE.md" -o -name "AGENTS.md" \)
```

Read it to understand:
- What this repository is about
- How documentation is organized
- Where key information lives

### 1.2 Explore Documentation Directories

Find the main documentation areas:

```bash
find . -type d \( -name "ai-docs" -o -name "docs" -o -name "guidelines" \) | head -10
```

For each documentation area found above, list the structure:

```bash
# For each discovered area (ai-docs, docs, guidelines, etc.):
find <doc-area> -name "*.md" | head -30
```

Categorize directories by purpose:
- **Workflows**: Processes, how-to guides (e.g., `ai-docs/workflows/`)
- **Domain**: APIs, CRDs, concepts (e.g., `ai-docs/domain/`)
- **Architecture**: System design, components (e.g., `ai-docs/architecture/`)
- **Practices**: Best practices, patterns (e.g., `ai-docs/practices/`)
- **Decisions**: ADRs, design decisions (e.g., `ai-docs/decisions/`)
- **References**: Links, ecosystem (e.g., `ai-docs/references/`)

### 1.3 Sample Documentation

Read 3-5 representative docs from each area to understand:
- **Content type**: Concept explanations, API references, step-by-step guides, rules/constraints
- **Depth**: High-level overviews vs. detailed specifications
- **Target audience**: Developers, operators, contributors

---

## Step 2: Identify Repository Type and Focus

Based on what you read, classify the repository:

### Type A: Enhancement/Design Repository
**Characteristics**:
- Contains enhancement proposals, RFCs, ADRs
- Focuses on **process** (how to propose/design features)
- Has **constraints** ("must use X pattern", "avoid Y")
- Examples: openshift/enhancements

**Test focus**: Process adherence, pattern application, constraint enforcement

### Type B: Component/Code Repository
**Characteristics**:
- Contains component documentation (APIs, CRDs, libraries)
- Focuses on **usage** (how to use this component)
- Has **examples** (YAML snippets, code samples)
- Examples: openshift/machine-config-operator

**Test focus**: API usage, example accuracy, architecture understanding

### Type C: General Documentation Repository
**Characteristics**:
- Pure documentation (no code or proposals)
- Focuses on **information retrieval** (finding the right docs)
- Has **hierarchy** (navigation, search, index)
- Examples: Standalone docs sites

**Test focus**: Navigation, search, information accuracy

---

## Step 3: Extract Domain Knowledge

### 3.1 Documentation Structure

Map out the documentation organization:

**CRITICAL**: Use the ACTUAL path from Step 1.1's find command output. Entry point is at repository root (CLAUDE.md or AGENTS.md), NOT in subdirectories like ai-docs/.

```yaml
documentation_structure:
  entry_point: CLAUDE.md  # or AGENTS.md (whichever exists at root - prefer CLAUDE.md if both)
  areas:
    - path: ai-docs/workflows/
      topics: [enhancement-process, feature-implementation, testing-workflow]
    - path: ai-docs/platform/
      topics: [operator-patterns, status-conditions, webhooks]
    - path: ai-docs/practices/
      topics: [api-evolution, security, testing]
```

### 3.2 Identify Constraints (if Type A)

Look for explicit rules, "must/must not" statements, anti-patterns:

```bash
grep -r "must use" ai-docs/
grep -r "do not" ai-docs/
grep -r "anti-pattern" ai-docs/
grep -r "deprecated" ai-docs/
```

For each constraint found:
- **Rule**: What the constraint enforces
- **Documentation**: Where it's documented
- **Wrong approach**: What would violate it

Example:
```yaml
constraints:
  - rule: "New APIs must start with v1alpha1"
    documentation: ai-docs/practices/development/api-evolution.md
    wrong_approach: "Starting with v1 API for stability"
```

### 3.3 Identify APIs/Components (if Type B)

Look for API documentation, CRDs, libraries:

```bash
find ai-docs/domain -name "*.md"
find ai-docs/apis -name "*.md"
```

For each API/component:
- **Name**: API or component name
- **Documentation**: Path to docs
- **Example type**: yaml, code, json

Example:
```yaml
apis:
  - name: MachineConfig
    documentation: ai-docs/domain/machineconfig.md
    example_type: yaml
  - name: KubeletConfig
    documentation: ai-docs/domain/kubeletconfig.md
    example_type: yaml
```

### 3.4 Identify Components (if architectural)

If the repo has multi-component architecture:

```bash
grep -r "component" ai-docs/architecture/
```

List key components:
```yaml
components:
  - machine-config-daemon
  - machine-config-controller
  - machine-config-server
```

---

## Step 4: Suggest Test Categories

**CRITICAL**: Only use these builtin templates - do NOT invent new categories:
- `builtin:navigation` - Finding documentation
- `builtin:component-usage` - API/component usage with examples
- `builtin:architecture` - Component relationships and data flows
- `builtin:authoring` - Creating content following patterns (Type A repos)
- `builtin:anti-pattern` - Rejecting constraint violations (Type A repos)

**CRITICAL**: The test_categories field MUST use `count` (not `target_cases`, `target_count`, or `num_cases`). The generation script will fail with a schema error if you use the wrong field name. See the examples below for the exact format.

Based on repository type and content, suggest test categories FROM THE LIST ABOVE ONLY:

### For Type A (Enhancement/Design):
```yaml
test_categories:
  - name: navigation
    template: builtin:navigation
    count: 2
    description: Agent finds relevant process documentation
    
  - name: authoring
    template: builtin:authoring
    count: 1
    description: Agent creates content following documented patterns
    
  - name: anti-pattern
    template: builtin:anti-pattern
    count: 3  # One per major constraint
    description: Agent rejects approaches that violate constraints
```

### For Type B (Component/Code):
```yaml
test_categories:
  - name: navigation
    template: builtin:navigation
    count: 2
    description: Agent finds API/component documentation
    
  - name: component-usage
    template: builtin:component-usage
    count: 3  # One per major API
    description: Agent explains how to use APIs with examples
    
  # Optional:
  # - name: architecture
  #   template: builtin:architecture
  #   count: 1
  #   description: Agent explains how components work together
```

### For Type C (General Docs):
```yaml
test_categories:
  - name: navigation
    template: builtin:navigation
    count: 4
    description: Agent finds information via search and hierarchy
```

---

## Step 5: Generate Dataset Schema

Based on test categories, describe what each test case should contain:

### For Navigation Tests:
```yaml
schema: |
  Each case contains:
  - input.yaml: with 'prompt' field (user's question)
  - expected_files: list of documentation files agent should read
  - expected_mentions: keywords that should appear in response
```

### For Authoring Tests:
```yaml
schema: |
  Each case contains:
  - input.yaml: with 'prompt' field (creation task)
  - expected_patterns: list of patterns agent should apply
  - expected_documentation: docs agent should reference
```

### For Anti-Pattern Tests:
```yaml
schema: |
  Each case contains:
  - input.yaml: with 'prompt' field (request for wrong approach)
  - expected_rejection: true
  - expected_guidance: correct approach keywords
  - expected_documentation: constraint documentation paths
```

### For Component-Usage Tests:
```yaml
schema: |
  Each case contains:
  - input.yaml: with 'prompt' field (API usage question)
  - expected_api_mention: API/component name
  - expected_example_type: yaml|code|json
  - expected_fields: key fields that should be explained
```

---

## Step 6: Suggest Judges

**CRITICAL**: Judges MUST have executable implementations using one of:
- `check:` - Inline Python code that returns 0.0-1.0
- `llm_rubric:` - Natural language rubric for LLM-based scoring  
- `prompt:` or `prompt_file:` - Detailed LLM evaluation instructions
- `module:` - External Python module reference

**NEVER use `scoring:` or `validation:` or any other field** - these are not recognized by the scoring engine and will be silently ignored.

### Category-Specific Judges

**IMPORTANT**: Use the `if:` field to make judges category-specific. This prevents judges from penalizing responses for not covering content outside their test category's scope.

**Pattern**: Each judge should use `if:` to specify which test categories it applies to:

```yaml
judges:
  - name: navigation-accuracy
    if: "annotations.get('category') == 'navigation'"
    description: Did the agent find the right documentation files?
    llm_rubric: |
      Score on scale 1-5:
      - 5: Found and listed all expected files, showed evidence of reading CLAUDE.md/AGENTS.md first
      - 4: Found most files but missed 1-2, or skipped reading entry point docs
      - 3: Found some correct files but also read many irrelevant ones
      - 2: Minimal doc usage
      - 1: Did not consult expected documentation
      
      Check: Does "Documentation Used" section list files matching expected_files?
      Did agent read CLAUDE.md or AGENTS.md to navigate to the right docs?

  - name: api-usage-example-quality
    if: "annotations.get('category') == 'component-usage'"
    description: Quality of YAML examples and API explanations
    llm_rubric: |
      Score on scale 1-5 based on YAML example completeness, API group correctness,
      field explanations, and documentation references.

  - name: architecture-understanding
    if: "annotations.get('category') == 'architecture'"
    description: Understanding of component relationships and data flows
    llm_rubric: |
      Score on scale 1-5 based on whether the agent explains how components interact,
      data flows between them, and references architecture documentation.
```

**Why this matters**: Without `if:`, all judges evaluate all tests. A component-usage test asking "How do I use KubeletConfig?" would be penalized by the architecture-understanding judge for not explaining full MCO architecture, even though the question didn't ask for that. The `if:` field makes judges skip irrelevant tests.

### Universal Judges (all doc evals):
```yaml
judges:
  - name: has-documentation-section
    check: |
      import re
      # Check for "## Documentation Used" section in stdout
      match = re.search(r'## Documentation Used', outputs.get('stdout', ''))
      return 1.0 if match else 0.0
    description: Agent must list files it read
```

### Navigation Judges (navigation category only):
```yaml
  # Builtin judge: verifies agent read expected documentation files
  - name: consulted-docs
    builtin: consulted_docs
    if: "annotations.get('category') == 'navigation'"
    arguments:
      min_coverage: 0.8  # 80% of expected_files must be read
      match: suffix      # Match by file path suffix (default)
    description: Agent must read the expected documentation files
  
  # LLM judge: evaluates navigation quality and strategy
  - name: navigation-accuracy
    if: "annotations.get('category') == 'navigation'"
    description: Did the agent find the right documentation files?
    llm_rubric: |
      Score on scale 1-5:
      - 5: Found and listed all expected files, showed evidence of reading CLAUDE.md/AGENTS.md first
      - 4: Found most files but missed 1-2, or skipped reading entry point docs
      - 3: Found some correct files but also read many irrelevant ones
      - 2: Minimal doc usage, mostly relied on general knowledge
      - 1: Did not consult expected documentation

      Check: Does "Documentation Used" section list files matching expected_files?
      Did agent read CLAUDE.md or AGENTS.md to navigate to the right docs?
```

### Anti-Pattern Judges (anti-pattern category only):
```yaml
  - name: rejected-wrong-approach
    if: "annotations.get('category') == 'anti-pattern'"
    description: Agent must reject constraint violations
    llm_rubric: |
      Score on scale 1-5:
      - 5: Agent explicitly rejects the request and explains why it violates documented rules
      - 4: Agent rejects but explanation could be clearer
      - 3: Agent hesitates or provides mixed message
      - 2: Agent almost agrees or doesn't clearly reject
      - 1: Agent accepts the wrong approach
    
  - name: cited-constraint-docs
    if: "annotations.get('category') == 'anti-pattern'"
    description: Agent must cite constraint documentation
    llm_rubric: |
      Score on scale 1-5:
      - 5: Explicitly cites constraint documentation (e.g., "ai-docs/domain/machineconfig.md states...")
      - 4: References constraint but not exact documentation
      - 3: Mentions constraint exists but no documentation reference
      - 2: Vague mention of rule
      - 1: No constraint documentation cited
```

### Component-Usage Judges (component-usage category only):
```yaml
  - name: api-usage-example-quality
    if: "annotations.get('category') == 'component-usage'"
    description: Quality of YAML examples and API explanations
    llm_rubric: |
      Score on scale 1-5:
      - 5: Complete working YAML with correct API group, explains key fields, cites docs
      - 4: Good YAML with minor gaps
      - 3: Basic example but missing field explanations
      - 2: Example incomplete or incorrect
      - 1: No example or completely wrong

      Check: apiVersion, kind, metadata, spec fields, doc references
```

### Architecture Judges (architecture category only):
```yaml
  - name: architecture-understanding
    if: "annotations.get('category') == 'architecture'"
    description: Understanding of component relationships and data flows
    llm_rubric: |
      Score on scale 1-5:
      - 5: Explains data flow between all components, mentions roles, traces full lifecycle
      - 4: Covers most components and flow
      - 3: Some component relationships but gaps
      - 2: Partial understanding, significant gaps
      - 1: Confused about roles or relationships or fundamentally incorrect

      Check: Component roles (not just names), data flow, CRDs mentioned, architecture docs cited
```

---

## Step 7: Generate eval.yaml

**YAML SYNTAX**: Quote all list items containing special chars `()`, `-`, `:` to avoid parse errors. Example: `- "field (type)"` not `- field (type)`.

Produce a complete `eval.yaml` configuration:

**CRITICAL**: Keep `execution.arguments` exactly as shown below. The `{prompt}` placeholder MUST be the first line - it gets replaced with the actual test question at runtime. Do NOT add custom instructions before `{prompt}` or replace it with repository-specific text.

```yaml
name: {repo-name}-docs-eval
description: Test AI agents using {repo-name} documentation

# No skill field - this is prompt-based evaluation (execution.prompt)
execution:
  mode: case
  # Jinja2 template: {{ input.field }} accesses input.yaml fields
  # Alternative simple syntax: {field} for basic substitution
  prompt: |
    {{ input.prompt }}

    CRITICAL REQUIREMENTS:
    1. You MUST read documentation files BEFORE answering:
       - Start by reading CLAUDE.md or AGENTS.md to understand the documentation structure
       - Use the navigation guidance in those files to find relevant documentation
       - Read the specific documentation files needed to answer the question
    2. Base your answer ONLY on information you find in the documentation files - do NOT rely on general knowledge
    3. After your answer, include a "## Documentation Used" section listing all files you actually read:
       - path/to/file.md (section name if applicable)
       - CLAUDE.md or AGENTS.md
    
    If you cannot find relevant documentation, say so explicitly rather than answering from general knowledge.

runner:
  type: claude-code
  workspace_mode: repo  # Enable in-repo execution for documentation access
  settings:
    # CRITICAL: These exact tools are REQUIRED (not optional):
    # - Read: Access documentation files
    # - Grep: Search across files for keywords/patterns
    # - Glob: Find files by pattern matching
    # DO NOT substitute Bash - it bypasses permission controls and breaks test isolation
    append_allowed_tools: ['Read', 'Grep', 'Glob']
    permission_mode: auto

models:
  skill: claude-sonnet-4-6
  judge: claude-opus-4-6

permissions:
  allow: []
  deny:
    # CRITICAL: Block ALL tools that could access test infrastructure
    # Tool list must include: Read, Grep, Glob, Bash
    - path: "eval/"
      tools: ["Read", "Grep", "Glob", "Bash"]
      reason: "Test cases contain answer keys (expected_files, expected_mentions, etc.) and run results from other agents"
    - path: "eval.yaml"
      tools: ["Read", "Grep", "Bash"]
      reason: "Eval config contains domain knowledge, expected schemas, and judge criteria"
    - path: "eval.md"
      tools: ["Read", "Grep", "Bash"]
      reason: "Analysis cache contains documentation structure map, API lists, and component details"
    - path: "tmp/"
      tools: ["Read", "Grep", "Glob", "Bash"]
      reason: "Harness state files not relevant to documentation testing"

mlflow:
  experiment: {repo-name}-docs-eval

dataset:
  path: eval/dataset
  schema: |
    {generated schema from Step 5}
  
  test_categories:
    {suggested categories from Step 4}
  
  domain:
    type: {repo-type}  # e.g., openshift-enhancements, machine-config-operator
    
    documentation_structure:
      {from Step 3.1}
    
    # Conditional: only if Type A
    constraints:
      {from Step 3.2}
    
    # Conditional: only if Type B
    apis:
      {from Step 3.3}
    
    # Conditional: only if Type B with architecture
    components:
      {from Step 3.4}

outputs:
  - path: output
    schema: "stdout.log: Agent's response"

traces:
  stdout: true
  stderr: true
  events: true
  metrics: true

judges:
  # CRITICAL: Include if: field for category-specific judges
  # See Step 6 examples - each judge should specify which test categories it evaluates
  # Example: if: "annotations.get('category') == 'navigation'"
  {suggested judges from Step 6}

thresholds:
  # Map judge names to their regression thresholds
  # For each judge listed above, set appropriate thresholds:
  # - min_pass_rate: for boolean judges (0.0-1.0)
  # - min_mean: for numeric judges (1-5 for LLM rubrics)
  {judge_name}:
    min_pass_rate: 0.8  # or min_mean: 3.5 for LLM rubric judges
```

---

## Output Format

Provide the following in your response:

### 1. Repository Analysis

```yaml
repository_type: enhancement|component|docs
focus_areas:
  - workflows
  - domain
  - practices
key_characteristics:
  - Has explicit constraints
  - API-focused
  - Multi-component architecture
```

### 2. Suggested eval.yaml

The complete configuration file (as shown in Step 7).

### 3. Recommended Next Steps

```markdown
## Next Steps

1. **Review generated eval.yaml**
   - Verify test categories match repository goals
   - Adjust constraint/API lists if needed

2. **Generate test cases**
   ```bash
   /eval-dataset --config eval.yaml
   ```

3. **Run evaluation**
   ```bash
   /eval-run --model sonnet --config eval.yaml
   ```
```

---

## Important Notes

- **Only use existing builtin templates**: navigation, component-usage, architecture, authoring, anti-pattern. Do NOT invent new template names like "workflow" - they will fail at dataset generation.
- **CRITICAL - Category-specific judges**: Every judge (except universal ones like has-documentation-section) MUST include an `if:` field to specify which test categories it evaluates. Example: `if: "annotations.get('category') == 'navigation'"`. This prevents judges from penalizing responses for not covering content outside their test category's scope. A component-usage test asking "How do I use X?" should NOT be judged on architectural breadth.
- **Don't assume**: If you can't find constraints or APIs, don't invent them. Only include what you actually found in the documentation.
- **Be specific**: Use actual file paths, not placeholders.
- **Test category count**: Suggest realistic numbers (2-3 navigation, 1-3 per API, 1 per constraint).
- **Judge implementation**: All judges must use `check:`, `llm_rubric:`, `prompt:`, or `module:` fields. Never use `scoring:` or `validation:` - they are not recognized.
