# Synthetic Generation (prompt-mode evals)

Used when `eval.yaml` has `generation.strategy: synthetic` (typically from `/eval-analyze --prompt`).
A script generates cases directly from `generation.seeds` — the agent does not author them.

## What it does

Generates test cases from the `generation.seeds`. Each seed names a `category`, a `count`, and one
**generation prompt** via a discriminator (mirroring judges): `builtin: docs/navigation` (a builtin
from `agent_eval/prompts/`), `prompt_file: ./path.md` (project-specific), or an inline `prompt:`.
Repository knowledge from `generation.context` is injected into every prompt. Discover builtin prompts:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/list_prompts.py
```

## Execute

Extract the judge model from the eval config and call the generation script (use the --config path from Step 0):

```bash
JUDGE_MODEL=$(python3 -c "from pathlib import Path; import yaml; import sys; config = yaml.safe_load(Path(sys.argv[1]).read_text()); print(config.get('models', {}).get('judge', 'claude-opus-4-6'))" "<config_path>")

python3 ${CLAUDE_SKILL_DIR}/scripts/generate_synthetic.py \
  --config <config_path> \
  --output <dataset_path> \
  --model "${JUDGE_MODEL}"
```

Replace `<config_path>` with the actual value from the --config argument (default: eval.yaml).
Add `--force` when regenerating over an existing `case-NNN` set (see the resize flow under
Report Results) — without it the script refuses to overwrite previously generated cases.

The script will:
1. Read `generation.seeds` from the eval config
2. Resolve each seed's generation prompt (builtin / prompt_file / inline)
3. Use Claude API to generate test cases following the prompt instructions
4. Apply `generation.context` for repository-specific knowledge
5. Write cases to `<dataset_path>/case-NNN/`, stamping each with `annotations.category`
6. Write `manifest.yaml` at the dataset root — generation provenance: generator model,
   temperature, sha256 of each seed's resolved prompt, `generation.context` hash,
   timestamp, per-seed requested/returned/written counts (what the LLM produced vs
   what per-case validation kept), `failed_categories`, and per-case provenance
   (case_id, category, seed source). A root-level file: case discovery ignores it.

## After generation

Provenance-independent steps still apply:
- **Audit + validate** the dataset (see SKILL.md Step 6): run `audit_dataset.py`
  (writes `dataset_audit.yaml` at the dataset root), then review a generated case
  against `dataset.schema`.
- If `--harbor` was passed, **emit Harbor task packages** (see SKILL.md Step 8) — Harbor packaging works for any provenance.

## Report Results

Tell the user:

- **Cases generated**: N cases at `<dataset_path>`
- **Categories**: List which categories and how many cases per category
- **Context**: What repository-specific knowledge was used
- **Model used**: Which model generated the cases (from `models.judge` or default)
- **Provenance**: `manifest.yaml` at the dataset root (model, per-seed prompt hashes, realized counts)
- **Next steps**:
  - Review generated cases in `<dataset_path>/`
  - Run evaluation: `/eval-run --model <model>`
  - Generate more: increase per-seed `count` in `generation.seeds`, then re-run
    `/eval-dataset` passing `--force` to the generation script (`--count` does not
    apply in synthetic mode). Regeneration **REPLACES the entire `case-NNN` set**
    with fresh stochastic content — temperature 1.0, seed counts are declarative
    totals, and numbering restarts at `case-001` — so the script refuses to
    overwrite without `--force`. Hand-edits such as `TODO_` replacements are lost
    and must be reapplied; `manifest.yaml` is rewritten to describe the new set.

## Example Output

```text
Generated 15 test cases:
  - navigation (5 cases): docs/navigation
  - anti-pattern (5 cases): docs/anti-pattern
  - authoring (5 cases): docs/authoring

Context applied:
  - Documentation structure: CLAUDE.md, ai-docs/workflows/, ai-docs/domain/
  - Constraints: 3 rules from ai-docs/practices/
  - APIs: 5 components from context.apis

Model used: claude-opus-4-6

Next: /eval-run --model opus
```
