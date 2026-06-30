---
name: eval-analyze
description: Generate eval.yaml for the agent eval harness. Two modes - (1) Skill-based - examines SKILL.md, sub-skills, scripts, test cases to verify implementation quality, OR (2) Prompt-based - tests agent capabilities using custom analysis prompts (documentation effectiveness, pattern understanding, API usage, constraint compliance). Produces complete config with execution mode, dataset schema, outputs, judges, models, thresholds. Use when setting up evaluation, testing skills/documentation, adding quality checks, or benchmarking. Auto-triggered by /eval-run when eval.yaml missing. Triggered by "how do I know if my skill/docs work?"
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, AskUserQuestion
---

You generate `eval.yaml` — the configuration that `/eval-run` needs. You either:

1. **Analyze a skill** (default): Read the skill deeply (including sub-skills), explore test cases, generate config for testing the skill
2. **Custom analysis** (`--prompt`): Execute a custom analysis prompt that defines what to evaluate and how

The core principle: **observe, don't assume**. Every field name, file pattern, and directory path in the generated eval.yaml must come from reading actual files. If you can't point to a specific file or field you observed, don't put it in the config.

## Step 0: Parse Arguments and Discover Layout

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--skill <name>` | no | auto-detect | Which skill to analyze |
| `--config <path>` | no | auto-discover | Output path for the config |
| `--prompt <path>` | no | none | Custom analysis prompt (for non-skill evals) |
| `--update` | no | false | Fill in missing sections only, preserve user edits |

```bash
mkdir -p tmp
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py init tmp/analyze-config.yaml \
  skill=<skill> prompt=<prompt> config=<config> update=<true/false>
```

### Config Location Discovery

If `--config` provided, use that path. Otherwise run `python3 ${CLAUDE_SKILL_DIR}/../../scripts/discover.py` and decide:
- **No configs**: Create `eval.yaml` at project root
- **One root config** and `--skill` targets a different eval than the existing one: offer to reorganize into `eval/` layout. If the user accepts, run the reorganization script (see Phase 7). If declined, ask where to put the new config.
-- **Nested/flat layout already exists**: place the new config at `eval/<skill-name>/eval.yaml` (nested) or alongside existing flat configs
-- **`--config` provided**: use the explicit path, bypass layout logic

Set the resolved config path as `<config>` for all subsequent steps. Set `<eval_md_path>` to the same directory as `<config>`, with filename `eval.md`.

**Modes**: 
- `--skill my-skill` → skill-based eval (`execution.skill`, case/batch mode)
- `--prompt examples/openshift-agentic-docs.md` → prompt-based eval (`execution.prompt`, case mode, tests agent capabilities)
- See `examples/` for domain-specific analysis prompt recipes

## Step 1: Determine Analysis Mode

**If `--skill` provided**: Go to Step 2 (skill analysis)  
**If `--prompt` provided**: Go to Step 2-Prompt (custom prompt analysis)  
**If neither**: Detect what exists via `python3 ${CLAUDE_SKILL_DIR}/scripts/find_skills.py` and checking for CLAUDE.md/AGENTS.md/ai-docs/, then:
- **Both skills and agentic documentation**: Ask user which mode (skill-based vs prompt-based eval)
- **Only skills**: Auto-select skill mode → Step 2
- **Only agentic documentation**: Ask user to provide analysis prompt (see examples/ for recipes)
- **Neither**: Error - no evaluable content found

---

## SKILL ANALYSIS (Default)

### Step 2: Find the Target Skill

If `--skill` was provided, locate its SKILL.md:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/find_skills.py --name <skill>
```

If not provided, list all project skills:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/find_skills.py
```

This reads `.claude-plugin/plugin.json` for custom skill paths, falls back to `.claude/skills/` and `skills/`, and excludes eval harness skills. If only one skill is found, use it automatically. If multiple, ask the user which to analyze. If none are found, tell the user — they may need to check their skill directory paths or create a skill first.

**If `--update` and eval.yaml already has a `skill` field**: use that skill. If `--skill` is also provided and differs, ask the user which they mean — don't silently overwrite.

## Step 3: Check If Analysis Is Needed

If the resolved `<config>` already exists and `--update` was not set:

```bash
test -f <config> && echo "CONFIG_EXISTS" || echo "NO_CONFIG"
```

If it exists, validate it:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py config <config>
```

Then check if eval.md (the cached analysis) is still fresh — meaning the SKILL.md hasn't changed since the last analysis:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py memory <eval_md_path>
```

If FRESH and eval.yaml has a non-empty `dataset.schema`, at least one `outputs` entry with a schema, at least one judge, and `models.skill` set, report that config is up to date and exit. No work needed. (An INCOMPLETE config — empty sections, or missing `models.skill` from a pre-restructure eval.yaml — still needs analysis.)

If STALE, NO_CONFIG, or `--update` was set, proceed to full analysis.

## Step 4: Deep-Read the Skill

This is the most important step — the quality of everything downstream depends on how thoroughly you understand the skill.

Launch an Explore agent to do the analysis:

1. Read `${CLAUDE_SKILL_DIR}/prompts/analyze-skill.md` to get the analysis instructions
2. Use the Agent tool with `subagent_type="Explore"`
3. Pass as prompt: the contents of analyze-skill.md, with the actual skill path prepended (e.g., "Analyze the skill at .claude/skills/my-skill/SKILL.md. <rest of analyze-skill.md>")

The analysis is **recursive** — the agent follows sub-skill chains (Skill tool calls, `/skill-name` references) until it finds the skills that produce the final artifacts (typically 2-5 levels, capped at 5 to avoid circular references), reading each sub-skill's SKILL.md to trace the full pipeline. The outputs section must describe what the entire pipeline produces, not just the top-level orchestrator.

The agent returns structured YAML with: purpose, inputs, outputs, sub_skills, flags, pipeline, quality_criteria, and suggested_judges. See `${CLAUDE_SKILL_DIR}/prompts/analyze-skill.md` for the full schema.

**Verify the response**: check that outputs reference actual directories and file patterns (not placeholders like `<output-dir>`), that sub_skills lists real skill names, and that suggested_judges include working code snippets. If anything looks fabricated, ask the agent to re-examine specific files.

## Step 5: Explore the Dataset

First check if eval.yaml already has a `dataset.path` (from a previous run or `--update`):

```bash
ls <dataset_path>/ 2>/dev/null | head -20
```

If not set or doesn't exist, search the project (relative to `<config>` directory) for test case directories using the Glob tool:

```
Glob: **/cases/ or **/test-cases/ or **/fixtures/ or **/examples/ or **/dataset/ or **/eval/ or **/tests/data/
```

Exclude `.venv/`, `.git/`, `node_modules/` from results.

If nothing found, ask the user where their test cases are (or will be).

If a cases directory exists, read **one complete sample case** — every file in it. Note:
- File names and formats (YAML, JSON, markdown, etc.)
- Field names and their purposes
- Which files are inputs vs references/gold standards
- Any metadata or annotations

This is what you'll describe in `dataset.schema`. If you didn't read the actual files, your schema description will be wrong — and downstream judges will fail because they expect fields that don't exist.

If no test cases exist, note this clearly and suggest running `/eval-dataset` to generate them. Describe the expected case structure in `dataset.schema` anyway — eval-dataset uses that description to create matching cases.

## Step 6: Generate eval.yaml

Combine the skill analysis (Step 4) and dataset exploration (Step 5) into a complete eval.yaml. Read the full template and writing guidance at `${CLAUDE_SKILL_DIR}/references/eval-yaml-template.md`.

Key points:
- **Execution mode**: use the `execution.mode` from the skill analysis (Step 4). If the analyzer returned `ASK_USER`, ask the user which mode to use — explain what the analyzer observed and let them decide. Do not default to `case` without evidence; a skill that processes collections of items internally (batch-size controls, multi-item iteration, multi-agent fan-out, result aggregation) is `batch` even if it also accepts a single item. See `eval-yaml-template.md` for the full mode selection guidance.
- **Arguments template**: under `execution.arguments`. For `case` mode, build a template with `{field}` placeholders matching the input.yaml fields you observed in Step 5 (e.g., `"{strat_key} {adr_file?}"`). For `batch` mode, use the literal arguments string (e.g., `"--input batch.yaml --headless"`).
- **Runner**: `runner.type: claude-code` is the default and almost always correct. Only change it if the user has explicitly mentioned another harness. **workspace_mode decision**: Set `workspace_mode: repo` ONLY when the evaluation requires agents to navigate the real repository structure (documentation navigation, code exploration). For skill-based evaluations, omit `workspace_mode` entirely — skills run in isolated /tmp workspaces by default. For prompt-based evaluations, decide based on what you're testing: if agents need access to ai-docs/, docs/, pkg/ at their real paths, use `workspace_mode: repo`; otherwise use the default isolated workspace.
- **Models**: set `models.skill` to `claude-opus-4-6` (the default for eval runs). Set `models.judge` to `claude-opus-4-6` — LLM and pairwise judges need a strong model for accurate scoring. If the skill uses AskUserQuestion interactively (not `--headless`), set `models.hook` to `claude-sonnet-4-6` for LLM-based question answering (fast enough for picking options, cheaper than Opus). CLI flags override.
- **MLflow**: set `mlflow.experiment` to `<project>-eval` (or leave blank — it falls back to the top-level `name`).
- The `dataset.schema` and `outputs[*].schema` fields drive the entire pipeline — be specific, reference actual file/field names you observed
- **External-state fields**: if the skill analysis (Step 4) identified input fields that reference external systems (Jira project keys, GitHub repos, API endpoints via MCP tools or env vars like `JIRA_SERVER`), annotate those fields in `dataset.schema` with `[EXTERNAL: System]` markers (e.g., `'project_key' ([EXTERNAL: Jira] — must be a real project key)`). This tells `/eval-dataset` not to fabricate values for these fields. See `eval-yaml-template.md` for the convention.
- **Permissions**: if the skill's `allowed-tools` frontmatter includes `Skill` (meaning it invokes sub-skills), add `"Skill"` to `permissions.allow`. The Skill tool requires explicit permission in headless mode — without it, nested skill calls fail silently and the pipeline degrades. **IMPORTANT**: For skill-based evaluations, do NOT add `deny` rules — the skill runs in an isolated workspace. Deny rules are ONLY for prompt-mode evaluations (where the agent works in-repo and you need to prevent test cheating).
- **Environment variables**: if the skill needs external service credentials (e.g., `JIRA_SERVER` for a jira-emulator, API keys for test instances), add `execution.env` entries. Use `$VAR` syntax for values that should be resolved from the caller's environment (e.g., `$JIRA_TOKEN`), or literal values for test-only endpoints (e.g., `http://localhost:8080`).
- If the skill uses AskUserQuestion, calls external services (MCP tools), or runs scripts that interact with APIs, add `inputs.tools` entries. Use `match` to describe what to intercept in natural language (e.g., "any Jira interaction via MCP or scripts"), and `prompt` for how to handle it. The AskUserQuestion hook uses 3-tier answer resolution: exact match from `case_overrides`, then an LLM call (using `models.hook`) with the case's `input.yaml` and `answers.yaml` as context, then fallback to the first option. If the skill asks domain-specific questions (e.g., "is this a duplicate?"), suggest the user create `answers.yaml` files per case with guidance for the LLM answerer.
- **Annotation-aware judges**: judges receive `outputs["annotations"]` — the parsed `annotations.yaml` from the dataset case. Use this for outcome-aware scoring where the expected result depends on the test case (e.g., `annotations.get("dedup_is_duplicate")` determines whether producing no output is correct).
- **Prefer builtin judges for common patterns** — the harness ships reusable judges in `agent_eval/judges/`. Use `builtin:` instead of writing inline code. Discover available builtins: `python3 ${CLAUDE_SKILL_DIR}/scripts/list_builtins.py`. See the template for examples.
- **Parameterize with `arguments:`** — all judge types support an `arguments:` dict. Use it instead of hardcoding values in check code or prompt text. For inline checks, `arguments` is passed as the second parameter. For LLM prompts, use `{{ arguments.key }}` (Jinja2 rendered).
- Aim for 1-2 `builtin` judges + 2-3 inline `check` judges + 1-2 LLM `prompt` judges. Start lean.
- If `--update`: preserve everything already in the file, only add missing top-level keys (e.g., add a `models:` block if the user is upgrading from an older config that lacked it). Check existing inline check judges — if any use the old `(outputs)` signature (single parameter), update them to `(outputs, arguments)` (the current contract). Also check LLM judge prompts for literal `{{ }}` that isn't a template variable — all prompts are now Jinja2 rendered.

## Step 6b: Validate Generated Config

After writing eval.yaml to the resolved `<config>` path, validate that all references are correct:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py config <config>
```

This checks dataset path exists (resolved relative to the config file's directory), output paths are relative, judge prompt_file/context/module references resolve, and runner.settings exists.

**Errors** (exit code 1): fix before proceeding — broken file references, absolute paths, missing modules.

**Warnings** (exit code 0): may be expected — empty dataset (user hasn't created cases yet), missing judges (will be added later). Report them to the user but don't block.

## Step 7: Generate eval.md

The eval.md caches the skill analysis so it doesn't need to be repeated. Write it to `<eval_md_path>` (same directory as the config file). The hash tracks only the top-level SKILL.md — if sub-skills change, the user should run `/eval-analyze --update` to refresh. Compute the skill hash:

```bash
python3 -c "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('<skill-path>/SKILL.md').read_bytes()).hexdigest()[:12])"
```

Read the template at `${CLAUDE_SKILL_DIR}/prompts/generate-eval-md.md`. Write eval.md with YAML frontmatter (skill, analyzed_at, skill_hash) and a markdown narrative of the analysis.

## Step 8: Report

Tell the user what was generated:

- **eval.yaml**: created/updated — N judges configured, dataset at `<path>` (M cases found)
- **eval.md**: skill analysis cached (hash: `<hash>`)
- **Next steps**:
  - If no test cases found: `/eval-dataset` to generate test cases (required before eval-run)
  - If test cases exist: `/eval-run --model <model>` to execute the evaluation

If validation produced warnings, list them so the user knows what's incomplete.

## Rules

- **Read before you write** — every field name and file pattern in eval.yaml must come from reading actual files, not from templates or assumptions
- **Schema descriptions must be specific** — "input.yaml with a 'prompt' field" is good. "Input files" is useless. If you can't be specific, you didn't read the files.
- **Generate working judges** — inline check scripts must be valid Python. LLM prompts must define what each score level means.
- **Preserve user work** — when updating, diff carefully. User-modified judges, schema descriptions, and thresholds should be kept.
- **Fail loudly** — if the skill analysis is incomplete or the dataset can't be found, say so. Don't generate a config full of placeholders.

$ARGUMENTS

---

## PROMPT-BASED ANALYSIS (Custom)

### Step 2-Prompt: Generate Eval Config from Analysis Prompt

**Objective**: Generate `eval.yaml` using a custom analysis prompt that defines what to evaluate and how.

This is for non-skill evaluations where you want to test agent capabilities directly:
- **Skill eval**: "Does this skill produce the expected outputs?"
- **Prompt eval**: "Can an agent accomplish X given Y context?"

#### Execute Prompt Analysis

1. Resolve prompt: `python3 ${CLAUDE_SKILL_DIR}/scripts/resolve_prompt.py <prompt-ref>`
2. Launch Explore agent with prompt content defining what to analyze, test categories, judges, and traces
3. Extract generated eval.yaml and write to `<config>`
4. Validate: `python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py config <config>` (check execution.prompt set, fields populated, paths resolve)

Report to user:
- **eval.yaml** created from `<prompt-name>` (execution.prompt, case mode)
- **Test structure**: {summary of test_categories or schema}
- **Next**: `/eval-dataset` to generate cases, then `/eval-run --model sonnet`

---

## Mode Comparison

| Aspect | Skill Analysis | Prompt-Based Analysis |
|--------|---------------|----------------------|
| **Command** | `/eval-analyze --skill my-skill` | `/eval-analyze --prompt examples/openshift-agentic-docs.md` |
| **Analyzes** | SKILL.md, scripts, sub-skills | Docs, patterns, APIs (prompt-defined) |
| **Executes** | Skill invocation (`execution.skill`) | Direct prompt (`execution.prompt`) |
| **Mode** | `case` or `batch` | `case` only |
| **Dataset** | Schema-based (input/output fields) | Taxonomy-based (test categories) |
| **Judges** | Output quality checks | Capability checks (rubrics) |
| **Domain config** | Usually empty | Prompt-defined knowledge/constraints |
| **Purpose** | Does my skill work? | Can agents use my docs? |

---

## Examples

```bash
# Skill analysis
/eval-analyze --skill my-skill              # Explicit skill
/eval-analyze                                # Auto-detect single skill

# Prompt-based analysis
/eval-analyze --prompt examples/openshift-agentic-docs.md    # Domain-specific recipe
/eval-analyze --prompt path/to/prompt.md                     # Custom prompt

# Options
/eval-analyze --skill my-skill --update                      # Preserve user edits
/eval-analyze --prompt examples/openshift-agentic-docs.md --config eval/docs.yaml  # Custom output path
```

