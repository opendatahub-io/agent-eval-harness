---
name: eval-analyze
description: Analyze a skill and generate eval.yaml for the agent eval harness. Deeply examines the skill's SKILL.md, sub-skills, scripts, and test cases to produce the full evaluation config — execution mode, dataset schema, output descriptions, judges, models, and thresholds. Use this skill whenever someone wants to set up evaluation, test a skill, add quality checks, benchmark a skill, or just created a new skill and needs eval infrastructure. Also triggered automatically by /eval-run when eval.yaml is missing. Even if the user just says "how do I know if my skill is working?" — this is the right starting point.
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, AskUserQuestion
---

You analyze a target skill and produce `eval.yaml` — the configuration that `/eval-run` needs. You read the skill deeply (including sub-skills it invokes), explore existing test cases, and generate everything: dataset schema, output descriptions, judges, and thresholds.

The core principle: **observe, don't assume**. Every field name, file pattern, and directory path in the generated eval.yaml must come from reading actual files. If you can't point to a specific file or field you observed, don't put it in the config.

## Step 0: Parse Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--skill <name>` | no | auto-detect | Which skill to analyze |
| `--config <path>` | no | `eval.yaml` | Output path for the config |
| `--update` | no | false | Fill in missing sections only, preserve user edits |

```bash
mkdir -p tmp
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py init tmp/analyze-config.yaml \
  skill=<skill> config=<config> update=<true/false>
```

## Step 1: Find the Target Skill

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

## Step 2: Check If Analysis Is Needed

If eval.yaml already exists and `--update` was not set:

```bash
test -f <config> && echo "CONFIG_EXISTS" || echo "NO_CONFIG"
```

If it exists, validate it:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py config <config>
```

Then check if eval.md (the cached analysis) is still fresh — meaning the SKILL.md hasn't changed since the last analysis:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py memory eval.md
```

If FRESH and eval.yaml has a non-empty `dataset.schema`, at least one `outputs` entry with a schema, at least one judge, and `models.skill` set, report that config is up to date and exit. No work needed. (An INCOMPLETE config — empty sections, or missing `models.skill` from a pre-restructure eval.yaml — still needs analysis.)

If STALE, NO_CONFIG, or `--update` was set, proceed to full analysis.

## Step 3: Deep-Read the Skill

This is the most important step — the quality of everything downstream depends on how thoroughly you understand the skill.

Launch an Explore agent to do the analysis:

1. Read `${CLAUDE_SKILL_DIR}/prompts/analyze-skill.md` to get the analysis instructions
2. Use the Agent tool with `subagent_type="Explore"`
3. Pass as prompt: the contents of analyze-skill.md, with the actual skill path prepended (e.g., "Analyze the skill at .claude/skills/my-skill/SKILL.md. <rest of analyze-skill.md>")

The analysis is **recursive** — the agent follows sub-skill chains (Skill tool calls, `/skill-name` references) until it finds the skills that produce the final artifacts (typically 2-5 levels, capped at 5 to avoid circular references), reading each sub-skill's SKILL.md to trace the full pipeline. The outputs section must describe what the entire pipeline produces, not just the top-level orchestrator.

The agent returns structured YAML with: purpose, inputs, outputs, sub_skills, flags, pipeline, quality_criteria, and suggested_judges. See `${CLAUDE_SKILL_DIR}/prompts/analyze-skill.md` for the full schema.

**Verify the response**: check that outputs reference actual directories and file patterns (not placeholders like `<output-dir>`), that sub_skills lists real skill names, and that suggested_judges include working code snippets. If anything looks fabricated, ask the agent to re-examine specific files.

## Step 4: Explore the Dataset

First check if eval.yaml already has a `dataset.path` (from a previous run or `--update`):

```bash
ls <dataset_path>/ 2>/dev/null | head -20
```

If not set or doesn't exist, search the project for test case directories using the Glob tool:

```
Glob: **/cases/ or **/test-cases/ or **/fixtures/ or **/examples/
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

## Step 5: Generate eval.yaml

Combine the skill analysis (Step 3) and dataset exploration (Step 4) into a complete eval.yaml. Read the full template and writing guidance at `${CLAUDE_SKILL_DIR}/references/eval-yaml-template.md`.

Key points:
- **Execution mode**: determine from the skill analysis whether it expects a single input (`execution.mode: case`) or a batch file (`execution.mode: batch`). Look at `$ARGUMENTS` in the SKILL.md — if it takes one value (a key, prompt, or file path), use `case`. If it takes `--input <file>` with a YAML list, or has parallelism/batch-size controls, use `batch`. When in doubt, use `case`.
- **Arguments template**: under `execution.arguments`. For `case` mode, build a template with `{field}` placeholders matching the input.yaml fields you observed in Step 4 (e.g., `"{strat_key} {adr_file?}"`). For `batch` mode, use the literal arguments string (e.g., `"--input batch.yaml --headless"`).
- **Runner**: `runner.type: claude-code` is the default and almost always correct. Only change it if the user has explicitly mentioned another harness.
- **Models**: set `models.skill` to a sensible default (e.g., `claude-opus-4-7`) so the user doesn't need `--model` on every invocation. Set `models.judge` to the same or a comparable model — LLM and pairwise judges read it. CLI flags override.
- **MLflow**: set `mlflow.experiment` to `<project>-eval` (or leave blank — it falls back to the top-level `name`).
- The `dataset.schema` and `outputs[*].schema` fields drive the entire pipeline — be specific, reference actual file/field names you observed
- **Permissions**: if the skill's `allowed-tools` frontmatter includes `Skill` (meaning it invokes sub-skills), add `"Skill"` to `permissions.allow`. The Skill tool requires explicit permission in headless mode — without it, nested skill calls fail silently and the pipeline degrades.
- If the skill uses AskUserQuestion, calls external services (MCP tools), or runs scripts that interact with APIs, add `inputs.tools` entries. Use `match` to describe what to intercept in natural language (e.g., "any Jira interaction via MCP or scripts"), and `prompt` for how to handle it.
- Aim for 2-4 inline `check` judges + 1-2 LLM `prompt` judges. Start lean.
- If `--update`: preserve everything already in the file, only add missing top-level keys (e.g., add a `models:` block if the user is upgrading from an older config that lacked it)

## Step 5b: Validate Generated Config

After writing eval.yaml, validate that all references are correct:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py config <config>
```

This checks dataset path exists, output paths are relative, judge prompt_file/context/module references resolve, and runner.settings exists.

**Errors** (exit code 1): fix before proceeding — broken file references, absolute paths, missing modules.

**Warnings** (exit code 0): may be expected — empty dataset (user hasn't created cases yet), missing judges (will be added later). Report them to the user but don't block.

## Step 6: Generate eval.md

The eval.md caches the skill analysis so it doesn't need to be repeated. The hash tracks only the top-level SKILL.md — if sub-skills change, the user should run `/eval-analyze --update` to refresh. Compute the skill hash:

```bash
python3 -c "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('<skill-path>/SKILL.md').read_bytes()).hexdigest()[:12])"
```

Read the template at `${CLAUDE_SKILL_DIR}/prompts/generate-eval-md.md`. Write eval.md with YAML frontmatter (skill, analyzed_at, skill_hash) and a markdown narrative of the analysis.

## Step 7: Report

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
