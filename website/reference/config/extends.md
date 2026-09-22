# `extends`

An optional top-level key that layers this file (a **profile**) over a base
config. Profiles exist so a variant of an eval — another provider, a shorter
timeout, an extra judge — is a few lines next to `eval.yaml` instead of a
diverging copy of it.

```yaml
# eval-profiles/openrouter-glm-5.2.yaml
extends: ../eval.yaml            # relative to THIS file
models:
  skill:    openrouter:/z-ai/glm-5.2
  subagent: openrouter:/z-ai/glm-5.2
execution:
  timeout: 36000
permissions:
  allow: ["Bash(python3 *)"]      # ADDED to the base list
judges:
  - name: rfe_quality              # merged INTO the base judge of that name
    model: openrouter:/z-ai/glm-5.2
mlflow:
  experiment: rfe-speedrun-openrouter
```

Run it like any config: `/eval-run --config eval-profiles/openrouter-glm-5.2.yaml`.
Every reader — execute, score, report, Harbor task bundling, EvalHub, validate,
discovery — sees the **merged** config, and the run records where it came from
in `run_result.json` under `eval_params.config_chain`
(`["eval.yaml", "eval-profiles/openrouter-glm-5.2.yaml"]`, root first).

## Merge policy

| Value | Rule |
| --- | --- |
| Mappings | Merge key by key, recursively. |
| Scalars | The profile wins. |
| Lists of scalars (`permissions.allow`, `traces.events`, `plugin_dirs`, …) | **Extend with dedupe**: base items first, new items appended, duplicates dropped. |
| `judges` (entries carry `name`) and `execution.steps` (entries carry `id`) | **Merge by key**: a same-keyed entry deep-merges over the base entry, new keys append. |
| Other lists of mappings (`hooks.*`) | Extend by equality. |
| Any list tagged `!replace` | Replaces the base list outright — the escape hatch for removing a judge or an allow rule. |

```yaml
extends: ../eval.yaml
permissions:
  allow: !replace ["Bash(python3 *)"]   # the base's allow list is gone
```

`!replace` applies to list values only (a tagged mapping is a load error).
This is also the policy behind [`runner.settings`](runner.md#settings), minus
the dedupe and key-merge: settings lists extend as they always did. A provider
routing declaration merges the other way round — lists replace, because
`order` and `quantizations` are complete statements — see
[models → providers](models.md#providers-openrouter).

## Paths, names and chains

- Only `extends` itself resolves against the profile's own directory. Everything
  else — `dataset.path`, `eval_name`, the default `name` — is taken from the
  **root** of the chain, the base `eval.yaml`, so a profile runs and reports
  exactly where its base would. `prompt_file` and `plugin_dirs` keep resolving
  against the project root (the working directory).
- Chains nest (`extends: glm.yaml` inside a profile is fine) up to 8 levels;
  cycles and missing bases are load errors. `extends` must be a relative path,
  so a chain stays portable across checkouts and containers.
- A file that contains `extends` is a profile, not a standalone eval. Config
  discovery skips it **by default** (it would otherwise register as an eval named
  after its stem); callers that ask for profiles
  (`discover_configs(root, include_profiles=True)`, which the dependency scan
  does) get it back with `profile_of` pointing at its base and the base's eval
  name. `eval-profiles/` and `eval/profiles/` are the conventional homes and are
  part of the scan. Harbor task packages built from a profile record the chain in
  `task.toml` and are not reused by a run of a different chain.

## Inspecting the merged config

```bash
python3 -m agent_eval.config --print eval-profiles/openrouter-glm-5.2.yaml
```

prints the merged YAML with a `# from:` comment on every list item naming the
file(s) that contributed it, and `# !replace from:` on a list a profile replaced.
`validate_eval` prints the chain as `CONFIG_CHAIN: eval.yaml <- eval-profiles/….yaml`.
