# Case Generation Reference

## Answers for interactive skills

If eval.yaml has `inputs.tools` entries for AskUserQuestion, the skill asks
questions during execution. The hook uses LLM-based answering (via
`models.hook`) that reads `input.yaml` and `answers.yaml` from each case as
context. Create `answers.yaml` with **guidance** that tells the LLM how to
answer domain-specific questions for this case:

```yaml
# answers.yaml — LLM answerer guidance for this case
dedup_is_duplicate: true
dedup_guidance: >
  This RFE is intentionally a rephrased version of an existing RFE
  about model signature verification. If asked whether existing RFEs
  cover this need, the answer is yes.
```

The LLM reads these fields alongside the question and options to pick the right
answer. For general clarifying questions, the LLM uses `input.yaml` context — no
`answers.yaml` needed. Only create `answers.yaml` when the case has
domain-specific decisions (e.g., "is this a duplicate?", "should this be
split?") where the correct answer depends on the test scenario.

If unsure what questions the skill asks, you can leave `answers.yaml` out — the
hook still calls the LLM using `input.yaml` context and the handler prompt,
falling back to the first option only if the LLM call fails.

## Annotations for outcome-aware judges

Judges receive `outputs["annotations"]` — the parsed `annotations.yaml` from
each case. If the eval config has judges that check expected outcomes (e.g.,
`annotations.get("dedup_is_duplicate")` to determine whether no output is
correct), add the relevant fields to each case's `annotations.yaml`:

```yaml
# annotations.yaml — fields for outcome-aware judges
dedup_is_duplicate: true   # or false — tells judges whether no RFE is expected
tags: [dedup, high-overlap]
known_issues:
  - dedup should flag this as overlapping with RHAIRFE-1001
```

Check the eval.yaml `judges` section for:
- Any `check` snippets that access `outputs.get("annotations", {})` — those
  fields must exist in annotations.yaml for the judge to work.
- Any `if` conditions (e.g., `if: "annotations.get('dedup_is_duplicate')"`) —
  these control which judges run per case based on annotation values. Create
  cases that exercise **both branches** of each conditional judge: some cases
  where the condition is true (judge runs) and some where it's false (judge is
  skipped). If all cases have the same annotation value, a conditional judge
  either always runs or never runs — both are gaps in coverage.

## Companion files

If eval.md lists `companion_files` (files the skill reads from disk at runtime —
e.g., `strategy.md`, `adr.md`), each test case must include them. In `case`
mode, the harness copies all case files into the workspace, so the skill will
find them at their expected relative paths. Generate realistic content for these
files appropriate to each case's scenario.

## Reference outputs

Only include gold standard reference files if you can confidently produce a
correct output. It's better to leave references out (the user can generate them
later with `/eval-run --gold`) than to include incorrect ones that mislead
judges.

## Harbor task packages

To run on Harbor (containers — Podman or Kubernetes), generate self-contained
task packages:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/harbor.py \
    --config <eval.yaml> --out <harbor-tasks-dir> --image <image> \
    --arguments '{prompt}' [--skill <skill>] [--cases case-001 ...]
```

Each task directory contains `task.toml` (env image + verifier),
`instruction.md` (resolved per-case command + input context),
`tests/test.sh` + `tests/eval.yaml` (the judge → `reward.json` bridge via
`agent_eval.harbor.reward`), and `environment/` (case input auto-uploaded by
Harbor). Per-case grading produces Harbor's `reward.json` (boolean judges gate;
numeric judges average); pairwise/regression stay suite-level above Harbor.

Notes:
- Use `--arguments` to supply a per-case template even when the eval's own
  `execution.mode` is `batch` — container isolation lets each case run
  independently.
- The bundled `tests/eval.yaml` has `dataset.path` blanked; judges score the
  agent's produced artifacts relative to the case workspace.

Run with `harbor run -p <task-dir> --agent claude-code -m <model>` or via
`/eval-run --runner harbor`. See `deploy/harbor/README.md` for details.
