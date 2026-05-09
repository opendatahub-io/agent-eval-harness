You are analyzing a skill to understand what it does, what inputs it expects, and what artifacts it produces. This analysis will be used to generate an eval.yaml configuration.

## What to read

1. **The target SKILL.md** — read it completely
2. **Sub-skills** — if the skill invokes other skills (via the Skill tool or `/skill-name` references), read each sub-skill's SKILL.md recursively. Follow the full chain until you find the skills that actually produce the final output artifacts.
3. **Scripts** — read any Python scripts called via Bash
4. **Prompts and templates** — read any prompt files or templates referenced
5. **Test cases** — if there's a dataset directory, read one sample case to understand the input structure

The recursive part matters: a top-level skill might just orchestrate sub-skills, and the actual outputs (the files judges will score) come from a skill several levels deep. Follow sub-skill chains until you find the skills that produce the final artifacts — this is typically 2-5 levels.

**Cycle and depth protection:** Track visited skill names as you recurse. If you encounter a skill you've already visited, stop — note the cycle in `sub_skills` and move on. Cap at 5 levels regardless. If a referenced sub-skill can't be found (maybe it's in a different plugin or the path is wrong), note it as unresolved in the `sub_skills` list and describe what you can infer from the reference — don't fabricate its contents.

## Report format

Report your findings as structured YAML between ```yaml markers, followed by a narrative explanation.

```yaml
purpose: "<one sentence describing what the full pipeline does>"

inputs:
  description: |
    <natural language description of what the skill expects as input —
     how cases are structured, what files or fields they contain>
  invocation: "<how the skill is invoked: /skill-name args>"
  companion_files:
    - "<files the skill reads from disk at runtime, beyond the prompt —
       e.g., strategy.md, adr.md, config files. These must be in the
       test case directory so the harness can provision them into the
       workspace. List actual filenames you observed in the SKILL.md.>"

outputs:
  # File artifacts written to disk — field names match eval.yaml
  # IMPORTANT: path must be a named subdirectory (e.g., "output", "artifacts"),
  # never "." — the harness cleans output dirs between runs.
  # For stdout-only skills, use "output" as a conventional empty directory.
  - path: "<named subdirectory for outputs, e.g. 'output' or 'artifacts'>"
    schema: |
      <what is produced here — file types, naming patterns, content
       structure. Describe what you actually observed, not generic patterns.
       If the skill writes to stdout rather than files, note that judges
       should read outputs["stdout"] instead of outputs["files"].>
  # Tool call side effects (if the skill calls external APIs)
  # - tool: "<tool name pattern, e.g. mcp__atlassian__create_issue>"
  #   schema: |
  #     <what this tool call does and what fields in its input/output matter>

sub_skills:
  - name: "<sub-skill name>"
    purpose: "<what it does in the pipeline>"
    produces: "<what artifacts it writes, if any>"
  # List all sub-skills in pipeline order

execution:
  # Field names match eval.yaml's execution: block — drop them in directly.
  mode: "<case or batch>"
  arguments: "<template with {field} placeholders from input.yaml>"
  reasoning: |
    <why you chose this mode — what you observed in the skill.
     This is analyzer-only context, not copied into eval.yaml.>
  # mode guidance — read the skill's LOGIC, not just its arguments:
  #
  # batch: the skill is DESIGNED to process multiple items in one invocation.
  #   Look at the skill's internal logic, not just its CLI flags. Signals:
  #   - The skill iterates over a list of inputs (loops, batch files, arrays)
  #   - It has batch-size, parallelism, or concurrency controls
  #   - It launches multiple agents or sub-skills for different items
  #   - It aggregates results across items (summary tables, index files)
  #   - Its pipeline phases operate on a SET of items, not one item
  #   A skill that supports BOTH single-item and batch invocation is still
  #   batch if its primary design processes collections. Don't confuse
  #   "can accept one item" with "is designed for one item at a time."
  #
  # case: the skill is fundamentally designed to process ONE input per run.
  #   It runs a pipeline on a single item and exits. There is no internal
  #   iteration, no batch controls, no multi-item aggregation.
  #
  # When you cannot confidently determine the mode from the skill's logic,
  # set mode to "ASK_USER" and explain what you observed in the reasoning
  # field. The caller will ask the user to decide.
  #
  # arguments examples:
  # For case mode: "{prompt}", "{strat_key} {adr_file?}"
  # For batch mode: "--input batch.yaml --headless --dry-run"

flags:
  supported:
    - "--flag: what it does"
  headless: <true|false>
  dry_run: <true|false>

pipeline:
  - step: "<what happens first — which skill/script runs>"
  - step: "<what happens next>"
  # Trace through sub-skills, not just top-level steps

quality_criteria:
  deterministic:
    - "<things that can be checked with code — file existence, field validation, value ranges>"
  llm_judgment:
    - "<things that need LLM assessment — quality, completeness, accuracy>"

suggested_judges:
  - name: "<judge name>"
    type: "<check|llm>"
    description: |
      <what this judge evaluates and how>
    # For check type, include a working inline script:
    check: |
      <python snippet that takes outputs dict, returns (bool, str)>
    # For llm type, include evaluation instructions.
    # Template variables available in LLM judge prompts:
    #   {{ outputs }} — renders file artifacts and modified files as markdown
    #   {{ conversation }} — renders root-level assistant text (excludes subagent text)
    #   {{ annotations }} — renders dataset annotations
    prompt: |
      <what to evaluate and how to score>
```

## Narrative

After the YAML block, explain:
1. How the pipeline flows end-to-end (across all skill levels)
2. What a "good" output looks like vs a "bad" one
3. Any edge cases or failure modes you noticed
4. What evaluation criteria would be most valuable
5. Which sub-skill's outputs are the ones that matter for scoring
6. Whether the skill edits input files in-place (e.g., uses the Edit tool on `source.md`) rather than writing to an output directory. If so, note this explicitly. The harness automatically collects in-place edits into `outputs["modified_files"]`, so judges should be written against that dict (e.g., `outputs.get("modified_files", {}).get("source.md")`) rather than expecting files in an output directory.
7. Which tools and scripts interact with external services — look for AskUserQuestion (needs auto-answers), MCP tools calling external APIs (Jira, Slack, etc.), AND Python scripts that import API clients or call external URLs. These all need `inputs.tools` entries in eval.yaml so headless eval can intercept them. Also identify which **input fields** reference real external state (e.g., a Jira project key that must exist on the target instance, a GitHub repo URL that must be accessible, a Slack channel ID). These fields cannot be synthesized by eval-dataset — list them so eval-analyze can mark them with `[EXTERNAL: System]` in the dataset schema.

Be thorough but concise. Reference actual file paths and field names you observed — don't invent generic examples.
