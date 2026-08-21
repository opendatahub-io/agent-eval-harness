# Implementation plan — Measurement-Validity Program (arXiv 2608.00794)

> **Status (2026-08-21): complete — all 11 commits (PR1–PR11) landed on
> `feat/measurement-validity`.** Deferred follow-ups (out of program scope, per
> the amendments below): v1-3 construct-fidelity screen; pairwise verdict-alpha
> + `min_pairwise_alpha`; cross-replication ICC
> (`agent_eval/anova/stats/test_retest.py`); simulator `samples: k`
> self-consistency draws.

## Context

`docs/paper-validity-analysis.md` (committed to the repo working tree, untracked) maps the paper
"Measurement Without Validity" onto agent-eval-harness: the harness already collects the raw data
(sampling matrices, position-swapped pairwise verdicts, human feedback channel) but reports only
chance-uncorrected exact-match agreement, has an unmeasured user simulator, and no task-validity
checks. This plan implements the paper's prescriptions as **10 feature workstreams + 1 docs
workstream**, every new surface **opt-in** (existing eval.yaml files behave byte-identically).

**Packaging (user decision)**: everything ships as **one PR** — a single branch
`feat/measurement-validity` off `main`, **one commit per workstream in the sequence below**
(commit messages = the `feat(...)` titles). Cut lines are commit boundaries: if the tail gets
heavy or review demands it, later commits drop cleanly without rework. The PR description maps
commits → prescriptions. Full unit suite must be green **after every commit**, since each
workstream extends the shared seams (score.py, config.py, report.py) the next one builds on.

Planning provenance: 9 per-workstream file-level plans + an integration plan + 3 adversarial
reviews (code-reality: 13 findings incl. 1 blocker; config-compat: 15 findings incl. 1 blocker;
scope/simplicity: 20 findings incl. 3 blockers). All review amendments and slimmings are **folded
into the specs below** — an implementer must not reproduce the original unamended workstream
plans. Full function-level specs: `/tmp/vplan/plan-{0..8}.json`, `/tmp/vplan/integration.json`,
`/tmp/vplan/reviews.json` (**execution step 0: re-read these; they are the detailed spec of
record, subordinate to this file's amendments**).

Note: the scope reviewer recommended a leaner core (WS1→WS2→WS3→WS6→WS5, deferring WS8/WS9
indefinitely). The user directed full-program single-PR packaging instead; the commit order below
keeps the reviewer's high-leverage core first, so stopping early remains trivial at any commit
boundary. Specific scope-review simplifications that WERE adopted are marked *(scope)* below.

## Global conventions (bind every PR)

1. **IRRResult is the one return contract** of `agent_eval/reliability.py` — all coefficient
   functions return it (never tuples); `min_units` keyword-only **with defaults**. Consumers use
   attribute access (`r.value`, `r.reason_code`, `r.n_units`). *(fixes review blocker #1: WS6/WS8
   originally tuple-unpacked)*
2. **One degenerate vocabulary**: snake_case `reason_code ∈ {perfect_agreement, insufficient_data,
   undefined, below_floor}` defined as WS1 constants and used verbatim in every summary block and
   every gate check. No string-matching on prose reasons. Drop `passes_gate` from the public API;
   gates check `reason_code == 'perfect_agreement'` (passes) vs anything else unavailable (fails).
3. **THRESHOLD_KEYS registry** in `agent_eval/config.py`, introduced PR2 as
   `{min_mean, min_pass_rate, min_win_rate, max_error_rate, min_alpha}`;
   PR6 appends `min_human_agreement`, PR8 `min_panel_alpha`, PR9 the reserved `simulator` mapping
   key. `_parse_thresholds` (the ONE validation helper — WS9's duplicate is superseded) validates
   all `*_alpha`/`*_agreement` values (numeric, finite, ≤ 1.0). Unknown keys warn (never error).
   `min_pairwise_alpha` (pairwise verdict-alpha gating) is deferred to a follow-up *(scope)*.
4. **detect_regressions final signature** (PR2):
   `(current_results, thresholds, baseline_results=None, *, pairwise=None, include_irr=True, simulator=None)`
   — PR9 only activates `simulator`. Three-state gate semantics documented in-function:
   breach / degenerate-pass (`reason_code == perfect_agreement`) / configured-but-unavailable =
   regression. Both wrappers (`report.py`, `log_results.py`) forward all kwargs from PR2 on.
5. **Harbor/EvalHub scoping — one mechanism**: `include_irr=False` at **both**
   `harbor/run.py:695` **and** `evalhub/runner.py:218` (PR2) covers `min_alpha`,
   `min_pairwise_alpha`, and later `min_panel_alpha`; the reserved `simulator` key is stripped on
   both paths (PR9). One combined stderr skip-notice per path. **Never mutate `config.thresholds`**
   (`harbor/run.py:329` reads it as a required-judges set) — consequence tiers resolve at detection
   time via an `effective_thresholds()` accessor.
6. **One family-inference implementation**: `agent_eval/model_families.py` (created PR7) with
   `infer_model_family` (regex-anchored table: `^(gpt-|chatgpt|o[134](-|$))` so "olmo" can't match;
   Bedrock vendor-dot prefixes; LiteLLM route stripping; `None` = unknown = stay silent),
   `family_composition` (PR8), `same_family_advisory` (PR8, extended PR10). WS7's `_provider_family`
   and WS8's reliability.py placement are superseded.
7. **Scoring-path purity**: no scipy/pandas/pingouin imports reachable from `score.py`,
   `model_families.py`, `dataset_audit.py`, `reliability.py` (ensure_deps installs only
   pyyaml/mlflow/anthropic/jinja2). PR1 ast-guards reliability.py; PR2 extends the guard to a
   pytest over all scoring-path modules.
8. **Coefficient block shape** everywhere in summary.yaml:
   `{metric, level, value, reason_code, reason, n_units, label, rationale}` — used by
   `stability.irr` (nested dict under the judge aggregate — canonical shape; WS7 reads nested, not
   flat), `panel`, `human_agreement`, `clarity`.
9. **Labeling invariants** (grep-enforced in tests): no Landis-Koch adjectives; the verbatim label
   "single-judge self-consistency alpha (upper bound on inter-rater reliability)" wherever
   stability.irr renders; every uncorrected percent-agreement carries "uncorrected"; paper
   citations use Sec 5.3/10.2/10.4/11.3/A.1/B.4/B.5 (never "Sec 6.4").
10. **hook_answers.jsonl schema frozen in PR3** with PR9's fields reserved (documented in
    tool-interception.md at PR3 time): `{ts, question, options, answer, tier:
    override|llm|fallback|disabled, hook_model, match, llm_raw, error, temperature_stripped}` +
    reserved `source: human|agent`, `calibration{gold, shadow, agree, held_out, error,
    decoding{temperature, temperature_stripped}}`, `shadows[{model, answer, error, held_out}]`.
    Scope vocabulary: `case|run` (never "batch").
11. **Docs ride the code**: CLAUDE.md + AGENTS.md (mirrored) config bullets, README.md:359
    thresholds enumeration, and website **reference/config key tables** update in the same PR as
    the key; narrative website pages consolidate in PR11. CHANGELOG entry per feature PR.
12. **Naming**: WS6's run-level block is `summary['human_calibration']` (per-judge key stays
    `human_agreement`); `thresholds.simulator` (reserved) vs the `simulator_provenance` judge get
    cross-referencing doc notes; if the deferred cross-replication ICC follow-up ever ships, its
    module is `agent_eval/anova/stats/test_retest.py` (never a second `reliability.py`).

## Commit sequence (single PR — each "PRn" below = one commit on `feat/measurement-validity`)

Land strictly in order; full test suite green after each commit. Cut lines: **A** after PR3
(chance-corrected IRR + consequence gates + measured simulator), **B** after PR5 (+ dataset
validity), **C** after PR7 (paper-complete minus cross-family), **D** full program.

---

### PR1 — `feat(reliability): pure-stdlib chance-corrected IRR module` (M)

**New** `agent_eval/reliability.py` (stdlib only): `krippendorff_alpha(units, level,
*, min_units=2)` (coincidence-matrix formulation, missing-data tolerant, nominal/ordinal/interval
deltas), `fleiss_kappa`, `cohen_kappa` (all with `min_units` **defaults** so two-arg calls work),
`select_irr_metric(n_raters, varying_identity, complete_matrix, scale) -> (metric, rationale)`
(full Figure-1 decision tree; rationale strings ship in reports per P8), `bootstrap_ci`
(unit-level cluster resampling, seeded, percentile — **consumed by PR2**, which renders the CI on
stability.irr blocks; not dead code). All return **IRRResult** (convention 1); snake_case
reason_code constants are THE vocabulary (convention 2) — consumers use `r.reason_code` directly,
no precheck duplication in score.py. **Cut** *(scope)*: the `compute_irr` one-call wrapper and
`passes_gate` (both consumers call primitives + check reason_code). **New**
`tests/test_reliability.py`: vendored oracles —
Krippendorff 2011 textbook matrix (nominal 0.743 / interval 0.849 / ordinal 0.815, n=40 pairable)
and the paper's replication-repo 20×4 matrix reproducing all 7 published alphas (4-way 0.886 +
six pairwise), Apache-2.0 attribution comments; degenerate/missing-data/CI determinism cases.
**Gate before merge**: authoring-time cross-check of pinned values against PyPI `krippendorff`
(dev-only, never a runtime dep). Docs: architecture-tree bullets.

### PR2 — `feat(scoring): IRR over sampling matrices + consequence-tier min_alpha gates` (M)

The **convention-setter** (conventions 3, 4, 5, 7, and the canonical coefficient block shape of
convention 8 — later commits inherit it, never rename at rebase). `score.py`: cross-case
stability block (:1660-1676) builds the case×sample matrix from
`per_case[*][judge].stability.values` (+ `[None]*error_count`), calls the WS1 primitives
(select_irr_metric → coefficient), writes nested `stability.irr` **including a bootstrap CI**
(`ci: [lo, hi]` — at 3-20 cases the interval is the finding); per-case Fleiss completeness check
`len(values)==samples`; kappa-vs-alpha divergence surfaced (P3). Degenerate handling consumes
`IRRResult.reason_code` directly (no duplicate prechecks *(scope)*). Pairwise *(scope-slimmed)*:
persist dropped `pref_ab`/`pref_ba` (:2225-2233) + the trivial `swap_consistency` rate; the
pairwise verdict-alpha and its `min_pairwise_alpha` gate are **deferred to a follow-up**; headline
wins/ties counts unchanged. Gating: `min_alpha` (per-case self-consistency);
`JudgeConfig.consequence: exploratory|safety|gating` wired through the explicit-kwargs
constructor (:1281-1301), `CONSEQUENCE_TIER_MIN_ALPHA {0.67, 0.70, 0.80}` resolved via
`effective_thresholds()` at detection time — kept thin (the scope reviewer proposed cutting
tiers entirely; retained as the P5 surface per user engagement, minimal machinery: one accessor,
one load warning); warning fires when a consequence-tagged judge can't produce IRR data —
**including builtin-LLM judges** (pinned to n=1 at score.py:785-790). `cmd_pairwise` gains **no**
new exit code (gate via `score.py regression` only). Harbor/EvalHub: `include_irr=False` + notice
at both call sites. Report: `_irr_badge` beside the stability bar (:1358-1364), threshold column,
rationale tooltip, the verbatim upper-bound label. **Amendment (review)**: add `consequence` to
`valid_judge_fields` in `skills/eval-analyze/scripts/validate_eval.py` (:598-602 — unknown judge
fields are load ERRORS there) + round-trip test. No MLflow alpha metrics yet (PR7). Tests:
test_irr_scoring, test_min_alpha_gate, **test_threshold_consumers.py parity harness (new — every
later gate commit adds rows)**, config/report/harbor rows; **one shared hygiene test**
(no-Landis-Koch / no-"Sec 6.4" greps live here only *(scope)*), assertions go through
`_merge_summary` output, not re-simulated internals. Docs: thresholds + judges bullets everywhere
(incl. README.md:359).

### PR3 — `feat(eval-run): simulator answer-provenance ledger + provenance judge` (M)

`tools.py`: one JSONL record per intercepted question to `Path(__file__).parent /
'hook_answers.jsonl'` (works in case/batch/in-repo modes; never CWD), `_llm_answer` → `(label,
meta)`, disabled-records on **both** silent-disable paths (PyYAML ImportError :31-43; missing
tool_handlers.yaml :52-54), best-effort writes inside the never-crash envelope. **Adopted fix**:
resolve `tool_handlers.yaml` via `__file__` fallback too — this repairs the pre-existing silently
broken in-repo interception (CHANGELOG behavior note). `collect.py`: per-case copy + batch
run-root copy; ledger stays out of `_modified/` (hooks/ already in `_HARNESS_PATHS:35`).
`score.py load_case_record`: expose `hook_answers` None-vs-list + `interception_configured` flag +
run-root fallback + **`case_dir/hooks/hook_answers.jsonl` fallback** — *(fixes config-compat
blocker: in-container Harbor scoring finds the ledger, so the judge works on Harbor instead of
zeroing mean_reward via the default bool-gate reward)*. **New builtin** `process/
simulator_provenance`: fails on fallback/disabled/error records OR when interception is configured
+ AskUserQuestion events exist + ledger is None; passing certifies **provenance coverage only**
(docstring + docs); batch mode reports run-level unattributed provenance. Report: per-case
"Simulated user" tier line — **one lenient JSONL ledger parser lives in score.py; report.py
imports it** (no second parser) *(scope)*. Freeze the schema with PR9's reserved fields
(convention 10); document
the hook-crash external-kill blind spot. Recipe (docs): judge + `thresholds.simulator_provenance.
min_pass_rate: 1.0`.

### PR4 — `feat(eval-dataset): deterministic dataset audit + generation manifest` (M)

v1-1 scope only (**v1-3 construct-fidelity screen deferred** to a follow-up PR — unverified
design, new LLM-cost surface; drop config.py/validate_eval.py/screen_dataset.py from this PR).
**New** `agent_eval/dataset_audit.py` + thin `skills/eval-dataset/scripts/audit_dataset.py`
(bootstrap import; symlink exists): reference-resolution check (labeled *necessary-not-sufficient*
for answerability), verbatim contamination (sanctioned `answers.yaml` workspace copy whitelisted),
near-duplicate detection (difflib/Jaccard, ~0.85, CLI-flag thresholds — **no new eval.yaml
surface**), composition/skew tables (difficulty check presence-conditional), conditional-judge
branch analysis restricted to annotations-only `if:` conditions — sharing/cross-pinning
score.py:1553's evaluator (no second condition evaluator); any `{{` templated argument field is
reported indeterminate wholesale *(scope)*; outputs-referencing → indeterminate. **`write_audit` is load-and-merge**: replaces only
audit-owned keys, preserves foreign top-level keys (`null_probe`, future `construct_fidelity`) +
round-trip test *(fixes review major)*. `generate_synthetic.py`: persist `manifest.yaml`
(generator model, per-seed resolved-prompt sha256, context hash, temperature, timestamp passed in,
realized per-seed counts, per-case provenance from :175-179); `--force` semantics reconciled with
the documented resize flow in `references/synthetic-generation.md`. `workspace.py`: soft preflight
WARN via **per-case content hashes** stored in the audit (not dir mtimes); same 6-line warning in
`harbor/tasks.py` (path parity). Audit artifacts live at dataset root as files — invisible to all
five dir-only case-discovery sites (verified).

### PR5 — `feat(dataset): null-agent solvability probe` (S/M)

**New** `agent_eval/agent/null.py` NullRunner (RUNNERS registry, agent/__init__.py:9-14);
`runner.type: "null"` **rejected at config load** with a pointer to `--agent null` (YAML-footgun
fix). `audit_dataset.py --null-run <run-dir>`: reads per_case per-judge records, **recomputes**
reward via `agent_eval.harbor.reward.compose_reward` (reward is not stored in summary.yaml); flags
on bool-judge passes + reward ≥ `--reward-threshold` (**fixed default 0.5** — no re-derivation of
normalization semantics *(scope)*); if-skipped (value None) and errored judges never count; merges a `null_probe` key into
dataset_audit.yaml. Exit 0 by default (findings, not verdicts) + opt-in `--fail-on-null-pass` for
CI. Statistic labeled "null-pass rate (joint task/judge non-discriminativeness, upper-bounds
1−V1)". SKILL.md Step 6.5: exact command sequence (`--model` still required by execute.py but
ignored; interception resolution skippable; judges run with `--samples 3` for majority-voted
null-passes). Batch-mode datasets: documented limitation (audit exits 2 with guidance).

### PR6 — `feat(eval-review): human-anchored judge calibration` (M)

`review.yaml` gains optional `verdicts: {case_dir: {judge: value}}`, `reviewer_id` (asked during
review, default "human"), `blind: bool`, `selection: all|failures|manual`; eval-review SKILL.md
elicits verdicts **before** revealing judge rationale (Step 3 sub-step; reconcile with existing
`reviewer:` field). `score.py calibration` subcommand (help text disambiguates from simulator
calibration): joins verdicts vs `per_case` **reduced** values on the case-dir key, excludes
null-valued (if-skipped/errored) entries, metric via `select_irr_metric` (Cohen kappa valid here:
2 fixed raters; alpha otherwise), IRRResult attribute-style; below n<5 → raw agreement table, no
coefficient (configured gate then = regression, the plan default); merges per-judge
`human_agreement` into `summary['judges'][name]` (visible to detector + report) **and** the
run-level `summary['human_calibration']` block. **Amendments (review)**: `min_human_agreement`
joins THRESHOLD_KEYS with value validation (the "no config.py changes" claim was stale after PR2);
**stale-calibration signal** — when the gate is configured, the judge row lacks `human_agreement`,
but `human_calibration` names that judge → loud "stale calibration — re-run score.py calibration"
regression (silent skip only when never calibrated). Optional attach_feedback.py: push verdicts as
HUMAN-source `{case}/{judge}/human` assessments. Label: "agreement with a single human reviewer
(n=X)"; blind status rendered as **"reviewer-reported blind"** (self-reported, not enforced)
*(scope)*. Deterministic `check` judges are first-class targets.

### PR7 — `feat(validity): P8 validity report section + MLflow/anova/compare routing` (M)

`score.py`: `build_validity_block` → `summary['validity']`: per-judge P8 triple (metric, value +
threshold, selection rationale — read from **nested** `stability.irr`; `_judge_irr` accessor is
the single read point), `human_agreement` passthrough, three layer stanzas (V1: generation
strategy + dataset_audit presence; V2: interception status — **a `*` wildcard handler counts as
intercepting AskUserQuestion** (interception.py:54 + tools.py:118 prefix rule) — hook model,
"uncalibrated simulator" until PR9 data exists; V3: IRR table), honest V_total: multiplicative
frame with unmeasured layers **named**, never a fabricated number, `_alpha_threshold` delegates to
`effective_thresholds()`/`CONSEQUENCE_TIER_MIN_ALPHA` (no re-derivation). Same-family caveat via
**new** `agent_eval/model_families.py` (convention 6). `report.py`: `_render_validity` between
Scoring Summary and Regressions (final section order: Scoring Summary → Simulator Calibration
(PR9) → Validity & Reliability → Regressions → Human Calibration (PR6) → Shared Outputs);
displayed-precision restraint (B.5) on means. `log_results.py`: `_validity_mlflow_fields` —
per-judge `{judge}/irr_value` metrics + `validity/*` tags (per-judge tag fan-out accepted,
~≤13 tags) — the **only** PR that routes validity to MLflow. **Deferred out of this commit
*(scope)***: the entire cross-replication ICC half (pingouin `test_retest.py`, analyze.py
`judge::` columns, compare.py ICC table) — it measures pipeline test-retest (agent+judge variance
confounded), not a paper V-layer, and came from the unverified V3-3 proposal; it becomes a
follow-up after the program, alongside eval-anova's own deep-report rendering.
`eval-optimize/SKILL.md`: one paragraph — low alpha / low human agreement =
construct-development signal (Sec 11.3), consume via the optimize loop. Re-scoring invalidation
note ("re-run calibration/clarity/validity subcommands") in cmd_judges output + SKILL.md.

### PR8 — `feat(judges): cross-family judge panels + instrument-clarity check` (L)

`judges[].model` accepts a **list** (string stays valid; `model: null` normalized before the type
check — backcompat); panel = k samples per model via the existing samples loop, majority/median
reduction, cases×models Krippendorff alpha (per-model ratings as raters), `panel` block in the
coefficient shape, family composition labels from model_families. Non-Anthropic members via
gateway aliases (ANTHROPIC_BASE_URL / LiteLLM) — single Anthropic-Messages client;
`make_judge`-style fallbacks reject panels with a clear error. `min_panel_alpha` joins
THRESHOLD_KEYS, routed **under `include_irr`** (no separate strip mechanism; EvalHub covered;
test row mirroring PR2's harbor test) *(fixes review major)*. `same_family_advisory` wired into
`from_yaml` — **fires only when reliability features are engaged** (a judges[].model panel,
models.hook_shadow, or a consequence-tagged judge — user decision Q2); **consequence tiers inject
min_alpha only** — a consequence-tagged judge with a panel but no explicit `min_panel_alpha` gets
a load warning (user decision Q3). Instrument-clarity (Sec 10.2): `score.py clarity` subcommand —
3 cross-family raters × N≤20 stratified case subsample, 4-way alpha vs 0.67, `clarity` block +
report badge; explicitly instrument-clarity, not rater validity; degenerate checks via
`reason_code`, never prose string-matching *(scope)*. (Scope reviewer would defer WS8 entirely
pending gateway demand — retained per user's full-program direction; the gateway prerequisite is
stated in docs, and within-family panels are labeled as such, never sold as cross-family.)
Harbor docs: panels execute
in-container (m× judge cost) while the cross-case alpha is not aggregated on that path yet;
min-harness-version note for task-package reuse. Large docs surface (9 files listed in the
workstream plan).

### PR9 — `feat(simulator): calibration shadow + simulator block + thresholds.simulator` (L)

The corrected V2-2 slice (`samples: k` stays deferred; ledger records decoding config now).
Config: `ToolInputConfig.calibration: bool` serialized via **post-load merge in
`generate_interception`** (applies to heuristic AND resolved tool_handlers.yaml — the bypass at
interception.py:143-149) + mirrored in the `_setup_in_repo_tool_hooks` rewrite (build_handlers +
merge); SKILL.md Step 3a (not "3b") tells the agent's rewrite to preserve harness-owned keys;
`case_overrides` gain machine-readable provenance (`source: human|agent`, per-entry or file-level).
`tools.py`: shadow-runs the LLM tier on override-answered questions, **held-out** (answers.yaml
stripped from context), logged never injected; `hook_model` setdefault fix shipped loudly
(CHANGELOG + generate_interception notice). **Hook wall-clock (named change, not a risk note)**:
explicit `timeout` on generated hook entries at all three generation sites (interception.py
build_settings_hooks, workspace.py case/batch, in-repo) + in-hook overall deadline that degrades
to fallback-with-ledger-record ("skipped: deadline") instead of being killed. `score.py`
`aggregate_simulator` → `summary['simulator']`: tier distribution, fallback rate, gold agreement
stratified by source (P1 banner fires unless HUMAN-provenance pairs exist; agent pairs labeled
"LLM-vs-LLM consistency (not human calibration)"), `ledger_scope: case|run|missing`.
`thresholds.simulator` reserved mapping key (`max_fallback_rate`, `min_gold_agreement` —
human-stratum only, fail-loud; `min_cross_simulator_agreement` reserved for PR10): validated in
`_parse_thresholds`; `detect_regressions(simulator=...)` at all call sites; **stripped + notice on
Harbor AND EvalHub** (evalhub/runner.py:216-218; config_translator.py pass_criteria note)
*(fixes review majors)*. `validate_eval.py`: exempt the reserved key from the judge-name warning +
sub-key validation + zero-warning test. Judge named "simulator": **two-stage reservation** —
ValueError only when thresholds.simulator coexists; DeprecationWarning otherwise *(backcompat
fix)*. Report: Simulator Calibration card (tier bar, gold agreement with n, banner).

### PR10 — `feat(simulator): cross-family shadow simulators` (M, stacked on PR9)

`models.hook_shadow` (max 2, gateway aliases; answers logged, never injected); `cross_simulator`
section (all-agree rate, nominal alpha when computable, per-question disagreements, family
composition so within-family agreement is never sold as robustness); `same_family_advisory`
extended (hook_shadow suppression + DEFAULT_HOOK_MODEL drift-guard test vs the tools.py literal).
In single-PR packaging this is simply the **last feature commit**; the ledger schema is frozen at
PR3 with these fields reserved, so no schema rev occurs. (Scope reviewer would defer WS9/WS10
entirely — retained per user's full-program direction, with the hook-deadline budget and EvalHub
strip as named changes, not risk notes.)

### PR11 — `docs: validity program consolidation` (S)

Narrative website sweep (concepts/guides/glossary/reading-the-report), the six-way glossary
disambiguation (stability / stability.irr / panel / human_agreement / simulator.calibration /
anova reliability), thresholds.simulator vs simulator_provenance adjacency notes, ci.md gating
examples, README safety-net sweep, status marks in docs/paper-validity-analysis.md, **merged with
the parked docs-PR follow-ups from project memory** (hooks, workspace.files, judges.samples +
score_range, stale traces.events bullet). The cross-replication ICC feature (deferred from PR7)
and its docs are a separate post-program follow-up, not part of PR11.

---

## Decisions already taken (defaults adopted from reviews — flag at approval if you disagree)

- Pairwise slimmed *(scope)*: PR2 persists `pref_ab`/`pref_ba` + `swap_consistency` only; the
  pairwise verdict-alpha and a `min_pairwise_alpha` gate are a post-program follow-up.
- `cmd_pairwise` gains no new exit code; pairwise gating lives in `score.py regression`.
- Pairwise headline counts unchanged (swap-inconsistency stays in `ties`; `swap_consistency`
  reported separately).
- WS1 slimmed *(scope)*: no `compute_irr` wrapper, no `passes_gate`; `bootstrap_ci` kept because
  PR2 renders the CI on stability.irr.
- `consequence:` tiers kept (thin) despite the scope reviewer's cut recommendation — they are the
  P5 surface and the user engaged with the feature (Q3); implementation is one detection-time
  accessor + one load warning, no threshold mutation.
- Cross-replication ICC (pingouin, eval-anova) deferred out of the program entirely *(scope)*.
- One ledger parser (score.py), one condition evaluator (score.py's, shared/cross-pinned by the
  audit), one hygiene test (PR2) *(scope)*.
- PR3 **fixes** the pre-existing in-repo interception breakage (tool_handlers.yaml via __file__).
- v1-3 construct-fidelity screen deferred out of the sequence (follow-up after PR4).
- Null probe: exit 0 default + `--fail-on-null-pass`; batch-mode limitation documented not fixed.
- PR6 below-floor (n<5) with a configured gate = regression (consistent with
  configured-but-unavailable).
- PR7 per-judge MLflow tag fan-out accepted (~13 tags typical).
- PR9 `min_gold_agreement` gates the human stratum only, fail-loud (no agent-pair opt-in knob v1).
- Cross-family (panels + shadows) is gateway-alias-only in v1 (no per-model endpoint/secrets
  surface inside the never-crash hook).

## User decisions (recorded)

- **Q1 — packaging/scope**: user asked "can this be all in one PR?" → **Yes: full program, one
  PR** on branch `feat/measurement-validity`, one commit per workstream in sequence order; cut
  lines remain available as commit boundaries during execution/review.
- **Q2 — B.4 same-family advisory**: **fires only when reliability features are engaged**
  (judges[].model panel, models.hook_shadow, or consequence-tagged judge); the report caveat
  always renders.
- **Q3 — consequence tiers × panels**: **self-consistency only** — tier injects min_alpha; a
  consequence-tagged panel judge without explicit min_panel_alpha gets a load warning.

## Impact on eval-anova (reviewed pre-execution, verified against code)

The program **changes no eval-anova code** (the cross-replication ICC enhancement was deferred
out entirely, per the scope review). Verified compatibility and interactions:

1. **Additive-safe consumption** — `load_conditions_from_runs` (analyze.py:251-289) reads only
   `summary['per_case']` and passes each case's judge dicts to `compose_reward`, which ignores
   unknown keys. All new summary surfaces are either top-level keys it never reads (`validity`,
   `simulator`, `human_calibration`, `pairwise` extensions) or extra fields nested inside
   judge aggregates/records (`stability.irr`, `human_agreement`, `panel` — records keep `value`).
   Existing anova runs and new-format runs mix freely in one `--analyze-only` pass.
2. **Matrix factors don't cover the new knobs** — `_RUNNER_FACTORS` (orchestrate.py:59) is
   `model/effort/subagent/subagent_model`; unmapped level keys are rejected (:106). You cannot yet
   ANOVA over `models.hook`, `judges[].model` panels, or `judges.samples` as factors. Named
   follow-up (post-program): extend the matrix schema with hook/judge factors — this was also the
   V2 assessment's observation that simulator comparisons need exactly this.
3. **Cost multiplication, not estimated** — matrix cells inherit eval.yaml unchanged, so
   `judges.samples: k` multiplies judge cost across every cell × replication; the matrix cost
   estimator doesn't model judge sampling. One docs sentence in PR2's thresholds/judges docs +
   the eval-anova QUICKSTART note (PR11).
4. **`simulator_provenance` is a boolean gate in the default reward** — adding the PR3 judge to
   an eval.yaml used for anova (or RL reward) zeroes the composite for any fallback-answered
   case under the default bool-gate composition. Intended semantics, but it shifts anova
   composites; document next to the judge recipe (PR3): exclude it via the `reward:` section or
   accept the gate.
5. **Per-run reliability gates don't break analysis** — a matrix cell whose run trips `min_alpha`
   still produces a full summary.yaml; analyze.py skips runs only on unreadable/missing
   `per_case`. Regression exit codes affect CI semantics, not anova aggregation.

## Verification

- Per PR: its new/extended pytest files plus the full unit suite
  (`python3 -m pytest tests/ -v`); `tests/test_threshold_consumers.py` parity harness must pass
  with rows for every gate key shipped so far; `tests/test_venv_activation.py` enforces
  bootstrap/symlink conventions automatically.
- PR1 gate: pinned oracle values cross-checked against PyPI `krippendorff` before merge
  (authoring-time only).
- After PR2 and after PR9: one real mini-eval (existing fixture eval.yaml, `samples: 3`,
  interception on) to see `stability.irr` in summary.yaml, the report badges, the ledger, and the
  provenance judge on live data; after PR7 confirm `mlflow` run shows validity tags/metrics.
- Website: `mkdocs build --strict` (site builds strict; a shipped key missing from its reference
  table is a docs bug).
- Harbor/EvalHub: `tests/test_harbor_run.py` rows added in PR2/PR8/PR9 assert skip-with-notice
  (no reliability gate ever regresses those paths).

## Execution notes

- Step 0a (user request): **store this plan in the repo** as
  `specs/012-measurement-validity/plan.md` (next free spec number after 011-multi-step-execution),
  committed as the first commit on the branch; also copy `/tmp/vplan/*.json` into
  `specs/012-measurement-validity/details/` so the function-level specs survive /tmp cleanup.
- Step 0b: the vplan JSONs are the spec of record beneath this plan (this file's amendments win
  on any conflict).
- Current branch is `feat/stage-plugins-in-workspace`; create `feat/measurement-validity` off
  `main`. One commit per workstream (PR1…PR11 order), full test suite green after each commit;
  open a single PR to `main` at the end (or earlier at a cut line if review size demands — user
  preference is one PR).
- All three reviews are folded into this file; any discoveries during execution amend the owning
  commit's spec, never a silent follow-up.
