# Runs directory & artifacts

Every `/eval-run` writes a self-contained run directory holding execution metadata,
raw logs, and per-case artifacts. Judges read from this tree, the HTML report renders
from it, and `/eval-review` / `/eval-mlflow` consume it after the fact.

## Where runs live

Runs are written under `AGENT_EVAL_RUNS_DIR` (default `eval/runs`), configured during
[`/eval-setup`](../guides/eval-check.md). Each run gets its own subdirectory keyed by
run ID.

```bash
export AGENT_EVAL_RUNS_DIR=eval/runs   # default
```

!!! note "Scoping by eval name"
    When a config declares a `name`, scripts resolve the base as
    `$AGENT_EVAL_RUNS_DIR/<eval-name>/<run-id>/`. With no name the run sits directly
    under the base. Either way, `report.html`, `run_result.json`, and `cases/` are
    siblings inside the run directory.

!!! warning "Run artifacts are sensitive"
    `stdout.log` / `events.json` hold the verbatim session transcript, and
    `permission_denials` entries preserve each denied call's full `tool_input`
    — deliberately, since the input is what distinguishes an over-strict rule
    from an escape attempt. Anything the agent saw or tried (paths, commands,
    tokens passed on command lines) is in these files: keep the runs directory
    out of version control and scrub before sharing.

## Per-run layout

```text
$AGENT_EVAL_RUNS_DIR/<run-id>/
├── run_result.json     # execution metadata (exit code, duration, tokens, cost, permission denials)
├── stdout.log          # raw agent output (JSONL stream-json for claude-code)
├── stderr.log          # captured stderr
├── collection.json     # per-case artifact counts
├── events.json         # parsed event stream (batch mode; if traces.events)
├── report.html         # scored HTML report
├── summary.yaml        # judge results: judges (mean, pass_rate, scored_cases,
│                       #   errored_cases, stability) + per_case + run_metrics
│                       #   + judge_usage / total_cost_usd (judge spend, see below)
├── provider/           # OpenRouter runs only (absent otherwise) — see "Cost provenance"
│   ├── ledger.jsonl            # one row per provider generation the harness learned about
│   ├── routing_snapshot.json   # the preflight's frozen catalog view the routing audit joins against
│   ├── hook-ids-<case>.jsonl   # generation ids of the harness's own hook calls (tool interception)
│   └── key.json                # enforcement: key-guardrail only — the per-run key's hash, name,
│                               #   limit, providers, created_at / revoked_at (never the key)
└── cases/
    └── <case-id>/
        ├── artifacts/          # files collected from outputs[].path
        ├── _modified/          # in-place edits (auto-detected via git diff)
        ├── stdout.log          # per-case agent output (case mode)
        ├── stderr.log          # per-case stderr
        ├── events.json         # parsed event stream (case mode; if traces.events)
        └── subagents/          # captured subagent transcripts (*.jsonl)
```

!!! tip "Case mode vs batch mode"
    In **case mode** (`execution.mode: case`) each case runs in its own workspace, so
    `stdout.log`, `stderr.log`, and `events.json` live under `cases/<case-id>/`. In
    **batch mode** they live at the run root (one invocation for all cases) and judges
    fall back to the run-level files. See the
    [execution model](../concepts/execution-model.md).

### `summary.yaml`

Written by `score.py judges` (and by the Harbor runner in the same shape). Besides
`judges`, `per_case`, `pairwise` and `run_metrics` it carries the **judge usage side
channel**: what the LLM judges themselves consumed, kept apart from the agent's cost.

| Field | Meaning |
| --- | --- |
| `per_case.<case>.<judge>.usage` | The judge call's usage record: `model`, `provider`, `id`, `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cost_usd`, `cost_source` (`provider-inline` when the provider priced the request in its reply, e.g. OpenRouter; `runner-estimate` for a runner/agent judge's CLI estimate; `none` when only tokens are known). Sampled judges (`samples: N`) store the sum over all attempts, failed ones included, with `requests` / `requests_missing_cost`. |
| `per_case.<case>.<judge>.tool_choice_mode` | Present only when an OpenRouter judge had to fall back from a forced tool call (`required` or `auto`). |
| `judge_usage` | Run-level aggregate: `judge_cost_usd` (`null` when no call was priced — never a partial sum), `requests`, `requests_missing_cost`, token totals, `cost_sources` (count per source), `by_judge`, `by_model`, and `tool_choice_fallbacks` when any happened. Absent when no judge produced usage (deterministic-only runs). |
| `total_cost_usd` / `total_cost_source` | Agent `cost_usd` + `judge_cost_usd`, written only when at least one addend is known. The sum is computed only when **both** are numeric; otherwise `total_cost_usd` is `null` and `total_cost_source` says which side was numeric (`complete`, `agent-only`, `judge-only`). An estimate is never used to fill a null. |

Judge spend never enters `run_result.json` `cost_usd`, so `run_metrics`
(`cost_per_turn_usd`, `cost_per_mtok_usd`) stay agent-only and comparable with older runs.
The pairwise section carries its own `judge_usage` for the comparison judge's calls.

### `run_result.json`

Written by `execute.py`. In case mode it carries an aggregate plus a `per_case`
breakdown; judges read it when `traces.metrics` is on.

| Field | Meaning |
| --- | --- |
| `exit_code` | Worst exit code across cases (non-zero on any failure) |
| `duration_s` | Sum of per-case durations |
| `wall_clock_s` | Actual elapsed time (differs from `duration_s` under parallelism) |
| `cost_usd` | Total cost across cases |
| `token_usage` | Aggregated `{input, output, ...}` token counts |
| `num_turns` | Total turns (root + subagent transcripts) |
| `num_cases` | Number of cases executed |
| `model` / `agent` / `agent_version` | Model, runner name, runner version |
| `message_ids` | Every assistant message id of the run (root stream + subagent transcripts). On a direct OpenRouter connection these are `gen-…` generation ids: the cost-truth key set the backfill prices. |
| `cost_source`, `cost_usd_estimate`, `cost_confidence`, `cost_coverage`, `cost_warnings`, `hook_cost_usd`, `providers`, `routing`, `provider`, `budget` | Cost provenance — see below. Absent on runs that never touched a provider. |
| `permission_denials` | Tool calls denied by permissions, as `[{tool_name, tool_use_id, tool_input}]` from the CLI result event (`[]` when none). Per case inside `per_case` entries (and per step under a multi-step case's `steps`); the top level carries the concatenation across cases (batch/single-run mode: that run's own list) |
| `execution_mode` | `case` or `batch` |
| `per_case` | Per-case dict of the same metrics plus `permission_denials`, keyed by case ID |

#### Cost provenance

Every `run_result.json` write goes through one reconciling writer
(`agent_eval.providers.reconcile.write_run_result`): it joins the run's
`provider/ledger.jsonl` rows against the transcript's `message_ids` and writes the
fields below. With no provider plan and no ledger file the payload is written
unchanged, so a run without OpenRouter reads exactly as before.

| Field | Meaning |
| --- | --- |
| `cost_usd` | Agent spend from a **truth source**: the sum of priced ledger rows when generation coverage is at least 80 %, else the key-usage delta, else `null`. Never the runner's estimate, never a mix. |
| `cost_usd_estimate` | The runner's own number (Claude Code prices a non-Anthropic model 2 to 60 times high). Set once, never overwritten. |
| `cost_source` | `openrouter:generation`, `openrouter:key-usage`, `runner:estimate` (only with `--allow-estimate`), `runner:reported` (Claude Code on Anthropic/Vertex, the legacy default), `harness:estimate` (Codex), or `unavailable`. Legacy literals (`openrouter-reconciled`, `runner-reported`) are read as their colon forms. |
| `cost_confidence` | `high` (coverage ≥ 95 % and the key-usage cross-check within 5 %), `medium` (coverage 80 to 95 %, or key-usage only on a dedicated key), `low` (coverage below 80 % on a shared key, or a cross-check deviation above 5 %). |
| `cost_coverage` | `{requests, requests_priced, requests_missing_cost, requests_unattributed, coverage, key_usage_delta_usd, key_usage_settle_s}` — the denominator is the transcript's `gen-…` ids, not the ledger. |
| `cost_warnings` | Human-readable findings: unpriced requests, a cross-check deviation, an unmatched per-model key, routing violations. |
| `hook_cost_usd` | Spend of the harness's own hook calls (tool interception). Excluded from `cost_usd` and from `run_metrics`. |
| `providers` | `{<provider slug>: {requests, cost_usd}}`, with `unknown` for unattributed rows. |
| `per_model_usage[m].cost_usd` / `.cost_usd_estimate` / `.providers` | Per-model cost from the ledger join (bare-slug echo, permaslug via the catalog, single-model fallback); the estimate is preserved next to it. A key with no match keeps `null` and a warning. |
| `routing` | The audit of served providers against the declared pins: `enforcement`, `policy`, `sha`, `declared`, `served`, `audited`, `compliant`, `violations`, `unattributed`, `degraded`, `audit_complete`, `snapshot`. A violation is a billed, kept generation whose provider was outside the declared set; it is reported, never repaired. |
| `provider` | `{name, kind, transport: direct, runner, base_url, key_scope, key_hash, key_exposed_to_agent, background_model}`. `key_scope` is `operator` at `enforcement: audit` and `per-run` at `key-guardrail`. |
| `budget` | `{invocation_usd, cli_cap_usd, run_usd, enforcement: cli-estimate \| key-guardrail, exceeded, exceeded_reason, overshoot_usd}`; `exceeded: run` with `post-hoc` is set when the reconciled sum passes `budget.run_usd`. |

Under a plan the case aggregate follows the same arithmetic: one unpriced case makes
the run's `cost_usd` `null` (`cases_priced` says how many were priced); a partial sum is
not spend. `execute.py --strict-cost` exits 2 on `cost_source: unavailable` or an
exceeded run budget, `--strict-routing` on violations or an incomplete audit.

`routing.enforcement` reads `none` when no routing key carries pins (nothing to
enforce, whatever the configured level). At `key-guardrail` a failed revocation of the
per-run key adds a `cost_warnings` entry naming the hash; `python3 -m
agent_eval.providers.openrouter.keys revoke <run_dir>` retries it, and `python3 -m
agent_eval.providers.openrouter.backfill <run_dir>` re-queries unpriced generations
offline.

A per-case file is usually written **inside** OpenRouter's `/generation` lag (the
record materialises 8 to 13 s after the stream ends), so it may briefly read
`cost_source: unavailable` with pending ids in `cost_coverage`; the progress line shows
`[cost pending: N ids]`. At run end the harness drains the backfill, reads the key
usage after a settle (about 20 s, up to 60 s) and re-reconciles every `run_result.json`
of the run before the strict flags judge it, so the files on disk converge. On Harbor
the trials' ids are backfilled once the job dir is parsed; `trial_costs` lists each
trial's `cost_usd` / `cost_source` / `cost_usd_estimate` (a trial is priced only when
every one of its ids has a row).

`provider/ledger.jsonl` holds one JSON row per generation: `role` (`agent`, `hook`,
`judge`, `key-usage`), `source` (`generation`, `key-usage`, `judge`), `gen_id`,
`message_index`, the requested/echoed/served model ids, `provider`, `quantization`,
`audit`, `status` (`ok`, `backfill_failed`, `partial`), `cost_usd`, native token
counts, latency and `backfill_lag_s`. Never request bodies, headers or keys.

!!! note "Adjusted, not raw, per-case values"
    For the claude-code runner, per-case `exit_code` is `1` (not `0`) when the
    CLI killed background tasks at its bg-wait ceiling — the ERROR note appended
    to `stderr.log` explains why — and `cost_usd` is the billed cost, which can
    exceed the conversation total shown in `stdout.log` when background agents
    burned tokens after the final turn.

### `collection.json`

Written by `collect.py` — a map of case ID to per-output-path artifact counts, e.g.
`{"case-001-simple": {"artifacts": 1, "artifacts/reviews": 1}}`. Use it to confirm the
run produced what you expect before scoring.

## Per-case artifacts: `artifacts/` vs `_modified/`

A case produces outputs in two distinct ways, and the harness collects both:

=== "artifacts/ (declared outputs)"

    Files the skill **writes to an output directory**. For each `outputs[].path` in
    `eval.yaml`, `collect.py` scans the workspace output dir, groups files by case
    (prefix pattern or position), and copies them under
    `cases/<case-id>/<output-path>/`.

    ```yaml title="eval.yaml"
    outputs:
      - path: artifacts
        schema: "One markdown file per case, named NNN-slug.md."
    ```

    Judges see these in `outputs["files"]` (keyed by relative path) and via convenience
    keys like `outputs["artifacts_content"]` — the last path component + `_content`.

=== "_modified/ (in-place edits)"

    Files the skill **edits in place** with the `Edit` tool instead of writing to an
    output dir. No `outputs` config is needed — detection is automatic:

    1. `workspace.py` commits the initial workspace state before execution.
    2. After execution, `collect.py` runs `git diff HEAD` to find changed files.
    3. Each modified file is copied to `cases/<case-id>/_modified/`.

    Judges see them in `outputs["files"]` under `_modified/<path>` keys, and also via
    the convenience map `outputs["modified_files"]` keyed by filename only:

    ```python
    edited = outputs.get("modified_files", {}).get("source.md")
    ```

!!! warning "`_modified/` excludes harness scaffolding"
    Paths under `.work`, `.staged-plugins/`, `subagents/`, and `hooks/` are
    skipped when building `_modified/`, so a skill's own transcripts, hook
    files, and the harness's staged plugin copies don't leak in as "edits".

```mermaid
flowchart LR
    A[Skill execution] --> B{output type}
    B -->|writes to outputs&#91;&#93;.path| C[collect.py copies files]
    C --> D["cases/&lt;id&gt;/artifacts/"]
    B -->|edits input file in place| E["git diff HEAD"]
    E --> F["cases/&lt;id&gt;/_modified/"]
    D --> G[load_case_record → outputs&#91;'files'&#93;]
    F --> G
```

## How `traces.*` gates what is captured

The [`traces`](config/traces.md) block toggles which execution data is written to the
run directory and loaded into each judge's record. Everything below is off-by-default
where noted.

| `traces` key | Captures | On disk | Judge access |
| --- | --- | --- | --- |
| `stdout: true` | Raw agent output | `stdout.log` | `outputs["stdout"]` (debugging; large) |
| `stderr: true` | Captured stderr | `stderr.log` | `outputs["stderr"]` |

> `stderr.log` may end with harness-appended `ERROR:`/`WARNING:` lines explaining
> why a case was failed (e.g. background tasks killed at the bg-wait ceiling,
> with advice to raise `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`).
| `events: true` | Parsed JSONL event stream | `events.json` | `outputs["events"]` (tool results capped at 50K chars) |
| `metrics: true` | Execution metadata | `run_result.json` | `outputs["exit_code"]`, `["duration_s"]`, `["cost_usd"]`, `["num_turns"]`, `["token_usage"]` |

!!! note "`events.json` is derived, not raw"
    It is generated at collection time by `collect.py`, parsing `stdout.log` and
    merging subagent transcripts from `subagents/*.jsonl`. `outputs["tool_calls"]`
    (for `outputs[].tool` entries) and `outputs["conversation"]` are both derived from
    it. Harbor pods instead write raw `events.jsonl`, which the scorer normalizes on
    load.

!!! tip "Artifacts and annotations are always loaded"
    `artifacts/`, `_modified/`, dataset `annotations.yaml`, and `input.yaml` are read
    regardless of `traces` — those toggles only gate logs, the event stream, and
    execution metrics.

## Related

<div class="grid cards" markdown>

- [**traces config**](config/traces.md) — the stdout / stderr / events / metrics toggles
- [**outputs config**](config/outputs.md) — declaring `path` and `tool` artifacts to collect
- [**judges**](config/judges.md) — how judges read the case record
- [**tracing**](../concepts/tracing.md) — the event stream and MLflow traces
- [**environment variables**](environment-variables.md) — `AGENT_EVAL_RUNS_DIR` and friends

</div>
