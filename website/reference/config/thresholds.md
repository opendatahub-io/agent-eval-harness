# thresholds

`thresholds` turns judge scores into a **pass/fail gate**. Each entry maps a
[judge](judges.md) name to one or more metric checks; if a run misses any of
them, scoring exits non-zero — the hook you want for CI.

```yaml
thresholds:
  output_quality:
    min_mean: 3.5            # numeric judge — average score across cases
    min_alpha: 0.7           # sampled judge — self-consistency alpha over the sampling matrix
  has_content:
    min_pass_rate: 1.0       # boolean judge — fraction of cases passing (0.0–1.0)
    max_error_rate: 0.2      # optional — fail if >20% of cases errored
  # pairwise:
  #   min_win_rate: 0.6      # pairwise judge — fraction of cases won vs baseline
```

The block is a plain mapping (`dict`, default empty). When it is empty or
omitted, no gate runs and scoring always succeeds. Unknown keys **warn** at
config load (they never gate); `*_alpha` values must be finite numbers
≤ 1.0 (the coefficient maximum).

## The five keys

| Key | Applies to | Metric compared | Passes when |
| --- | --- | --- | --- |
| `min_mean` | numeric (score) judges | mean score across cases | `mean >= min_mean` |
| `min_pass_rate` | boolean judges | fraction of cases returning `True` | `pass_rate >= min_pass_rate` |
| `min_win_rate` | the `pairwise` judge | fraction of cases won vs the `--baseline` run | `win_rate >= min_win_rate` |
| `max_error_rate` | any judge | fraction of cases where the judge errored | `error_rate <= max_error_rate` |
| `min_alpha` | LLM/agent judges run with [`samples > 1`](judges.md) | single-judge self-consistency alpha (upper bound on inter-rater reliability) over the case × sample rating matrix | `alpha >= min_alpha`, or all ratings identical (see below) |

`max_error_rate` is the coverage gate. `min_mean`, `min_pass_rate` and
`min_win_rate` are computed over the cases that produced a value, so a judge
that errors on most of the dataset still reports a `mean` over the survivors
and passes `min_mean`. Declare `max_error_rate` to say how much of the
dataset actually has to be scored. It is off unless declared — one flaky
judge run should not fail a suite by default.

You may set more than one key per judge; each is checked independently. The
`min_*` keys are compared with `<` and `max_error_rate` with `>`, so a
metric exactly equal to its threshold **passes** either way.

## `min_alpha` — the reliability gate

`min_alpha` gates the chance-corrected agreement of a sampled judge with
itself: the **single-judge self-consistency alpha (an upper bound on
inter-rater reliability)** written to `stability.irr` in `summary.yaml`. It
has three states:

1. **Breach** — the alpha is computed and below the bound → regression.
2. **Perfect agreement** — every rating identical, so the coefficient is 0/0
   (`reason_code: perfect_agreement`) → **passes**. A mature all-pass suite
   must not fail CI on a degenerate denominator.
3. **Configured but unavailable** — the judge ran with `samples: 1`, is
   deterministic, errored everywhere, or had too little pairable data →
   regression, matching how the other keys treat a `None` metric.

Instead of (or alongside) an explicit bound, tag the judge with a
[`consequence` tier](judges.md): `exploratory`/`safety`/`gating` inject a
default `min_alpha` of 0.67/0.70/0.80 at detection time. An explicit
`min_alpha` always wins, and the stored `thresholds` block is never
rewritten. Only 0.67 is literature-backed (Krippendorff's customary floor
for tentative conclusions); 0.70 and 0.80 are author-proposed.

!!! note "Local scoring path only"
    Harbor and EvalHub aggregations carry no per-sample stability data, so
    `min_alpha` (explicit or tier-injected) is **skipped with a stderr
    notice** on those execution paths — it never regresses a containerized
    run. Score the run locally to evaluate the reliability gate.

## Match the key to the judge's value type

This is the load-bearing gotcha: each judge produces exactly one aggregate
metric, decided by what its values *are* — not by which threshold key you write.

| Judge returns | `mean` | `pass_rate` | Use |
| --- | --- | --- | --- |
| booleans (`True`/`False`) | = pass_rate | fraction `True` | `min_pass_rate` (or `min_mean`) |
| integers/floats (e.g. 1–5) | average | `None` | `min_mean` |
| pairwise verdicts | `None` | `None` | `min_win_rate` |

!!! note "Boolean judges expose both `mean` and `pass_rate`"
    For a boolean judge the two are equal, so `min_mean: 1.0` and
    `min_pass_rate: 1.0` behave identically. Numeric judges, however, have
    `pass_rate == None` — putting `min_pass_rate` on a 1–5 judge never
    measures what you want (see below).

## A `None` metric is flagged as a regression — never skipped

When a threshold key is configured but the metric it targets is unavailable
(`None`), the harness treats that as a **regression**, not a silent pass. A
missing metric almost always means a misconfiguration or a run that produced no
data:

- the judge was **skipped for every case** (e.g. an `if:` condition never fired),
- the judge **errored on every case** — every value rejected by its
  [`score_range`](judges.md), say, since an off-scale value is recorded as an error
  sample and never aggregated (the detail names it: *"…judge errored on N cases; see
  the per-case rationales"*), or
- the threshold **targets the wrong judge type** — `min_pass_rate` on a numeric
  judge, or `min_win_rate` without a pairwise comparison.

```mermaid
flowchart TD
    A["threshold key set for judge"] --> B{"metric available?"}
    B -->|"None (unavailable)"| R["REGRESSION — value 'n/a'"]
    B -->|"value present"| C{"value >= threshold?"}
    C -->|"yes"| P["pass"]
    C -->|"no"| R2["REGRESSION"]
```

!!! warning "Wrong-type thresholds fail loudly"
    `min_pass_rate` on a numeric judge (whose `pass_rate` is always `None`) is
    reported as a regression with the detail *"pass_rate unavailable — judge
    skipped for all cases or not a boolean judge"*. Fix it by switching to
    `min_mean`, not by removing the threshold.

## What happens on a regression

`score.py` runs the check after judging and again on demand:

=== "During a run"

    `/eval-run` scores, then evaluates `thresholds`. Detected regressions are
    printed and the process **exits with status 1**:

    ```text
    REGRESSIONS: 2 detected
      [output_quality] mean: >= 3.5 -> 3.1
      [has_content] pass_rate: >= 1.0 -> 0.8
    ```

=== "Standalone re-check"

    Re-run the gate against a stored run's `summary.yaml` without re-scoring —
    handy in CI:

    ```bash
    python3 ${CLAUDE_SKILL_DIR}/scripts/score.py regression \
      --run-id <id> --config eval.yaml
    ```

    Prints `REGRESSIONS: 0` and exits `0` when clean, or lists them and exits
    `1`. Judges with no matching threshold are ignored.

## Optional baseline comparison

The `regression` command accepts `--baseline <run-id>`. In addition to the
absolute `min_*` checks, it flags any judge whose `mean` or `pass_rate` has
**dropped by more than 0.5** relative to the baseline run:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/score.py regression \
  --run-id <id> --baseline <prior-id> --config eval.yaml
```

```text
REGRESSIONS: 1 detected
  [output_quality] mean_vs_baseline: 4.2 -> 3.5
```

Baseline degradation is only checked for metrics present in both runs; the
0.5 delta is fixed and not configurable.

## Related

<div class="grid cards" markdown>

- [**judges**](judges.md) — the judges whose names you reference here
- [**reward**](reward.md) — collapse the same judges into one RL scalar instead
- [**thresholds (concept)**](../../concepts/thresholds.md) — how gating fits the lifecycle
- [**CI integration**](../../guides/ci.md) — wiring the non-zero exit into a pipeline

</div>
