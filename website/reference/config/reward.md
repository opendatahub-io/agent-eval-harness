# reward

The optional `reward` block collapses per-judge results into a single scalar in
`[0, 1]` — the reward signal for RL training (e.g. GRPO). It is only needed when
training; the normal [`/eval-run`](../../guides/eval-run.md) report path scores
each judge independently and ignores this block.

!!! info "Where the reward is computed"
    On [Harbor](../../guides/harbor.md), the in-container verifier composes the
    reward per case and writes Harbor's `reward.json` / `reward.txt` contract. See
    the [reward API concept](../../concepts/reward-api.md) and the
    [RL cookbook](../../cookbook/reward-rl.md) for the end-to-end flow.

## Two mutually-exclusive modes

The block produces the reward one of two ways, and `judge` vs `formula` cannot
be mixed:

```mermaid
flowchart TD
    A[reward: block present?] -->|no| D[Default:<br/>bool judges gate,<br/>numerics normalized + averaged]
    A -->|yes| B{judge: set?}
    B -->|yes| J[Single-judge mode<br/>that judge's value IS the reward]
    B -->|no| F[Formula mode]
    F --> W{formula: weighted?}
    W -->|yes| WS[Weighted sum of weights:]
    W -->|no| EX["Python expression<br/>over judge names"]
```

=== "Single judge"

    The named judge's value **is** the reward — e.g. a learned reward model that
    already emits a `[0, 1]` score.

    ```yaml
    judges:
      - name: reward_model        # emits a float in [0, 1]
        module: eval.judges.rm
        function: score

    reward:
      judge: reward_model         # this judge's value is the reward
      normalize: false            # default: clamp to [0, 1] as-is
      # gate: false               # default in judge mode
    ```

    Set `normalize: true` to instead map the value from the judge's own
    [`score_range`](judges.md) to `[0, 1]` (useful when the judge emits, say, a
    1-5 rubric score):

    ```yaml
    judges:
      - name: output_quality
        score_range: [1, 5]     # the scale the value is mapped from

    reward:
      judge: output_quality
      normalize: true
    ```

    Without `normalize`, a judge that declares any other scale is misread in
    one of two directions: one reaching `1` saturates (every value at or above
    `1` is the maximum reward), a narrower one such as `[0, 0.5]` can never
    exceed `0.5`. Either warns at config load (see
    [Load-time warnings](#load-time-warnings)).

    !!! warning "`judge` stands alone"
        `judge` cannot be combined with `formula`, `weights`, or `raw` —
        combining them fails at config load. The judge name must match a judge
        defined in `judges:`, also validated at load. A missing or skipped judge
        (value `None`) scores `0.0`.

=== "Weighted formula"

    Weighted sum of the judges named in `weights`, each normalized to `[0, 1]`.
    The result is the weighted mean (divided by the sum of weights), clamped to
    `[0, 1]`.

    ```yaml
    reward:
      formula: weighted
      weights:
        quality: 0.7
        efficiency: 0.3
      raw: [efficiency]       # already in [0, 1] — skip normalization
      gate: true              # default in formula mode
    ```

    Each normalized judge is mapped from its **own** declared `score_range` (see
    [Precedence](#precedence)). Weights must be numeric and non-negative. A judge
    with a missing value is dropped from both the numerator and the weight sum.

=== "Expression formula"

    Any other `formula` value is a Python expression over judge names as
    variables. Each variable is that judge's value already normalized to `[0, 1]`.

    ```yaml
    reward:
      formula: "0.6 * quality + 0.4 * efficiency"
      raw: [efficiency]
      gate: false             # see the double-gating note below
    ```

    Multi-line expressions are allowed; the **last line is the returned value**
    (it must be an expression, not an assignment):

    ```yaml
    reward:
      formula: |
        base = mean([clarity, accuracy])
        min(base, efficiency)
      gate: false
    ```

## Fields

| Field | Type | Default | Applies to | Purpose |
| --- | --- | --- | --- | --- |
| `judge` | string | — | single-judge | Name of the judge whose value is the reward. Mutually exclusive with `formula`/`weights`/`raw`. |
| `normalize` | bool | `false` | single-judge | `false` clamps the judge value to `[0, 1]` as-is; `true` maps it from the judge's own `score_range`. |
| `formula` | string | `"weighted"` | formula | `"weighted"` or a Python expression over judge names. |
| `weights` | map | `{}` | formula (`weighted`) | Per-judge weights (numeric, non-negative). |
| `score_range` | `[min, max]` | *unset* (effectively `[1, 5]`) | both | **Deprecated** fallback range, used only for composed judges that declare no [`score_range`](judges.md) of their own — a judge's own range wins. Must be increasing. Declare the scale on the judge instead. |
| `raw` | list | `[]` | formula | Judges whose values are already in `[0, 1]`; clamped, not normalized. |
| `gate` | bool | `true` formula / `false` single-judge | both | When `true`, any boolean judge that returned `false` zeros the reward. |

## Precedence

Turning one judge's value into a `[0, 1]` contribution follows the first rule
that applies — identically **with and without** a `reward:` block:

1. **Boolean** → `1.0` / `0.0`; no range is consulted. (With `gate` on, a
   `false` has already zeroed the reward before this; in the default
   composition a `true` only passes the gate and is not averaged.)
2. **Clamped as-is** → a judge listed in `raw`, or the single `reward.judge`
   without `normalize: true`; no range is consulted.
3. The judge's own declared [`score_range`](judges.md).
4. `reward.score_range` — the deprecated fallback.
5. `[1, 5]`.

## Gating

When `gate` is `true`, **any** boolean judge that returned `false` forces the
reward to `0.0` — regardless of whether the `formula` even references that judge.

!!! warning "Avoid double-gating"
    Because gating applies to *every* boolean judge, an expression that already
    uses a boolean as its own gate (e.g. `passed * quality`) should set
    `gate: false` — otherwise the reward is gated twice.

`gate` defaults to `true` in formula mode and `false` in single-judge mode.

## Expression safety (AST validation)

Expression formulas are parsed and validated at **config load** — a typo or an
unsafe construct fails loudly then, rather than silently returning `0.0` on every
case at run time.

| Rule | Detail |
| --- | --- |
| Allowed calls | `min`, `max`, `abs`, `round`, `sum`, `len`, `mean` — nothing else |
| Operators | `+ - * / // %`, comparisons, `and`/`or`, ternary (`x if c else y`) |
| Banned | `**` (exponentiation), string/bytes constants, names starting with `_` |
| Constants | absolute magnitude capped at `1e6` |
| Size | at most 200 AST nodes |
| Structure | last statement must be an expression (the return value) |

!!! note "Load-time vs run-time errors"
    Structural and syntax problems are caught at load. **Run-time** failures — an
    undefined judge name in an expression, a division by zero — are caught during
    scoring: they emit a warning and degrade to reward `0.0` for that case.

## Load-time warnings

Two `reward:` configurations load fine but no longer mean what they read like.
Both warn from config load, so they print on every command that reads
`eval.yaml`.

A written `score_range` that a composed judge's own range now shadows:

```text
UserWarning: reward.score_range [1.0, 5.0] is deprecated and no longer normalizes
'testability' [0.0, 2.0]: a judge's own 'score_range' wins. It still applies to
'clarity'; drop it once every composed judge declares a 'score_range'.
```

It stays silent when the key is absent, when the ranges agree, for judges in
`raw`, for judges the composition never names, and in single-judge mode without
`normalize`.

A single judge clamped off its own scale:

```text
UserWarning: reward.judge 'quality' declares score_range [0.0, 2.0] but 'normalize'
is not set, so its value is clamped to [0, 1] — every score at or above 1 becomes
the maximum reward. Set 'normalize: true' to map it from [0.0, 2.0].
```

## Default when `reward` is omitted

With no `reward` block, the harness falls back to a built-in composition:

- any boolean judge returning `false` gates the reward to `0.0`;
- otherwise each numeric judge is normalized over its **own** declared `score_range` —
  falling back to `[1, 5]`, since there is no `reward.score_range` on this path — and the
  normalized values are averaged. This is the same [precedence](#precedence) a `reward:`
  block applies;
- if nothing scored because every scoring judge **errored** (e.g. values rejected as
  off-scale), the reward is `0.0`;
- if the gate passed and nothing was normalized — no numeric judges, or every one of
  them skipped by its `if:` condition — the reward is `1.0`.

## See also

<div class="grid cards" markdown>

- [**Reward API**](../../concepts/reward-api.md) — how rewards flow from judges to Harbor
- [**RL cookbook**](../../cookbook/reward-rl.md) — a complete reward-training config
- [**judges**](judges.md) — the judges a reward composes from
- [**thresholds**](thresholds.md) — suite-level regression gates (distinct from reward)

</div>
