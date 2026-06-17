# OpenShift Agentic Documentation Analysis Recipe

**This is a domain-specific example recipe** for evaluating OpenShift/Kubernetes operator and platform documentation. It includes ecosystem-specific terminology (CRDs, Operators, status conditions, webhooks, MachineConfig, etc.).

**To use this recipe**:
```bash
/eval-analyze --prompt examples/openshift-agentic-docs.md
```

**For other domains**: Create your own recipe by adapting this example.

---

# Documentation Analysis Prompt for eval-analyze

You are analyzing a repository's **agentic documentation** (CLAUDE.md, AGENTS.md, ai-docs/, etc.) to generate an evaluation configuration (`eval.yaml`) for testing whether AI agents can effectively navigate and use this documentation.

**What to analyze**: Agentic documentation written FOR agents (not manual user docs like README.md or end-user guides).

**Context**: This prompt is invoked via `/eval-analyze --prompt examples/openshift-agentic-docs.md`. It generates a prompt-mode eval config (not a skill eval).

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
- Examples: openshift/machine-config-operator, Operator repos

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

Map out the documentation organization using the ACTUAL paths from Step 1.1's find command output. Entry point is at repository root (CLAUDE.md or AGENTS.md), NOT in subdirectories.

```yaml
documentation_structure:
  entry_point: CLAUDE.md  # or AGENTS.md (whichever exists at root)
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
  - component-daemon
  - component-controller
  - component-server
```

---

## Step 4: Suggest Test Categories

List available templates:
```bash
python3 ${CLAUDE_SKILL_DIR}/../eval-dataset/scripts/list_templates.py
```

Based on repository type and content, suggest test categories from the available documentation templates:

### For Type A (Enhancement/Design):
```yaml
test_categories:
  - name: navigation
    template: documentation/navigation
    count: 2
    description: Agent finds relevant process documentation
    
  - name: authoring
    template: documentation/authoring
    count: 1
    description: Agent creates content following documented patterns
    
  - name: anti-pattern
    template: documentation/anti-pattern
    count: 3  # One per major constraint
    description: Agent rejects approaches that violate constraints
```

### For Type B (Component/Code):
```yaml
test_categories:
  - name: navigation
    template: documentation/navigation
    count: 2
    description: Agent finds API/component documentation
    
  - name: component-usage
    template: documentation/component-usage
    count: 3  # One per major API
    description: Agent explains how to use APIs with examples
    
  # Optional:
  # - name: architecture
  #   template: documentation/architecture
  #   count: 1
  #   description: Agent explains how components work together
```

### For Type C (General Docs):
```yaml
test_categories:
  - name: navigation
    template: documentation/navigation
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
  - annotations.yaml: with 'expected_files' field (list of doc paths for consulted_docs judge)
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

## Step 6: Select Judges

List available builtin judges with:
```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/list_builtins.py
```

**Pattern for documentation navigation**:

1. **Mechanical check** - Use `builtin: consulted_docs` to verify agent read expected docs:
   ```yaml
   - name: docs_consultation
     builtin: consulted_docs
     description: Verifies agent read the expected documentation files
     # No arguments needed - reads from annotations.expected_files in each case
   ```
   This judge extracts Read tool calls from events.json and checks coverage against `annotations.expected_files`.

2. **Semantic check** - Use LLM judges to verify agent *navigated* (vs. answered from cache):
   ```yaml
   - name: navigation_success
     prompt: |
       Expected files: {{ annotations.expected_files }}
       Did the agent find and read the correct docs, or answer from memory?
       {{ conversation }}
     if: "annotations.get('category') == 'navigation'"
   ```

**Important**: Both judges should reference `annotations.expected_files` (not `expected_paths` or other variants). This is the standard field name for expected documentation paths.

For category-specific judges, add `if:` conditions (YAML-level condition where `annotations` is implicit — NOT for use inside `check:` code blocks where you must use `outputs.get("annotations", {})`).

See `${CLAUDE_SKILL_DIR}/references/eval-yaml-template.md` for judge patterns and correct variable scoping.

---

## Step 7: Generate eval.yaml

Read the complete template structure at `${CLAUDE_SKILL_DIR}/references/eval-yaml-template.md`.

**Documentation eval specifics** (combine with the template):

1. **Execution**: Use `execution.prompt` (not `execution.skill`) with doc-reading requirements

2. **Runner**: 
   ```yaml
   runner:
     type: claude-code
     workspace_mode: repo      # Required for documentation navigation testing
     effort: medium
   ```
   **Why `workspace_mode: repo` for documentation evals**: This evaluation tests whether agents can navigate and use repository documentation (ai-docs/, CLAUDE.md, docs/). Agents need access to the full directory tree at their real paths. The default isolated workspace only provides input.yaml + symlinks to root-level files, which doesn't test documentation structure navigation. Set `workspace_mode: repo` when testing in-repo navigation or documentation; omit it for isolated capability tests.

3. **Permissions**: Add `deny` rules for `eval/`, `eval.yaml`, `eval.md`, `tmp/` paths
   (These prevent test cheating by blocking answer key access. Only applicable when workspace_mode: repo is set.)

4. **Dataset**: Include `test_categories` and `domain` sections from Steps 3-4

5. **Judges**: Use category-specific `if:` conditions and builtin judges where available

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
   /eval-run --model {model-name} --config eval.yaml
   ```
```

---

## Important Notes

- **Use actual paths**: Don't use placeholders. Every field name, file pattern, and directory path must come from reading actual files.
- **Realistic test counts**: Suggest 2-3 navigation, 1-3 per API, 1 per constraint.
- **Category-specific judges**: Most judges (except universal ones like has-documentation-section) should include an `if:` field to specify which test categories they evaluate. Example: `if: "annotations.get('category') == 'navigation'"`.

**Examples are illustrative**: The examples above use generic component names. Adapt to the actual domain you discover in the repository.
