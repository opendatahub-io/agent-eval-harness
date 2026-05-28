---
name: eval-dataset
description: Generate evaluation test cases for a skill. Creates realistic test inputs based on skill analysis, bootstraps a starter dataset, or expands an existing one to improve coverage. Use when setting up evaluation for the first time, when the user needs test cases, when coverage is too thin, or after /eval-analyze when no dataset exists yet. Triggers on "create test cases", "generate test data", "need test inputs", "make a dataset", "add more cases", "improve coverage". Also useful when /eval-run reports "no test cases found."
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, AskUserQuestion
---

You generate evaluation test cases for a skill. You read the skill analysis (eval.md) and eval config (eval.yaml) to understand what the skill does, then create realistic test cases that match the dataset schema. The goal is giving `/eval-run` something meaningful to test against.

## Step 0: Parse Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--config <path>` | no | `eval.yaml` | Path to eval config |
| `--count <N>` | no | 5 | Number of cases to generate |
| `--strategy <type>` | no | `bootstrap` | Generation strategy (see Step 3) |

## Step 1: Read Context

Read eval.yaml and eval.md to understand:
- **The skill** — what it does, what inputs it expects, what it produces
- **The execution config** — `execution.mode` (`case` or `batch`) and `execution.arguments` (the argument template). In `case` mode, `{field}` placeholders in the arguments are resolved per case from input.yaml — every field referenced in the template (e.g., `{strat_key}`, `{prompt}`) must exist in the generated input.yaml files.
- **The dataset schema** — `dataset.schema` describes the case structure (files, fields, formats)
- **The dataset path** — where cases should be created
- **The output schema** — `outputs[*].schema` describes what the skill produces (informs what reference outputs look like)
- **The judges** — extract the evaluation criteria from each judge:
  - `check` snippets reveal exact validation logic — what fields are accessed, what thresholds are used, what conditions trigger pass/fail
  - `prompt` / `prompt_file` text describes quality dimensions (completeness, accuracy, etc.)
  - `description` summarizes what each judge evaluates

Build a list of **judge-driven requirements** — these are the concrete things judges will check. Each test case should be designed to exercise at least one of these requirements. For example:
- A judge checking `len(content) >= 100` → include a case with minimal input that might produce short output
- A judge comparing against a reference → include a case where the correct answer is unambiguous
- A judge checking tool calls → include a case where the skill should (or shouldn't) invoke external tools
- A cost/efficiency judge → include a case with large input that tests scaling

If eval.yaml doesn't exist, ask the user which skill to evaluate, then invoke `/eval-analyze` to create the config:

```text
Use the Skill tool to invoke /eval-analyze --skill <skill-name>
```

Wait for the analysis to complete, then re-read eval.yaml. If /eval-analyze fails or the user skips it, you cannot generate meaningful cases — stop and explain why.

If eval.md doesn't exist, you can still work from eval.yaml's schema descriptions, but the cases will be less targeted.

## Step 2: Parse Schema into Generation Template

Read `dataset.schema` and extract a concrete checklist:

1. **Required files** — what files each case directory must contain (e.g., `input.yaml`, `reference.md`)
2. **Required fields per file** — for structured files like YAML/JSON, which fields are mandatory
3. **Optional fields** — fields described with "optionally" or "if available" — vary these across cases (include in some, omit in others) to test the skill's handling of missing optional context
4. **Field semantics** — what kind of content each field expects (e.g., "problem statement", "clarifying context", "priority level"). Use these descriptions to generate realistic content, not generic placeholders
5. **Naming patterns** — any file naming conventions mentioned (e.g., "named NNN-slug.md")

6. **Argument fields** — if `execution.mode` is `case`, parse `execution.arguments` for `{field}` placeholders. Every placeholder must appear as a required field in input.yaml. Cross-check against items 1-2 above — if `{strat_key}` is in the arguments but not in the schema, add it as a required field.

7. **External-state fields** — look for fields marked with `[EXTERNAL: System]` in the schema description. These reference real resources in external systems (Jira projects, GitHub repos, API endpoints) that must exist at execution time. Do NOT invent values for these fields — fabricated values (e.g., a Jira project key derived from the repo directory name) cause silent failures when the skill queries the external system and gets zero results. Mark these in your generation template as requiring `TODO_` placeholder values (see Step 5).

This checklist is your generation template. Every case must satisfy items 1-2 and 6. Items 3-4 guide content variety.

## Step 3: Assess Current State

Check what already exists:

```bash
ls <dataset_path>/ 2>/dev/null | head -20
```

Count existing cases and read one to understand the current structure. Note:
- How many cases exist
- What topics/scenarios they cover
- Any obvious gaps (only simple cases? no edge cases? no error scenarios?)

## Step 4: Choose Strategy

**`bootstrap`** (default) — Generate N cases from scratch. Use this when starting from zero or when fewer than 5 cases exist.

Design cases to cover:
- **1 simple case** — straightforward input, expected to pass all judges easily
- **1 complex case** — longer input, multiple requirements, tests the skill's full capability
- **1 edge case** — unusual input that tests boundaries (very short, very long, ambiguous, missing fields)
- **Remaining cases** — map to the judge-driven requirements from Step 1. Each remaining case should target a specific judge criterion that the first three cases don't already stress. If there are more judge criteria than remaining case slots, prioritize the strictest judges (those with high thresholds or binary pass/fail).

**`expand`** — Read existing cases, identify gaps, generate cases that fill them. Use this when cases exist but coverage is thin.

Read each existing case's input file to understand what's already covered. Then look for gaps by comparing against:
- The skill's documented capabilities (from eval.md)
- The judges' criteria (from eval.yaml — what do judges check that no case tests?)
- Edge cases mentioned in the skill analysis
- Input variety (all cases similar? need different lengths, complexities, topics)

Avoid duplicating existing scenarios — each new case should test something distinct that isn't already covered. Number new cases continuing from the highest existing case number.

**`from-traces`** — Extract real inputs from MLflow traces and turn them into test cases. Use this when the skill has been used in production and traces are available.

Run the extraction script:

```bash
python3 ${CLAUDE_SKILL_DIR}/../eval-mlflow/scripts/from_traces.py \
  --config <config> \
  --count <N>
```

This outputs YAML with extracted trace inputs (prompt text, tool interactions). Read the output and create case directories following the generation template from Step 2. The trace inputs give you realistic content for the input fields — but you still need to structure the files according to `dataset.schema`.

If the script exits with code 2 (no traces found) or MLflow is not configured, tell the user and fall back to `expand` strategy.

## Step 5: Generate Cases

For each case, create a directory under `dataset.path` following the structure described in `dataset.schema`.

**Naming**: Use descriptive directory names that indicate what the case tests:
```
case-001-simple-basic-input/
case-002-complex-multi-requirement/
case-003-edge-empty-context/
case-004-long-detailed-input/
case-005-ambiguous-phrasing/
```

**Content**: Use the generation template from Step 2. Every case must include all required files and fields. Vary optional fields across cases — include them in some, omit in others. Use the field semantics to generate realistic content appropriate to each field's purpose.

**Realism**: Cases should look like something a real user would encounter. Don't generate lorem ipsum or obviously synthetic inputs. Use realistic names, scenarios, and domain language appropriate to the skill.

**External-state placeholders**: For fields marked `[EXTERNAL: System]` in the schema, use `TODO_<SYSTEM>_<FIELD>` as the value (e.g., `project_key: "TODO_JIRA_PROJECT_KEY"`). If you want to show a plausible real value, put it in a YAML comment (e.g., `# replace with real key, such as MYPROJECT`). The `TODO_` prefix signals that this must be replaced with a real value from the target system before execution. List all placeholders in Step 7 so the user knows what needs manual review.

**Answers for interactive skills**: If eval.yaml has `inputs.tools` entries for AskUserQuestion, the skill asks questions during execution. The hook uses LLM-based answering (via `models.hook`) that reads `input.yaml` and `answers.yaml` from each case as context. Create `answers.yaml` with **guidance** that tells the LLM how to answer domain-specific questions for this case:

```yaml
# answers.yaml — LLM answerer guidance for this case
dedup_is_duplicate: true
dedup_guidance: >
  This RFE is intentionally a rephrased version of an existing RFE
  about model signature verification. If asked whether existing RFEs
  cover this need, the answer is yes.
```

The LLM reads these fields alongside the question and options to pick the right answer. For general clarifying questions, the LLM uses `input.yaml` context — no `answers.yaml` needed. Only create `answers.yaml` when the case has domain-specific decisions (e.g., "is this a duplicate?", "should this be split?") where the correct answer depends on the test scenario.

If unsure what questions the skill asks, you can leave `answers.yaml` out — the hook still calls the LLM using `input.yaml` context and the handler prompt, falling back to the first option only if the LLM call fails.

**Annotations for outcome-aware judges**: Judges receive `outputs["annotations"]` — the parsed `annotations.yaml` from each case. If the eval config has judges that check expected outcomes (e.g., `annotations.get("dedup_is_duplicate")` to determine whether no output is correct), add the relevant fields to each case's `annotations.yaml`:

```yaml
# annotations.yaml — fields for outcome-aware judges
dedup_is_duplicate: true   # or false — tells judges whether no RFE is expected
tags: [dedup, high-overlap]
known_issues:
  - dedup should flag this as overlapping with RHAIRFE-1001
```

Check the eval.yaml `judges` section for any `check` snippets that access `outputs.get("annotations", {})` — those fields must exist in annotations.yaml for the judge to work correctly.

**Companion files**: If eval.md lists `companion_files` (files the skill reads from disk at runtime — e.g., `strategy.md`, `adr.md`), each test case must include them. In `case` mode, the harness copies all case files into the workspace, so the skill will find them at their expected relative paths. Generate realistic content for these files appropriate to each case's scenario.

**Reference outputs**: Only include gold standard reference files if you can confidently produce a correct output. It's better to leave references out (the user can generate them later with `/eval-run --gold`) than to include incorrect ones that mislead judges.

## Step 6: Validate

After generating, verify the cases:

1. Read one generated case back and check it matches the schema
2. Count files per case — do they match what `dataset.schema` describes?
3. If `execution.mode` is `case`, verify that input.yaml contains all fields referenced by `{field}` placeholders in `execution.arguments`
4. If companion files are expected, verify they exist in each case directory
5. Check for obvious issues (empty files, placeholder text, wrong field names)

```bash
ls <dataset_path>/case-001-*/ 
```

## Step 7: Report

Tell the user what was created:

- **Cases generated**: N new cases at `<path>`
- **Strategy used**: bootstrap / expand / from-traces
- **Coverage**: What scenarios are now covered (simple, complex, edge cases)
- **What's missing**: Reference outputs (if not generated), any gaps still remaining
- **External-state placeholders**: If any `TODO_` placeholder values were generated, list each one with which case it's in, which external system it references, and what kind of value is needed (e.g., "case-001/input.yaml `TODO_JIRA_PROJECT_KEY` — needs a real Jira project key from your test instance"). These MUST be replaced with real values before running `/eval-run`.
- **Next steps** (include `--config <config>` if a non-default config was used):
  - `/eval-run --model <model>` to test the skill against these cases
  - `/eval-run --model <model> --gold` to generate gold references from the best outputs
  - `/eval-dataset --strategy expand --count 10` to add more cases later

## Rules

- **Match the schema exactly** — if `dataset.schema` says "input.yaml with a 'prompt' field", create input.yaml with a prompt field. Not input.json, not query.yaml.
- **Realistic over synthetic** — cases should feel like real usage, not test scaffolding
- **Cover the skill's range** — don't just generate 5 variations of the same simple input. Test different capabilities the skill claims to have.
- **Don't fabricate gold outputs** — if you're not confident in what a correct output looks like, leave the reference out. Wrong references are worse than no references.
- **Name cases descriptively** — `case-003-edge-empty-context` is better than `case-003`. The name should indicate what scenario is being tested.
- **Start small** — 5 well-designed cases beat 50 random ones. Quality over quantity, especially for the first dataset.

$ARGUMENTS
