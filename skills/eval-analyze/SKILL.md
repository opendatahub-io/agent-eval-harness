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

Combine the skill analysis (Step 4) and dataset exploration (Step 5) into a complete eval.yaml. **Read `${CLAUDE_SKILL_DIR}/references/eval-yaml-template.md` for the full field reference** — it documents every field, the workspace_mode decision guide, permissions/deny rules, the `[EXTERNAL: System]` convention, `inputs.tools`, and the reward schema in depth. The eval-analyze-specific decisions to get right:

- **Execution mode / arguments**: use `execution.mode` from Step 4 (if it returned `ASK_USER`, ask the user — don't default to `case`; a skill that processes collections internally is `batch`). For `case` mode, build `execution.arguments` with `{field}` placeholders matching observed input.yaml fields; for `batch`, the literal string (e.g. `"--input batch.yaml --headless"`).
- **workspace_mode**: omit for skill evals (isolated /tmp). Set `repo` only when the agent must navigate the real tree (doc navigation, code exploration) — and then add `permissions.deny` for `eval/`, `eval.yaml`, `eval.md`, `tmp/` to prevent test-cheating. Deny rules are prompt-mode only.
- **Models**: `models.skill` and `models.judge` → `claude-opus-4-6`; `models.hook` → `claude-sonnet-4-6` if the skill uses AskUserQuestion interactively. CLI flags override.
- **Schemas**: `dataset.schema` and `outputs[*].schema` drive the whole pipeline — be specific, use the real file/field names you observed. Mark inputs that reference external systems with `[EXTERNAL: System]` so `/eval-dataset` won't fabricate them.
- **Permissions**: if the skill's `allowed-tools` includes `Skill`, add `"Skill"` to `permissions.allow` — nested skill calls fail silently in headless without it.
- **Tool interception**: if the skill uses AskUserQuestion or calls external services (MCP tools/scripts), add `inputs.tools` entries (`match` = natural-language description, `prompt` = how to handle). AskUserQuestion answering uses `models.hook` + per-case `answers.yaml`.
- **Judges**: prefer `builtin:` for common patterns (discover: `python3 ${CLAUDE_SKILL_DIR}/scripts/list_builtins.py`); parameterize with `arguments:` instead of hardcoding. Aim for 1-2 builtin + 2-3 inline `check` + 1-2 LLM `prompt`; start lean. Judges receive `outputs["annotations"]` for outcome-aware scoring.
- **Reward (optional)**: if the analyzer suggested one and there are multiple judges, add a `reward:` section so the report and Harbor's `reward.json` match your intent — otherwise a default resolution applies that can silently disagree. Schema in the template.
- **Portability**: keep `dataset.path` / `outputs[*].path` project-relative (absolute paths break under Harbor / EvalHub).
- **`--update`**: preserve the existing file; only add missing top-level keys. Check LLM judge prompts for literal `{{ }}` that isn't a template variable.

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
2. Launch Explore agent with prompt content defining what to analyze, the generation block, judges, and traces
3. Extract generated eval.yaml and write to `<config>`
4. Validate: `python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py config <config>` (check execution.prompt set, fields populated, paths resolve)

**Same config surface as skill mode.** A prompt-mode eval.yaml still needs `models`, `judges`, and
`thresholds` — read `references/eval-yaml-template.md` and Step 6's field guidance (it applies to both
modes) rather than re-deriving it. Two prompt-mode specifics that Step 6 flags but the analysis prompt
may not: set `runner.workspace_mode: repo` when the agent must navigate the real repo (docs/ai-docs
navigation), and add `permissions.deny` for `eval/`, `eval.yaml`, `eval.md`, `tmp/` so the agent
can't read the answer key (test-cheating guard — deny rules are prompt-mode only).

**Prefer builtin generation prompts** — for synthetic-generation datasets, emit a top-level `generation:`
block with `strategy: synthetic`, `context:` (repository knowledge), and `seeds:`. Each seed sets a
`category`, a `count`, and one of `builtin:` / `prompt_file:` / `prompt:`. Prefer `builtin:` for
common patterns (they ship in `agent_eval/prompts/`); discover them with
`python3 ${CLAUDE_SKILL_DIR}/../eval-dataset/scripts/list_prompts.py`. Use `prompt_file:` for
project-specific generation prompts.

Report to user:
- **eval.yaml** created from `<prompt-name>` (execution.prompt, case mode)
- **Test structure**: {summary of generation.seeds or schema}
- **Next**: `/eval-dataset` to generate cases, then `/eval-run --model sonnet`

---

## Mode Comparison

| Aspect | Skill Analysis | Prompt-Based Analysis |
|--------|---------------|----------------------|
| **Command** | `/eval-analyze --skill my-skill` | `/eval-analyze --prompt examples/openshift-agentic-docs.md` |
| **Analyzes** | SKILL.md, scripts, sub-skills | Docs, patterns, APIs (prompt-defined) |
| **Executes** | Skill invocation (`execution.skill`) | Direct prompt (`execution.prompt`) |
| **Mode** | `case` or `batch` | `case` only |
| **Dataset** | Schema-based (input/output fields) | Generated (`generation.strategy`: synthetic \| from-traces) |
| **Judges** | Output quality checks | Capability checks (rubrics) |
| **Generation context** | Usually empty | Prompt-defined knowledge/constraints |
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

