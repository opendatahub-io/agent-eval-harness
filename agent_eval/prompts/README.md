# Builtin generation prompts

Generation prompts are **LLM instruction files** used by `/eval-dataset` to generate test
cases (synthetic generation). Each one tells the model how to author a *category* of
cases from repository context — it is not an interpolated template, it is a prompt.

They mirror the builtin **judges** model (`agent_eval/judges/`): builtins live here under
category subdirectories and are auto-discovered by `BuiltinPromptRegistry`. Discover them with:

```bash
python3 skills/eval-dataset/scripts/list_prompts.py
```

## Available builtins

| Reference | Purpose |
|-----------|---------|
| `docs/navigation` | Agent finds and navigates to relevant documentation |
| `docs/anti-pattern` | Agent rejects approaches that violate documented constraints |
| `docs/authoring` | Agent creates content following documented patterns |
| `docs/component-usage` | Agent explains an API/component with correct examples |
| `docs/architecture` | Agent explains system design and component interactions |

## Referencing prompts from `eval.yaml`

Generation lives in a top-level `generation:` block. Each entry in `seeds` picks one prompt via
a discriminator — mirroring judges (`builtin` / `prompt_file` / inline):

```yaml
generation:
  strategy: synthetic
  context:                       # repository knowledge injected into every prompt
    documentation_structure: { entry_point: CLAUDE.md, areas: [...] }
    constraints: [...]
    apis: [...]
  seeds:
    - category: navigation
      builtin: docs/navigation             # a builtin from this directory
      count: 10
    - category: internal-apis
      prompt_file: ./eval/prompts/internal-api.md   # project-specific, relative to eval.yaml
      count: 8
    - category: adhoc
      prompt: |                            # inline, no separate file
        Generate a case where the agent must reject a request that violates ...
      count: 3
```

Each seed's `category` is stamped onto every generated case as `annotations.category`, so the
category list is derived from the cases — never declared separately.

## Prompt structure

Each prompt is a markdown file with: **Purpose**, **Input Schema** (the `input.yaml` /
`annotations.yaml` shape to produce), **Generation Instructions** (referencing
`context.<subfield>`), **Example**, and **Validation Criteria**.

## Adding a builtin

Drop a `<name>.md` into a category subdirectory (e.g. `docs/`) following the structure above;
`BuiltinPromptRegistry` discovers it automatically. Reserve builtins for patterns proven across
several projects — project-specific prompts belong in the project repo, referenced via
`prompt_file:`.
