---
name: eval-dataset
description: Generate evaluation test cases for an eval.yaml. Sources cases per generation.strategy - skill analysis (default), synthetic LLM generation from generation prompts (documentation and agent-capability evals), or MLflow production traces. Bootstraps a starter dataset or augments an existing one to improve coverage. Use when setting up evaluation, when the user needs test cases, when coverage is too thin, or after /eval-analyze when no dataset exists yet. Triggers on "create test cases", "generate test data", "need test inputs", "make a dataset", "add more cases", "improve coverage", "generate documentation eval cases". Also useful when /eval-run reports "no test cases found."
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, AskUserQuestion
---

You generate evaluation test cases for an `eval.yaml`. Case **provenance** comes from `generation.strategy` (see Step 1.5): the agent authors cases from the skill analysis (`skill`, the default), a script synthesizes them from generation prompts (`synthetic`), or they are extracted from MLflow production traces (`from-traces`). In every case the goal is giving `/eval-run` something meaningful to test against, matching the dataset schema.

## Step 0: Parse Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--config <path>` | no | auto-discover | Path to eval config |
| `--count <N>` | no | 5 | Number of cases to generate |
| `--run-id <id>` | no | — | Prior eval run to learn from when augmenting existing cases |
| `--harbor` | no | — | Also generate Harbor task packages (Step 8) |
| `--image <image>` | with `--harbor` | — | Container image for Harbor task packages |

**Provenance is in the config, not a flag.** `generation.strategy` selects where cases come from:
`skill` (default — agent authors from skill analysis), `synthetic` (LLM generates from
`generation.seeds`), or `from-traces` (extracted from MLflow production traces). There is no
`--strategy` flag: whether to create a fresh set or augment an existing one is derived from the
current dataset state (Step 3), and `--run-id` informs the augment case.

`--count` applies to the `skill` and `from-traces` paths. `synthetic` is fully declarative — case
counts come from each seed's `count` in `generation.seeds`, so `--count` is ignored there; resize a
synthetic dataset by editing seed counts in eval.yaml.

### Config Discovery

If `--config` was explicitly provided, use that path directly. Otherwise, auto-discover:

```bash
python3 ${CLAUDE_SKILL_DIR}/../../scripts/discover.py
```

- **1 config found**: auto-select it as `<config>`
- **Multiple configs found**: present the list and ask the user which eval's dataset to populate
- **No configs found**: suggest running `/eval-analyze` first

## Step 1: Read Context

Read eval.yaml and eval.md to understand:
- **The skill** — what it does, what inputs it expects, what it produces
- **The execution config** — `execution.mode` (`case` or `batch`) and `execution.arguments` (the argument template). In `case` mode, `{field}` placeholders in the arguments are resolved per case from input.yaml — every field referenced in the template (e.g., `{strat_key}`, `{prompt}`) must exist in the generated input.yaml files.
- **The dataset schema** — `dataset.schema` describes the case structure (files, fields, formats)
- **The dataset path** — where cases should be created
- **The output schema** — `outputs[*].schema` describes what the skill produces (informs what reference outputs look like)
- **The judges** — extract the evaluation criteria from each judge. The approach depends on the judge type:
  - `builtin` judges have predefined criteria — list them with `python3 ${CLAUDE_SKILL_DIR}/../eval-analyze/scripts/list_builtins.py`. Use the builtin's known behavior to inform case design (e.g., `cost_budget` → include a large-input case that tests cost scaling; `output_completeness` → include a case with many requirements)
  - `check` snippets reveal exact validation logic — what fields are accessed, what thresholds are used, what conditions trigger pass/fail
  - `prompt` / `prompt_file` text describes quality dimensions (completeness, accuracy, etc.)
  - `description` summarizes what each judge evaluates

Build a list of **judge-driven requirements** — these are the concrete things judges will check. Each test case should be designed to exercise at least one of these requirements. For example:
- A judge checking `len(content) >= 100` → include a case with minimal input that might produce short output
- A judge comparing against a reference → include a case where the correct answer is unambiguous
- A judge checking tool calls → include a case where the skill should (or shouldn't) invoke external tools
- A cost/efficiency judge → include a case with large input that tests scaling

If eval.yaml doesn't exist, determine what to evaluate and invoke `/eval-analyze`:

1. **Check what's available**:
   - Skills in `skills/` directory
   - Agentic documentation (CLAUDE.md, AGENTS.md, ai-docs/)

2. **Ask user which evaluation mode**:
   - If skills exist: suggest `/eval-analyze --skill <skill-name>`
   - Otherwise: suggest `/eval-analyze --prompt <path>` (prompt mode, see examples/)

3. **Invoke /eval-analyze**:
   ```text
   Use the Skill tool to invoke /eval-analyze with the chosen mode
   ```

Wait for the analysis to complete, then re-read eval.yaml. If /eval-analyze fails or the user skips it, you cannot generate meaningful cases — stop and explain why.

If eval.md doesn't exist, you can still work from eval.yaml's schema descriptions, but the cases will be less targeted.

### Assess recommended case count

After reading the skill analysis and judges, estimate whether `--count` is sufficient. Count the skill's distinct execution paths (branches, modes, optional steps), the number of judges, and the number of conditional judges. A rough guideline: you need at least one case per execution path, plus enough variety for each judge to have both passing and failing examples. If the skill has 4 execution paths and 6 judges, 5 cases may be thin — suggest a higher count to the user ("This skill has N distinct paths and M judges — consider `--count 12` for better coverage").

## Step 1.5: Detect Provenance

Read the eval config's `generation.strategy` — the case provenance (use the --config path from Step 0). Absent normalizes to `skill`:

```bash
python3 -c "from pathlib import Path; import yaml; import sys; config = yaml.safe_load(Path(sys.argv[1]).read_text()); print((config.get('generation') or {}).get('strategy') or 'skill')" "<config_path>"
```

Replace `<config_path>` with the actual value from the --config argument (default: eval.yaml). Route on the result:

- **`synthetic`** → a script generates cases directly from `generation.seeds` + `context`. Follow `${CLAUDE_SKILL_DIR}/references/synthetic-generation.md` (then validate per Step 6 and, if `--harbor`, emit packages per Step 8).
- **`skill`** (default) → the agent authors cases from the skill analysis. Continue with Step 2 below.
- **`from-traces`** → the agent shapes cases from real inputs extracted from MLflow traces. Continue with Step 2 below; Step 4 sources the content.

Within the `skill` and `from-traces` paths, whether you **create a fresh set** or **augment an existing one** is not a flag — derive it from the current dataset state in Step 3 (empty/thin → fresh; populated → add gap-fillers). `--run-id`, if given, points at a prior eval run to target its failures when augmenting.

---

## SKILL & FROM-TRACES GENERATION (agent-authored)

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

**This assessment decides fresh vs. augment** (there is no flag): an empty or thin dataset means create a fresh starter set; a populated one means add gap-fillers without duplicating. Number new cases continuing from the highest existing case number.

## Step 4: Source the Cases

Pick the sub-section for this eval's provenance (from Step 1.5). Both write case directories following the generation template from Step 2.

### 4a. `skill` — author from the skill analysis

**Fresh set** (empty/thin dataset) — design cases to cover:
- **1 simple case** — straightforward input, expected to pass all judges easily
- **1 complex case** — longer input, multiple requirements, tests the skill's full capability
- **1 edge case** — unusual input that tests boundaries (very short, very long, ambiguous, missing fields)
- **Remaining cases** — map to the judge-driven requirements from Step 1. Each should target a specific judge criterion the first three don't already stress. If there are more criteria than slots, prioritize the strictest judges (high thresholds or binary pass/fail).

**Augment** (populated dataset) — read each existing case's input, then look for gaps against:
- The skill's documented capabilities (from eval.md)
- The judges' criteria (what do judges check that no case tests?)
- Edge cases mentioned in the skill analysis
- Input variety (all cases similar? need different lengths, complexities, topics)

If `--run-id` was provided, also read that run's results to target empirical failures:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py read $AGENT_EVAL_RUNS_DIR/<run-id>/summary.yaml
```
- If a judge consistently fails, add cases that isolate its criterion
- If a judge scores low on simple inputs but passes on complex ones (or vice versa), add cases that explore that boundary
- If certain case types never fail, focus elsewhere — don't add more of the same

### 4b. `from-traces` — shape from production traces

Extract real inputs from MLflow traces:

```bash
python3 ${CLAUDE_SKILL_DIR}/../eval-mlflow/scripts/from_traces.py \
  --config <config> \
  --count <N>
```

This outputs YAML with extracted trace inputs (prompt text, tool interactions). Read it and create case directories following the generation template from Step 2 — the trace inputs give realistic content, but you still structure the files per `dataset.schema`. When augmenting (populated dataset), skip inputs already covered.

If the script exits with code 2 (no traces found) or MLflow is not configured, tell the user; fall back to `skill` authoring (4a) if appropriate.

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

**Realism**: Cases should look like something a real user would encounter. Don't generate lorem ipsum or obviously templated inputs. Use realistic names, scenarios, and domain language appropriate to the skill.

**External-state placeholders**: For fields marked `[EXTERNAL: System]` in the schema, use `TODO_<SYSTEM>_<FIELD>` as the value (e.g., `project_key: "TODO_JIRA_PROJECT_KEY"`). If you want to show a plausible real value, put it in a YAML comment (e.g., `# replace with real key, such as MYPROJECT`). The `TODO_` prefix signals that this must be replaced with a real value from the target system before execution. List all placeholders in Step 7 so the user knows what needs manual review.

**Answers, annotations, companion files, and reference outputs**: See `${CLAUDE_SKILL_DIR}/references/case-generation.md` for `answers.yaml` (interactive skills), `annotations.yaml` (outcome-aware judges with `if` conditions), companion files, and reference output guidance.

## Step 6: Validate

After generating, verify the cases:

1. Read one generated case back and check it matches the schema
2. Count files per case — do they match what `dataset.schema` describes?
3. If `execution.mode` is `case`, verify that input.yaml contains all fields referenced by `{field}` placeholders in `execution.arguments`
4. If companion files are expected, verify they exist in each case directory
5. Check for obvious issues (empty files, placeholder text, wrong field names)
6. If judges have `if` conditions referencing annotations, verify that the generated cases cover both branches — at least one case where the condition is true and one where it's false. Warn if any conditional judge would never run (or always run) across the entire dataset.

```bash
ls <dataset_path>/case-001-*/ 
```

## Step 7: Report

Tell the user what was created:

- **Cases generated**: N new cases at `<path>`
- **Provenance**: skill / from-traces; and whether this was a fresh set or an augment
- **Coverage**: What scenarios are now covered (simple, complex, edge cases)
- **What's missing**: Reference outputs (if not generated), any gaps still remaining
- **External-state placeholders**: If any `TODO_` placeholder values were generated, list each one with which case it's in, which external system it references, and what kind of value is needed (e.g., "case-001/input.yaml `TODO_JIRA_PROJECT_KEY` — needs a real Jira project key from your test instance"). These MUST be replaced with real values before running `/eval-run`.
- **Next steps** (include `--config <config>` if a non-default config was used):
  - `/eval-run --model <model>` to test the skill against these cases
  - `/eval-run --model <model> --gold` to generate gold references from the best outputs
  - `/eval-dataset --count 10` to add more cases later (augment is derived from the existing dataset; add `--run-id <id>` to target a prior run's failures)

## Step 8 (if `--harbor`): Emit Harbor task packages

If `--harbor` was passed, generate self-contained task packages for
containerized execution. Run:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/harbor.py \
  --config <config> --out <dataset_dir>/../harbor-tasks --image <image> \
  [--judge-model <model>] [--verifier-timeout 900] [--agent-timeout 3600]
```

See `${CLAUDE_SKILL_DIR}/references/case-generation.md` for details.
---

## Rules

- **Match the schema exactly** — if `dataset.schema` says "input.yaml with a 'prompt' field", create input.yaml with a prompt field. Not input.json, not query.yaml.
- **Realistic over templated** — cases should feel like real usage, not test scaffolding
- **Cover the skill's range** — don't just generate 5 variations of the same simple input. Test different capabilities the skill claims to have.
- **Don't fabricate gold outputs** — if you're not confident in what a correct output looks like, leave the reference out. Wrong references are worse than no references.
- **Name cases descriptively** — `case-003-edge-empty-context` is better than `case-003`. The name should indicate what scenario is being tested.
- **Start small** — 5 well-designed cases beat 50 random ones. Quality over quantity, especially for the first dataset.

$ARGUMENTS
