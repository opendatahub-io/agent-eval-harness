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

The script will:
1. Read `generation.seeds` from the eval config
2. Resolve each seed's generation prompt (builtin / prompt_file / inline)
3. Use Claude API to generate test cases following the prompt instructions
4. Apply `generation.context` for repository-specific knowledge
5. Write cases to `<dataset_path>/case-NNN/`, stamping each with `annotations.category`

## After generation

Provenance-independent steps still apply:
- **Validate** a generated case against `dataset.schema` (see SKILL.md Step 6).
- If `--harbor` was passed, **emit Harbor task packages** (see SKILL.md Step 8) — Harbor packaging works for any provenance.

## Report Results

Tell the user:

- **Cases generated**: N cases at `<dataset_path>`
- **Categories**: List which categories and how many cases per category
- **Context**: What repository-specific knowledge was used
- **Model used**: Which model generated the cases (from `models.judge` or default)
- **Next steps**:
  - Review generated cases in `<dataset_path>/`
  - Run evaluation: `/eval-run --model <model>`
  - Generate more: increase per-seed `count` in `generation.seeds`, then re-run `/eval-dataset` (`--count` does not apply in synthetic mode)

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
