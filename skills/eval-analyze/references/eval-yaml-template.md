# eval.yaml Template

Use this template when generating eval.yaml. Fill in every field from what you observed in the skill analysis and dataset exploration — never use placeholder text.

## Full Structure

```yaml
name: <project-name>
description: <one line: what is being evaluated>

# Execution — how to invoke the eval target (skill or prompt)
#
# MODE (how many invocations):
# - case: one invocation per test case (default)
# - batch: one invocation for all cases via batch.yaml
#
# WHAT TO EXECUTE (mutually exclusive):
# - skill: skill name for '/skill-name' invocations
# - prompt: direct prompt template for prompt-based evaluation
#
# How to choose the mode — look at the skill's INTERNAL LOGIC:
#
# - batch: the skill is designed to process MULTIPLE items per invocation.
#   Look at the skill's pipeline, not just its CLI flags. Signals:
#   - It iterates over a collection of inputs (batch files, ID lists, arrays)
#   - It has batch-size, parallelism, or concurrency controls
#   - It launches multiple agents or sub-skills for different items
#   - It aggregates results across items (summary tables, index files)
#   - Its pipeline phases operate on a SET of items, not one
#   A skill that supports both single and multi-item invocation is batch
#   if its primary design processes collections.
#   Examples: /rfe.speedrun (batch-creates, reviews, submits sets of RFEs),
#             /rfe.auto-fix (processes N IDs with --batch-size)
#
# - case: the skill is fundamentally designed to process ONE input per run.
#   No internal iteration, no batch controls, no multi-item aggregation.
#   Examples: /test-plan.create RHAISTRAT-1520, /rfe.create "problem..."
#
# When in doubt, ask the user — don't silently default to case.
#
# EXAMPLES:
#
# ── Skill mode: case ──
execution:
  mode: case
  skill: rfe.create
  arguments: '--priority {{ input.priority }} "{{ input.prompt }}"'
  # → sends: /rfe.create --priority High "Add signature verification"
  # Uses isolated /tmp workspace (skill + input.yaml + symlinked resources)

# ── Skill mode: batch ──
# execution:
#   mode: batch
#   skill: rfe.speedrun
#   arguments: '--input batch.yaml --headless --dry-run'
#   # → sends: /rfe.speedrun --input batch.yaml --headless --dry-run
#   # Uses isolated /tmp workspace

# ── Prompt mode: case (common for doc testing, agent capability testing) ──
# execution:
#   mode: case
#   prompt: "{{ input.prompt }}"
#   # → sends: Add signature verification
#   # Workspace: DEPENDS on what you're testing (see Runner section below)
#   # Same input.yaml feeds both (prompt, priority); one Jinja2 dialect; mode stays case/batch.

# ── Prompt mode: batch (uncommon, but structurally valid) ──
# execution:
#   mode: batch
#   prompt: "{{ input.prompt }}"
#   # → batch.yaml contains all prompts; agent processes sequentially in one conversation
#   # Workspace: DEPENDS on what you're testing (see Runner section below)

# Additional execution options:
  # timeout: 3600           # Per-invocation wall-clock timeout (seconds)
  # max_budget_usd: 5.0     # Per-invocation cost cap
  # parallelism: 3          # Run up to N cases concurrently (case mode only)
  # env:                    # Inject env vars into workspace .claude/settings.json
  #   JIRA_SERVER: http://localhost:8080   # Literal value
  #   JIRA_TOKEN: $JIRA_TOKEN              # $VAR resolved from caller's environment

# Runner — agent harness + runner-specific knobs
#
# WORKSPACE_MODE determines execution context — choose based on WHAT you're testing:
#
# - (unset/omitted): Isolated /tmp workspace with input.yaml + symlinked resources
#                    DEFAULT and CORRECT for:
#                    • All skill-based evaluations (safe, reproducible, no repo contamination)
#                    • Prompt-based tests that don't need full repo access
#
# - repo: Run agents in actual repository directory with full file tree access
#         USE WHEN TESTING:
#         • Documentation navigation (agents need ai-docs/, docs/ at real paths)
#         • In-repo code understanding (agents need pkg/, cmd/, internal/)
#         • Repository structure/organization (grep across files, find patterns)
#         Requires permissions.deny rules to prevent repo modification
#
# DECISION GUIDE:
# Ask: "Does the agent need to navigate the real repository structure to answer correctly?"
#   YES (doc navigation, code exploration) → workspace_mode: repo
#   NO  (skill testing, isolated capabilities) → omit workspace_mode (use default)
#
runner:
  type: claude-code         # Discriminator: claude-code, opencode, etc.
  # workspace_mode: repo    # Set ONLY when testing requires full repo access (see above)
  # settings: {}            # Runner-specific settings overrides
  # plugin_dirs: []         # Plugin dirs the evaluated skill needs
  # env:                     # Extra env vars for the runner ($VAR resolves from caller)
  #   CUSTOM_AUTH_TOKEN: "$CUSTOM_AUTH_TOKEN"
  # system_prompt: ""       # Appended to harness system prompt
  # effort: high            # Claude Code reasoning effort: low | medium | high | xhigh | max

# Models — defaults for each role (CLI flags override)
models:
  skill: claude-opus-4-6         # Default for eval runs (or pass --model)
  # subagent: claude-sonnet-4-6  # Defaults to skill model
  judge: claude-opus-4-6         # LLM and pairwise judges need a strong model
  # hook: claude-sonnet-4-6      # For AskUserQuestion answering (fast, cheaper than Opus)

# Permissions for headless execution
# The Skill tool requires explicit permission in --print mode.
# If the skill under test invokes sub-skills via the Skill tool
# (check its allowed-tools frontmatter for "Skill"), add "Skill"
# to the allow list — otherwise nested skill calls silently fail.
#
# IMPORTANT: Deny rules are ONLY for PROMPT-MODE evaluations (workspace_mode: repo).
# For SKILL-based evaluations: OMIT deny rules entirely (skill runs in isolated workspace).
# For PROMPT-based evaluations: Add deny blocks to prevent test cheating.
permissions:
  allow: []     # Tool patterns to allow (e.g., "Skill", "Write(artifacts/**)")
  # deny: []    # ONLY for prompt-mode evals - see note above
  
  # Example deny rules for PROMPT-MODE evaluations (workspace_mode: repo):
  # deny:
  #   - path: "eval/"
  #     tools: ["Read", "Grep", "Glob", "Bash"]
  #     reason: "Test cases contain answer keys and run results from other agents"
  #   - path: "eval.yaml"
  #     tools: ["Read", "Grep", "Bash"]
  #     reason: "Eval config contains domain knowledge and expected schemas"
  #   - path: "eval.md"
  #     tools: ["Read", "Grep", "Bash"]
  #     reason: "Analysis cache contains documentation structure maps"
  #   - path: "tmp/"
  #     tools: ["Read", "Grep", "Glob", "Bash"]
  #     reason: "Harness state files not relevant to execution"

# MLflow logging target (optional)
mlflow:
  experiment: <project>-eval
  # tracking_uri: sqlite:///mlflow.db   # Override env var for self-contained runs
  # tags: { team: ml }

# Dataset — describe what you actually observed in the sample case
dataset:
  path: <path to cases directory>
  schema: |
    <natural language description of each case's structure>

# Inputs — tool interception for headless execution
#
# The `match` field is NATURAL LANGUAGE — not a regex or glob. At workspace
# setup time (before execution), eval-run's LLM agent reads these descriptions
# and compiles them into concrete tool patterns, env checks, and input filters
# in tool_handlers.yaml. At runtime, tools.py uses those compiled patterns.
#
# The `prompt` field has two roles:
# - For AskUserQuestion: used at RUNTIME by the hook LLM to pick answers
# - For other tools: used at DESIGN-TIME to generate env_checks/input_filters
#
# AskUserQuestion answering uses 3-tier resolution:
#   1. Exact match from case_overrides (set in tool_handlers.yaml by eval-run)
#   2. LLM call (models.hook) using the handler prompt + case context
#      (input.yaml and answers.yaml from the case directory)
#   3. Fallback: pick the first option or "yes"
#
# For skills with interactive decisions (e.g., duplicate detection
# confirmation), provide per-case answers.yaml files in the dataset
# with guidance the LLM answerer can use.
inputs:
  tools:
    # Auto-answer user questions
    # - match: Questions asked to the user via AskUserQuestion.
    #   prompt: |
    #     Answer based on the test case context in input.yaml and answers.yaml.
    #     Use answers.yaml guidance for domain-specific decisions.
    #     Default: pick the first option or answer "yes" for confirmations.

    # Control external service access (MCP tools AND scripts)
    # - match: |
    #     Any interaction with Jira — whether via MCP tools
    #     or Bash scripts calling the Jira API.
    #   prompt: |
    #     Only allow if targeting a test instance or emulator.

# Outputs — what the skill produces (files on disk or tool calls)
# IMPORTANT: path must be a named subdirectory (e.g., "output", "artifacts").
# Never use "." — the harness cleans output dirs between runs, and "." would
# delete the entire project. For skills that only write to stdout (captured
# via traces.stdout), use "output" as a conventional empty directory.
outputs:
  # File artifacts on disk
  - path: <output directory, e.g. "output" or "artifacts" — never ".">
    schema: |
      <natural language description of artifacts in this directory>
    # batch_pattern: "PREFIX-{n:03d}"
    # Batch collection: maps output files to cases when the skill processes
    # all cases in one invocation. {n} is a 1-based case index expanded to
    # match output file prefixes (e.g., "RFE-{n:03d}" → "RFE-001", "RFE-002").
    # Files whose name starts with the expanded prefix are assigned to that case.
    # Use "*" for shared directories — content is copied to every case.
    # If omitted, the collector auto-detects by common prefix patterns.

  # Tool call outputs (for side effects like API calls)
  # - tool: <tool_name_pattern>
  #   schema: |
  #     <what this tool call represents and what fields matter>

# In-place edits: if the skill modifies input files (e.g., editing source.md
# via the Edit tool), those changes are automatically collected into
# outputs["modified_files"] and outputs["files"]["_modified/filename"].
# No extra config needed — the harness diffs the workspace against its
# initial state after execution. Judges can access modified file content
# directly: outputs.get("modified_files", {}).get("source.md", "")
#
# For skills that ONLY edit in-place (no output directory), you still need
# at least one outputs entry with a path. Use a placeholder directory name
# (e.g., "output") — the directory may be empty, but judges will receive
# modified files via outputs["modified_files"] and {{ outputs }} regardless.

# Traces — execution data to capture for judges
traces:
  stdout: true           # Keep raw stdout.log on disk (debugging)
  stderr: true           # Capture stderr.log
  events: true           # Parse JSONL into events.json (default: true)
  metrics: true          # Capture exit code, tokens, cost, duration

# Judges — evaluate output quality
# Four judge types (determined by which field is set):
#   builtin:     reusable judge from the harness library
#   check:       inline Python snippet (receives outputs, arguments)
#   prompt/prompt_file: LLM judge (Jinja2 rendered)
#   module/function:   external Python module
# All judge types support optional `arguments:` and `if:` (conditional).
#
# CONDITIONAL EXECUTION: Add `if: "expression"` to skip judges on certain cases.
# Available in if expressions: annotations, outputs (direct access, no .get() needed)
#   if: "annotations.get('category') == 'navigation'"
#
# CRITICAL: Inside check blocks, use outputs.get("annotations", {}) — NOT bare annotations
#   check: |
#     cat = outputs.get("annotations", {}).get("category")  # Correct
#     # annotations.get("category")  # WRONG - NameError
judges:
  # Builtin judge: reusable judge from the harness library
  # List available: python3 ${CLAUDE_SKILL_DIR}/scripts/list_builtins.py
  # Each builtin is a .py (Python) or .md (LLM) file in agent_eval/judges/{category}/
  - name: budget_check
    builtin: cost_budget
    arguments:
      max_cost_usd: 5.0

  # - name: safety_check
  #   builtin: no_harmful_content

  # - name: completeness
  #   builtin: output_completeness
  #   model: claude-sonnet-4-6        # optional model override
  #   arguments:
  #     strictness: high              # low, medium, high

  # Inline check: validate structure with code
  - name: <descriptive_name>
    description: |
      <what this judge checks and why it matters>
    check: |
      <python snippet — receives (outputs, arguments) dicts, returns (bool|number, str)>
    # arguments:                      # optional, available as arguments dict in check
    #   max_chars: 10000

  # LLM judge: assess quality with a prompt
  # All LLM prompts are rendered with Jinja2. Available template variables:
  #   {{ outputs }}      — file artifacts and modified files as markdown
  #   {{ conversation }} — root-level assistant text (excludes subagent text)
  #   {{ inputs }}       — the case's input.yaml as **key**: value per field
  #   {{ evidence }}     — summary of tool calls the agent made (turns, cost,
  #                        tools, scripts, files read/written); lazy + cached
  #   {{ annotations }}  — dataset annotations
  #   {{ arguments }}    — judge arguments from eval.yaml (dict)
  - name: <descriptive_name>
    description: |
      <what this judge evaluates>
    prompt: |
      <preamble — what to evaluate>
      {{ outputs }}
      {{ conversation }}
      <scoring criteria — define what each score level means>
    # score_range: [1, 5]             # optional; default LLM assumption is [1, 5].
    #                                 # Set explicitly for rubrics on a different
    #                                 # scale (e.g. [1, 10], [0, 100]). The report
    #                                 # uses it for per-cell color bands; reward
    #                                 # composition still uses reward.score_range.
    # arguments:                      # optional, available as {{ arguments }} in prompt
    #   focus: completeness
    # context:                        # optional supplementary files
    #   - eval/prompts/scoring-rubric.md

  # LLM judge with external prompt file (Jinja2 rendered)
  # - name: <name>
  #   description: <what it checks>
  #   prompt_file: eval/prompts/quality-judge.md
  #   arguments:
  #     strictness: high
  #   context:
  #     - eval/prompts/domain-guidelines.md

  # External code judge (for complex validation)
  # - name: <name>
  #   description: <what it checks>
  #   module: eval.judges.my_checker
  #   function: check_quality
  #   arguments:                      # optional, passed as **kwargs to function
  #     threshold: 0.8

  # Pairwise comparison (used with score.py pairwise --baseline <id>)
  # - name: pairwise
  #   description: Compare two runs and pick the better output
  #   prompt_file: eval/prompts/comparison-judge.md
  #   # model: <model-id>   # Optional override; default is models.judge

# Thresholds for regression detection
thresholds:
  <judge_name>:
    min_pass_rate: 1.0     # for boolean judges (check, builtin)
    # min_mean: 3.5        # for numeric judges (llm)

# Reward composition (OPTIONAL) — collapse per-judge results into a single
# scalar in [0, 1] for RL training (GRPO). Only needed when training; the
# normal /eval-run report path does not require it.
#
# Two mutually exclusive ways to produce the reward:
#
# (a) A single judge whose value IS the reward (e.g. a learned reward model
#     that already emits [0, 1]):
#   reward:
#     judge: my_reward_model   # name of a judge defined above
#     normalize: false         # default: use the value as-is, clamped to [0,1]
#                              # true: map it from score_range to [0,1] instead
#     gate: false              # default false in judge mode
#
# (b) Compose from multiple judges via formula (shown below):
reward:
  # formula selects the mode:
  #   "weighted"      weighted sum of the judges named in `weights`
  #   "<expression>"  a Python expression over judge names as variables,
  #                   e.g. "0.6 * quality + 0.4 * efficiency".
  #                   Allowed calls: min, max, abs, round, sum, len, mean.
  #                   Multi-line is allowed; the last line is the result.
  formula: weighted
  weights:                 # used only by the "weighted" formula
    quality: 0.7
    efficiency: 0.3
  score_range: [1, 5]      # numeric judge range, normalized to [0, 1]
  raw: [efficiency]        # judges already in [0, 1] — skip normalization
  gate: true               # any boolean judge returning false zeros the reward.
                           # Gates on EVERY boolean judge regardless of the
                           # formula — for an expression that uses booleans as
                           # its own gate (e.g. "passed * quality"), set
                           # gate: false to avoid double-gating.
```

Resolution order at scoring time: (1) a `reward:` section if present —
`judge` mode if `judge` is set, otherwise the `formula`/`weights` composition —
else (2) the default: boolean judges gate, numeric judges are normalized and
averaged. `reward.judge` is validated against the defined judges at config load;
syntax- or AST-invalid formulas are also rejected at config load, while
evaluation-time errors (e.g. an undefined name in an expression) warn and
return 0.0.

## Writing Good Schema Descriptions

The `dataset.schema` and `outputs[*].schema` fields are the most important part of eval.yaml. They drive the entire pipeline — agents and judges read them to understand the data.

**Good** — references actual file names, field names, and content:
```
Each case directory contains:
- input.yaml: YAML file with 'prompt' (the problem statement to send
  to the skill) and 'clarifying_context' (additional background).
- reference.md: Gold standard output, a markdown document with
  YAML frontmatter (title, status, priority) and sections for
  Summary, Problem Statement, and Acceptance Criteria.
- annotations.yaml: Expected scores and test metadata.
```

**Bad** — vague, no specific field names:
```
Cases contain input files and reference outputs.
```

The difference: a good schema lets judges write `outputs["main_content"]` knowing what to expect. A bad schema forces them to guess.

### External-State Fields

Some input fields reference resources that must exist in an external system at execution time — Jira project keys, GitHub repo URLs, Slack channel IDs, API endpoints. If eval-dataset doesn't know a field is externally constrained, it will invent plausible but invalid values (e.g., `AGENTREADY` as a Jira project key derived from the repo directory name), causing silent failures at eval-run time.

Mark these fields with `[EXTERNAL: System]` in the schema description:

**Good** — external constraint is explicit:
```text
- input.yaml: YAML file with 'project_key' ([EXTERNAL: Jira] — must be
  a real project key on the target Jira instance, e.g. RHEL or MYPROJECT)
  and 'summary' (free text describing the issue to search for).
```

**Bad** — no indication the value must exist externally:
```text
- input.yaml: YAML file with 'project_key' (Jira project key)
  and 'summary' (issue description).
```

The `[EXTERNAL]` marker tells `/eval-dataset` to generate `TODO_` placeholder values instead of fabricating realistic-looking but invalid data. Users must replace these placeholders with real values before running `/eval-run`.

## Writing Good Judges

**Inline `check` judges** validate structure — things that can be verified deterministically:
- Files exist in the expected directories
- YAML/JSON fields are present and have valid values
- Counts, ranges, and formats are correct

Keep check scripts short (under 15 lines). They receive an `outputs` dict — **always use this dict to access files, never use `os.listdir()` or filesystem paths** (judges run in the project root, not the per-case output directory).

Key fields in `outputs`:
- `outputs["conversation"]` — pre-extracted root-level assistant text (string). Use this for check judges that need to search the skill's conversation output. Equivalent to `{{ conversation }}` for LLM judges.
- `outputs["files"]` — dict of `{relative_path: file_content}`, e.g. `{"artifacts/rfe-tasks/RFE-001.md": "# Summary\n..."}`
- `outputs["modified_files"]` — dict of `{filename: content}` for files modified in-place during execution (e.g., `{"source.md": "edited content..."}`)
- `outputs["events"]` — structured event list (for advanced judges that need tool calls, timestamps, or subagent separation)
- `outputs["case_dir"]` — absolute path to the per-case output directory
- `outputs["exit_code"]`, `outputs["duration_s"]`, `outputs["cost_usd"]`, `outputs["num_turns"]` — execution metadata
- `outputs["tool_calls"]` — list of captured tool calls
- `outputs["stderr"]` — captured stderr log
- `outputs["annotations"]` — parsed `annotations.yaml` from the dataset case directory (always present, empty dict if no file)

Example check judge — search conversation text for expected patterns:
```yaml
  - name: score_present
    check: |
      import re
      text = outputs.get("conversation", "")
      if not text:
          return (False, "No conversation output")
      match = re.search(r'Score[:\s]*(\d+)/15', text)
      if not match:
          return (False, "No score found in output")
      return (True, f"Score: {match.group(1)}/15")
```

Example check judge — find files by path prefix and read their content:
```yaml
  - name: files_exist
    check: |
      files = outputs.get("files", {})
      tasks = [k for k in files if k.startswith("output_dir/") and k.endswith(".md")]
      if not tasks:
          return (False, "No output files found")
      return (True, f"{len(tasks)} files found")

  - name: valid_yaml_header
    check: |
      import yaml
      files = outputs.get("files", {})
      reviews = {k: v for k, v in files.items() if k.endswith("-review.md")}
      for fname, content in reviews.items():
          parts = content.split('---', 2)
          fm = yaml.safe_load(parts[1])
          if 'score' not in fm:
              return (False, f"{fname}: missing score")
      return (True, f"{len(reviews)} reviews valid")
```

Example check judge for in-place edits (skills that edit input files via Edit tool):
```yaml
  - name: source_modified
    check: |
      modified = outputs.get("modified_files", {})
      text = modified.get("source.md", "")
      if not text.strip():
          return (False, "source.md was not modified")
      if len(text.strip()) < 50:
          return (False, f"Modified source.md too short ({len(text.strip())} chars)")
      return (True, f"source.md modified ({len(text)} chars)")
```

**Note on `{{ outputs }}` and modified files**: Modified files appear in `outputs["files"]` with a `_modified/` prefix (e.g., `_modified/source.md`). The `{{ outputs }}` template variable in LLM judge prompts renders ALL entries from `outputs["files"]`, so modified files are automatically included. LLM judges see them as `### _modified/source.md` sections.

**Error handling in check judges:** Use `.get()` with defaults for all dict lookups — if the skill produced no output or failed, keys may be missing. Return `(False, "reason")` for missing data rather than letting exceptions propagate:
```yaml
  - name: has_output
    check: |
      files = outputs.get("files", {})
      if not files:
          return (False, "No output files produced")
      content = list(files.values())[0]
      if len(content.strip()) < 50:
          return (False, f"Output too short ({len(content.strip())} chars)")
      return (True, f"{len(files)} files, {len(content)} chars")
```

**LLM `prompt` judges** assess quality — things that need understanding:
- Completeness: does the output cover all requirements?
- Accuracy: is the content correct?
- Relevance: does it address the input?

**IMPORTANT**: LLM judges only see what's in their prompt text. Use template variables to include skill output:

- `{{ outputs }}` renders all collected file contents (from `outputs[*].path` directories and `_modified/` in-place edits), formatted as markdown sections with file paths as headers.
- `{{ conversation }}` renders root-level assistant conversation text extracted from the event stream. It filters out subagent messages, tool calls, and non-text events. For stdout-only skills (no file artifacts), this is the primary way to give judges the skill's output.
- `{{ inputs }}` renders the case's `input.yaml` as `**key**: value` per field (nested dict/list values go through `yaml.safe_dump`). Handy for judges that need the original request alongside the outputs.
- `{{ evidence }}` renders a compact structured summary of what the agent actually did (turn count, cost, per-tool counts, skills invoked, scripts executed, files read/written). Derived from the parsed event stream and cached, only extracted when the prompt references `{{ evidence }}`. Runner-agnostic — matches tool-name and input-key aliases across Claude Code, opencode, codex, and responses-api.
- `{{ annotations }}` renders dataset annotations from the case's `annotations.yaml`.

Any of these can be used in the same prompt. Without any template variables, the LLM receives only the raw prompt text and cannot see any output.

Example with file artifacts:
```yaml
  - name: output_quality
    prompt: |
      Review the following outputs:

      {{ outputs }}

      Score on a 1-5 scale:
      ...
```

Example for conversation-only skills:
```yaml
  - name: response_quality
    prompt: |
      Evaluate this skill's response:

      {{ conversation }}

      Score on a 1-5 scale:
      ...
```

Example with both file artifacts and conversation output:
```yaml
  - name: comprehensive_quality
    prompt: |
      The skill produced these file artifacts:

      {{ outputs }}

      And this conversation output:

      {{ conversation }}

      Score on a 1-5 scale:
      ...
```

Example grounded in verifiable evidence (use `{{ evidence }}` when the rubric depends on what the agent actually *did*, not what it *said*):
```yaml
  - name: followed_workflow
    prompt: |
      The task was to update `docs/api.md` after reading the current source
      and running the linter.

      ## Case inputs
      {{ inputs }}

      ## What the agent actually did
      {{ evidence }}

      ## What it said
      {{ conversation }}

      Score 1-5 based on both process and outcome:
      - Was `docs/api.md` actually written? (Files written should include it)
      - Was `api.py` read before `docs/api.md` was written? (Files read
        should include the source)
      - Was `./lint.sh` executed? (Scripts executed should include it)
      - Did the agent stay under a reasonable turn budget for this task?

      Do not take the agent's self-report at face value — grade the rubric
      against Files read / Files written / Scripts executed / Total turns.
```
The `evidence` block is a compact structured summary rendered from the parsed event stream (turns, cost, per-tool counts, skills invoked, scripts executed, files read/written). It is extracted lazily — only when the prompt actually references `{{ evidence }}` — and cached, so multiple judges or samples on the same case pay for it once. Runner-agnostic: tool-name and input-key aliases are matched across Claude Code, opencode, codex, and responses-api, so the same prompt works regardless of which runner produced the trace.

Be specific about scoring criteria. "Score 1-5" is too vague. Define what each level means:
```
Score 1: Missing most requirements, major errors
Score 2: Partially addresses the input, significant gaps
Score 3: Covers the basics but lacks depth or has minor errors
Score 4: Good coverage, well-structured, minor issues only
Score 5: Comprehensive, accurate, well-written
```

**Declare the scale when it isn't `[1, 5]`.** LLM judges default to `[1, 5]`, other numeric judges to `[0, 1]`. For a rubric on any other range (e.g. `[1, 10]`, `[0, 100]`), set `score_range: [lo, hi]` on the judge itself — the report uses it to color the per-cell bands proportionally instead of against the default. If you're also composing a reward from these judges, set `reward.score_range` to match (that field governs weighted/formula normalization independently). For `[0, 1]` judges that should NOT be re-normalized by the reward composition (e.g. a builtin like `efficiency/cost_budget`), list them in `reward.raw`.

**How many judges**: aim for 2-4 inline checks + 1-2 LLM judges. Start lean — you can always add more in later iterations. Every judge needs a `description` field explaining what it checks.

**Naming**: use `snake_case` names (e.g., `files_exist`, `output_quality`). These names appear in `thresholds` and in scoring reports — keep them short and descriptive. Make sure threshold keys match judge names exactly.

## The --update Flow

When `--update` is set, preserve everything already in the file. Don't modify existing judges, schemas, thresholds, or permissions. Only add new top-level keys that don't exist yet (e.g., add `outputs` if missing, but don't touch an existing `outputs` section).
