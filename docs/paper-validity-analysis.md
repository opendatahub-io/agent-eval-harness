# Measurement Without Validity → agent-eval-harness: implementation analysis

Paper: **"Measurement Without Validity: The Compounding Reliability Problem in
Agentic AI Evaluation"** — William Caban, [arXiv 2608.00794](https://arxiv.org/abs/2608.00794),
34 pp., submitted to Knowledge-Based Systems.

Analysis date: 2026-08-21. Produced by a multi-agent review (3 paper readers,
5 subsystem audits, 3 per-layer designers, 6 adversarial code-grounded
verifiers, 1 completeness critic). All file/line citations below were verified
against the working tree at commit `157d917`.

---

## Implementation status (2026-08-21)

The program shipped as commits PR1–PR10 on `feat/measurement-validity`
(plan of record: `specs/012-measurement-validity/plan.md`). Per proposal:

| Proposal / prescription | Outcome |
|---|---|
| V3-1 chance-corrected IRR + `consequence` tiers | **Shipped** — PR1 (`agent_eval/reliability.py`) + PR2 (`stability.irr`, `min_alpha`, tier injection, bootstrap CI — closing gap 2) |
| V2-1 answer-provenance ledger + provenance judge | **Shipped** — PR3 (`hook_answers.jsonl`, `process/simulator_provenance`; also fixed the pre-existing in-repo interception breakage) |
| v1-1 dataset audit + generation manifest | **Shipped** — PR4 (`dataset_audit.yaml`, `manifest.yaml`, workspace preflight) |
| v1-2 null-agent solvability probe | **Shipped** — PR5 (`--agent null`, `audit_dataset.py --null-run`, `--fail-on-null-pass`) |
| V3-2 human-anchored judge calibration | **Shipped** — PR6 (`score.py calibration`, `human_agreement`, `min_human_agreement`, stale-calibration signal) |
| V3-3 validity report section + MLflow routing | **Shipped** — PR7 (`summary['validity']`, report section, MLflow tags/metrics — closing gaps 6 and 8, plus the gap-4 `/eval-optimize` paragraph); the cross-replication ICC half was **deferred** (see below) |
| P4 cross-family judge panels + Sec 10.2 clarity | **Shipped** — PR8 (`judges[].model` list, `min_panel_alpha`, `score.py clarity` — closing gaps 1 and 3) |
| V2-2 calibration shadow + simulator gates | **Shipped (slice)** — PR9 (`inputs.tools[].calibration`, `summary['simulator']`, `thresholds.simulator`); the `samples: k` half was **deferred** (see below) |
| V2-3 cross-family shadow simulators + same-family advisory | **Shipped** — PR10 (`models.hook_shadow`, `cross_simulator`; the B.4 advisory landed in PR7/PR8's `model_families.py`) |
| P7 demographic stratification | **N/A** — no dialect surface (unchanged from the analysis below) |

**Deferred** (named follow-ups, not silently dropped): the **v1-3
construct-fidelity screen** (unverified design, new LLM-cost surface); the
**pairwise verdict-alpha and its `min_pairwise_alpha` gate** (PR2 persists
`pref_ab`/`pref_ba` and the uncorrected `swap_consistency` rate only); the
**cross-replication ICC** for eval-anova (pipeline test-retest, not a paper
V-layer; module name reserved: `agent_eval/anova/stats/test_retest.py`); and
**`samples: k` simulator self-consistency draws** (the ledger already records
decoding config for it). Gap 5 (execution-path parity) was resolved as explicit
skip-with-notice on Harbor/EvalHub rather than parity; gap 7 (test oracle) via
PR1's vendored replication-repo fixtures.

The docs narrative (glossary disambiguation, CI gating examples, the
measurement-validity concepts page) landed as PR11.

---

## 1. What the paper says

**Central claim.** Total validity of an agentic evaluation pipeline is bounded
multiplicatively across three layers:

```
V_total ≤ V1 (task generation) × V2 (human-simulator calibration) × V3 (automated judgment)
```

Validity failures multiply, they don't add: 70% validity per stage caps the
pipeline at ~34% (0.70³). The paper stresses this is a *conceptual model*
(Sec 10.4), not a theorem — correlated failures (e.g. same provider family at
several layers, Appendix B.4) push real pipelines below the bound.

**Evidence per layer.**

- **V1** — a study of 10 popular agentic benchmarks found task-validity flaws
  in 7 and outcome-validity flaws in 7; all 10 had reporting gaps. Flagship
  statistic: *do-nothing agents passed 38% of tau-bench airline tasks*.
- **V2** — agent success varied up to **9 percentage points** across different
  LLM user simulators on the same tasks (Seshadri et al.), with directional
  miscalibration and demographic/dialect disparities (AAVE vs SAE).
  Key aphorism: *"Agreement on a distorted signal is agreement on distortion"*
  — high IRR downstream cannot rescue a miscalibrated simulator.
- **V3** — survey of 55 papers (2022–2026): **~82% use structurally
  mismatched, incomplete, or absent inter-rater reliability metrics**. Largest
  single failure mode: no IRR metric reported at all (24%). Cohen's kappa is
  *structurally invalid* for most agentic designs (it assumes exactly 2 fixed
  raters scoring every item — violated by rotating judge pools, per-task
  assignment, varying rater identity). Measured gap: substring grading reaches
  kappa = 0.049 vs human; 3-LLM ensemble 0.432; rigorous human annotation
  (WebArena Verified) 0.83.

**The eight prescriptions** (Sec 8-9, thresholds stratified by consequence):

| # | Prescription | Threshold |
|---|---|---|
| P1 | Validate simulator calibration against real human interactions before trusting results; report the calibration statistic (or its absence) alongside results | ICC(A,1) ≥ 0.70, ≥ 50 matched tasks |
| P2 | Select the IRR metric by pipeline structure, never convention (Figure 1 decision tree — the paper's "primary deployable artifact") | Cohen's kappa only for 2 fixed raters/complete matrix; Fleiss for fixed nominal panels; Krippendorff's alpha for ordinal/continuous, missing ratings, varying rater identity |
| P3 | Report both Fleiss' kappa and Krippendorff's alpha for ordinal rubrics; treat kappa < alpha divergence as a diagnostic | — |
| P4 | Cross-family judge ensembles (≥ 2 provider families); report cross-family agreement — single-family panels self-agree spuriously | — |
| P5 | Domain-appropriate reliability thresholds; if unmeetable, develop the construct, don't lower the bar | alpha ≥ 0.67 exploratory / 0.70 safety / 0.80 deployment-gating (only 0.67 is literature-backed) |
| P6 | Validate generated task distributions: expert-rate a sample for construct fidelity; V1 = proportion valid | ~100-task sample |
| P7 | Stratify by demographic/linguistic group (≥ 2 populations) in calibration | — |
| P8 | IRR metric, threshold, and selection rationale as *required* reporting fields (EvalCards-style) | — |

The paper's replication repo (Apache-2.0,
`github.com/williamcaban/experiment-measurement-without-validity`) contains
runnable Krippendorff's alpha code — a usable external test oracle.

---

## 2. How the paper maps onto the harness

The three layers map 1:1 onto existing subsystems:

| Paper layer | Harness subsystem |
|---|---|
| V1 task generation | `/eval-dataset` (skill-analysis, synthetic-from-seeds, from-traces), `generate_synthetic.py`, `validate_eval.py` |
| V2 human-simulator | AskUserQuestion PreToolUse hook: 3-tier answering (`case_overrides` → LLM via `models.hook` → fallback), `skills/eval-run/scripts/tools.py`, `agent_eval/tools/interception.py` |
| V3 automated judgment | judges (`builtin`/`check`/LLM/`module`), `judges.samples`, `score.py`, thresholds/`detect_regressions`, pairwise comparison |

### Where the harness is already ahead of the paper's surveyed median

Worth stating plainly: nearly every proposal below is *computation and
reporting over data the harness already collects* — the position the paper's
82%-failure survey shows most pipelines are **not** in.

- **Raw rating matrices already persist.** `judges.samples` test-retest keeps
  every raw sample per case (`per_case[*][judge].stability.values`,
  `score.py:1197-1235`). The case × sample matrix every IRR coefficient needs
  exists on disk; IRR is a pure computation, no new data collection. (24% of
  surveyed papers never collect repeated ratings at all.)
- **Position-swap control on pairwise judging** — a win must survive the AB/BA
  order swap (`score.py:2131-2139`) plus repeated-run verdict stability.
  ELO-as-IRR was a named survey failure mode; the harness already does better.
- **score_range enforcement stricter than the paper asks**: off-scale outputs
  become error samples — dropped, never clamped — which incidentally produces
  exactly the missing-ratings condition that structurally forces Krippendorff's
  alpha over Fleiss' kappa.
- **A human criterion channel exists end-to-end**: `/eval-review` →
  `review.yaml` → `attach_feedback.py` pushes HUMAN-source assessments to
  MLflow. Both halves of the judge-vs-human join (the paper's single
  highest-leverage intervention, ~16× V_total in its Table 3) share a
  case-directory key today.
- **Role-separated models** (`models.skill/subagent/judge/hook`) map 1:1 onto
  the paper's pipeline components — the structural hook for Appendix B.4
  cross-provider decorrelation already exists in config.
- **A tiered, human-first simulator** (exact overrides → LLM → fallback) with
  full answer recoverability from stream-json transcripts.
- **Replication-aware statistics** (eval-anova: repeated-measures /
  mixed-effects ANOVA, Pareto; pingouin — including `intraclass_corr` — and
  statsmodels already installed via the `anova` extra).
- **V1 hygiene in synthetic generation**: category stamped derived-never-LLM-
  declared, mechanical answer-key/input separation, structural lint, `TODO_`
  placeholders as forced human touchpoints.

### Where the paper's critique genuinely lands

- **Every agreement number in the harness is raw exact-match percent
  agreement.** No chance correction anywhere (repo-wide grep for
  ICC/kappa/Krippendorff: zero hits). `stability.stable` requires identical
  samples, so a 4.0-vs-4.1 disagreement counts the same as 1-vs-5 — exactly
  the ordinal distortion the paper describes. Under the paper's own coding
  rules the harness's self-reporting would land in "incomplete" (the largest
  survey failure category).
- **The simulator is unmeasured, not unsound.** Which tier answered
  (override/LLM/fallback) is only ever emitted on unpersisted hook stderr;
  `tools.py:152-156` itself admits a fallback-answered run is downstream-
  indistinguishable from a calibrated one. Where human gold answers exist
  (`case_overrides`), they *short-circuit* the LLM tier — so calibration data
  is structurally never produced.
- **No task-validity check exists**: `annotations.expected_files` are never
  resolved, there is no null-agent probe, generation provenance is discarded
  (manifest built in memory then dropped), re-generation silently overwrites
  case dirs, and `validate_eval.py` samples only the first 3 cases.
- **Deterministic `check` judges ship with zero human validation** — they are
  the harness's analog of the substring grader measured at kappa = 0.049.
- **The default pipeline is all-Anthropic at every layer** (skill, judges,
  hook default) — the same-family correlated-failure pattern of Appendix B.4.

### Where the paper does NOT apply (honest exclusions)

- **P7 (demographic/dialect stratification): not applicable.** The simulated
  user answers structured multiple-choice configuration questions in
  code/skill/docs evals — there is no dialect surface, and inventing one would
  be cargo-culting. The paper itself scopes P7 to user-facing conversational
  evals. (If a conversational-flavor eval ever appears, dataset annotations can
  carry group labels and the simulator aggregation can stratify by them.)
- **P1's full form** (ICC ≥ 0.70 vs ≥ 50 real human sessions) is out of reach
  for skill/code evals; the implementable subset is the prescription's own
  fallback clause — *report that calibration is unvalidated* — plus
  answer-level calibration against the human-authored override tier.
  ICC(A,1) itself is structurally wrong for nominal option labels; per the
  paper's own P2 discipline the correct statistics here are nominal
  (percent agreement labeled uncorrected + Krippendorff alpha where the
  matrix supports it).
- **V1-as-proportion is coarse at harness scale** (3–20 cases vs the paper's
  100-task protocol): report it, don't gate on it.

---

## 3. Proposals (designed per layer, adversarially verified against the code)

Nine proposals were designed; the top two per layer were verified by
adversarial reviewers instructed to refute them by reading the code. **All six
verified proposals survived as "needs-changes"** — the core ideas are sound and
non-redundant, with concrete corrections listed. Unverified proposals (v1-3,
V2-3, V3-3) are marked as such.

### V1 — task generation

**v1-1: Deterministic dataset audit + persisted generation manifest** (M, high) — *verified, needs-changes*
New `skills/eval-dataset/scripts/audit_dataset.py` auditing the *entire*
dataset: reference resolution of `annotations.expected_files`, verbatim
contamination (answer-key content leaked into `input.yaml`), near-duplicate
detection (stdlib difflib/Jaccard), and composition (realized category×count vs
`generation.seeds`, difficulty skew tables). Companion: `generate_synthetic.py`
persists `manifest.yaml` (generator model, prompt sha256, context hash,
temperature, per-case provenance — currently built at lines 175-179 and
discarded) and refuses to overwrite case dirs without `--force`. Soft preflight
warning in `workspace.py` when the audit is missing/stale — implementing the
paper's "assess V1 before scoring" ordering as a nudge, not a gate.
Root-level audit/manifest files verified invisible to all five case-discovery
sites (workspace, collect, harbor/tasks, evalhub adapter, sync_dataset).
*Key corrections:* model `dataset.audit` in config.py with load-time validation
(or use CLI flags); reconcile `--force` with the documented resize flow in
`references/synthetic-generation.md`; use content hashes not dir mtimes for
staleness (POSIX dir mtime misses in-place edits); scope conditional-judge
branch analysis to annotations-only `if:` conditions; label reference
resolution as *necessary-not-sufficient* for answerability; whitelist the
sanctioned `answers.yaml` workspace copy.

**v1-2: Null-agent solvability probe** (S/M, high) — *verified, needs-changes*
The paper's flagship V1 evidence (38% null-agent pass rate) operationalized
through existing machinery: a ~60-line `NullRunner` in the `RUNNERS` registry
(`agent_eval/agent/__init__.py:9-14`), then the unchanged
workspace→execute→collect→score pipeline with `--agent null`. Any case where a
bool judge passes (or recomputed reward clears a threshold) on a do-nothing run
is non-discriminative. Doubles as a judge-vacuity detector (e.g.
`consulted_docs` returns a vacuous PASS when `expected_files` is absent —
`agent_eval/judges/process/consulted_docs.py:60`).
*Key corrections:* reward is **not** in `summary.yaml` — recompute via
`agent_eval.harbor.reward.compose_reward` over per-judge records; don't flag on
`reward > 0` (numeric rubric judges rarely award the exact floor — use bool
passes + a meaningful threshold); run judges with `--samples 3` so null-pass
findings on stochastic judges are majority-voted; label the statistic
"null-pass rate (joint task/judge non-discriminativeness, upper-bounds 1−V1)"
— under LLM judges it is not the paper's pure-V1 figure.

**v1-3: Construct-fidelity screen + recorded V1 estimate + human sign-off** (M, medium) — *unverified*
Direct P6 implementation: `screen_dataset.py` rates each generated case against
the seed prompt's own Validation Criteria + `generation.context` +
`dataset.schema` (structured tool-forced verdict; screener model defaults to
≠ generator model per Appendix B.4 decorrelation); `v1_estimate` recorded in
`dataset_audit.yaml` explicitly labeled **"LLM-proxied"** (the paper prescribes
domain experts); human sign-off walkthrough recorded; optional
`generation.validation.review: required` escalates the workspace preflight to
a refusal.

### V2 — simulator calibration

**V2-1: Answer-provenance ledger (`hook_answers.jsonl`) + builtin provenance judge** (M, high) — *verified, needs-changes*
One JSONL record per intercepted question: `{question, options, answer, tier:
override|llm|fallback, hook_model, match, error, ...}`, plus explicit
`disabled` records on both silent-disable paths. Collected per case, exposed to
judges as `outputs["hook_answers"]`, with a builtin bool judge
`process/simulator_provenance` gating via the existing thresholds machinery
(`min_pass_rate: 1.0`). This is the prerequisite for any V2 statistic — it
converts the simulator from *unmeasurable* to *measured*, and implements P1's
reporting clause.
*Key corrections:* anchor writes to `Path(__file__).parent` not CWD (in-repo
mode would otherwise pollute the user's repo and trip the cleanliness check at
`execute.py:724-739`); handle batch mode (run-level ledger, `collect.py` batch
branch + run-root fallback in `load_case_record`); distinguish
`hook_answers=None` (no ledger — judge must fail when interception is
configured and AskUserQuestion events exist) from `[]`; a pass certifies
*provenance coverage*, not calibration — say so in the judge docstring.

**V2-2: Shadow calibration vs human overrides + k-sample self-consistency** (M→L, high) — *verified, needs-changes*
`calibration: true` shadow-runs the LLM tier even when a `case_override`
answers (override still injected; LLM answer only logged with
gold/shadow/agree) — turning every human-authored override into a
simulator-vs-human calibration pair at near-zero cost. `samples: k` draws the
LLM tier k times (modal answer injected, all draws logged). A simulator
aggregation step computes tier distribution, fallback rate, gold agreement
(labeled *uncorrected*), self-consistency, and nominal Krippendorff alpha when
the matrix permits; lands as a `simulator:` block in `summary.yaml` + a report
card with the P1 banner "simulator calibration not validated" when zero pairs
exist. Reserved `thresholds.simulator` keys gate CI.
*Key corrections (load-bearing):* serialization must happen in
`generate_interception` as a post-load merge — `build_handlers` is bypassed
whenever a resolved `tool_handlers.yaml` exists, which is precisely the flow
carrying `case_overrides` (and in-repo mode uses a separate inline builder);
k-draws must sample at **default temperature** (the hook pins temperature=0,
making self-consistency vacuous by construction — a gate that always passes);
`case_overrides` are *agent-authored by default* (SKILL.md line 121), so a
machine-readable `source: human|agent` provenance marker is required — else
"gold agreement" measures LLM-vs-LLM while claiming human calibration, the
paper's Sec 5.3 trap verbatim; shadow draws must run held-out (context minus
`answers.yaml`) or be labeled "in-context agreement"; the reserved-key routing
through `detect_regressions` needs explicit handling at all three call sites.
*Ship the calibration-shadow slice first; defer `samples: k`.*

**V2-3: Cross-family shadow simulators + same-family warning at config load** (M, medium) — *unverified*
`models.hook_shadow`: up to 2 additional simulator models answering every
intercepted question (logged, never injected) — sidestepping the replay problem
that makes simulators otherwise incomparable (question sets are emergent per
run; `models.hook` is not an eval-anova matrix factor). Cross-simulator
agreement + panel family composition reported (the Seshadri 9pp finding made
observable). Config-load advisory when hook/judge/skill share one provider
family (Appendix B.4), silent on gateway aliases.

### V3 — judgment reliability + cross-cutting reporting

**V3-1: Chance-corrected IRR with structurally auto-selected metric + consequence-stratified gates** (M, high) — *verified, needs-changes*
New **pure-stdlib** `agent_eval/reliability.py` (constraint discovered:
`ensure_deps.py` never installs the anova extra into `.eval-venv`, so the
scoring path cannot use scipy/pingouin): Krippendorff's alpha
(nominal/ordinal/interval, missing-data tolerant), Fleiss' kappa, Cohen's
kappa, and `select_irr_metric(...)` encoding the paper's Figure 1 decision tree
with a rationale string. Wired into the existing cross-case stability block —
the case × sample matrix already exists in `stability.values`, with error
samples as missing ratings (per Appendix A.1, N samples of one model = a
varied-identity rater pool → alpha is *forced*, not a menu choice). Pairwise:
persist the currently-dropped `pref_ab`/`pref_ba` verdicts and a
swap-consistency rate. New `min_alpha` threshold key + optional per-judge
`consequence: exploratory|safety|gating` field injecting the paper's tier
defaults (0.67/0.70/0.80), warning at load when a consequence-tagged judge has
`samples: 1`. No Landis-Koch labels anywhere.
*Key corrections:* perfect-agreement degenerate matrices must **pass** the gate
(null-with-reason), not trip the unavailable-is-regression path; scope
`min_alpha` off the Harbor path (its aggregation carries no stability data —
would flag every run); resolve tier defaults at detection time, never by
mutating `config.thresholds` (harbor/run.py:329 reads it as a required-judges
set); errored pairwise verdicts are missing ratings, not a nominal category;
**label the coefficient "single-judge self-consistency alpha (upper bound on
IRR)"** — a self-consistent-but-biased judge sails through it (see P4 gap).

**V3-2: Human-anchored judge calibration via structured /eval-review verdicts** (M, high) — *verified, needs-changes*
Close the criterion-validity loop (the paper's ~16× highest-leverage
intervention): extend `review.yaml` with optional
`verdicts: {case_dir: {judge_name: value}}` elicited *before* revealing the
judge's rationale; a `calibration` subcommand joins them against
`summary.yaml per_case` on the shared case-directory key, selects the
structurally correct metric (Cohen's kappa is valid *here* — exactly 2 fixed
raters, complete matrix; alpha otherwise), persists per-judge
`{metric, value, n_cases, rationale}`, optional `min_human_agreement` gate.
Deterministic `check` judges are first-class targets.
*Key corrections:* merge agreement into `summary['judges'][name]` (the
top-level calibration block is invisible to `cmd_regression` as designed —
the gate would silently never fire); V3-1 is a hard prerequisite; record
contamination (`blind: true|false` — the user has typically already seen
report.html) and sampling basis (`selection: all|failures|manual` — kappa is
prevalence-sensitive, failure-skewed subsets bias it); label as "agreement
with a single human reviewer (n=X)"; exclude `null`-valued (if-skipped/errored)
judge entries from the join.

**V3-3: Validity & Reliability report section + cross-replication ICC in anova.json** (M, medium) — *unverified*
The harness analog of P8's EvalCard: an always-rendered report section with
per-judge rows carrying the P8 triple (metric, value+threshold, selection
rationale), three layer stanzas (V1: dataset provenance/audit presence; V2:
simulator status, "uncalibrated" when no data; V3: the IRR table), an honest
V_total statement — **the multiplicative frame with unmeasured layers named,
never a fabricated number** (per the paper's own Sec 10.4 caveat) — and the
same-family caveat. Cross-run: `pingouin.intraclass_corr` (already installed,
behind `ANOVA_AVAILABLE`) over the per-(case, replication) frame analyze.py
already builds, captioned *pipeline test-retest (agent+judge variance
confounded)*, not judge IRR.

---

## 4. Prescription coverage

| Prescription | Status | Via |
|---|---|---|
| P1 simulator calibration | ✅ adapted subset | V2-1 (reporting clause) + V2-2 (nominal agreement vs human-provenance overrides); full form (real human sessions) reported as unvalidated — the prescription's own fallback |
| P2 structural metric selection | ✅ covered | V3-1 `select_irr_metric` (Figure 1 tree as code) |
| P3 kappa + alpha dual reporting | ✅ covered | V3-1 (Fleiss when matrix complete, divergence surfaced) |
| P4 cross-family judge ensembles | ❌ **gap** | Only transpositions exist (V2-3 simulator shadows, V3-3 caveat banner). Incremental fix: `judges[].model` as a list via gateway aliases, fan the judge call out per model, cross-family alpha in reliability.py |
| P5 consequence-stratified thresholds | ✅ covered | V3-1 `consequence:` tiers + V2-2 simulator thresholds (0.70/0.80 documented as author-proposed) |
| P6 generated-task validation | ✅ covered | v1-1 + v1-2 + v1-3 stack (honest deviations labeled) |
| P7 demographic stratification | N/A | No dialect/demographic surface in multiple-choice config questions; paper scopes P7 to conversational evals |
| P8 required IRR reporting fields | ✅ covered | V3-3 validity section + V3-1 rationale strings (MLflow routing still missing — see below) |

## 5. Gaps the design round left open

Ranked by leverage:

1. **Cross-family judge ensembles (P4)** — without them every alpha the harness
   gates on is single-judge self-consistency, an *upper bound* on IRR, which
   partially undercuts the P5 tiers it feeds. Mostly plumbing, not
   architecture, given the roles-based models config + samples aggregation.
2. **Confidence intervals on reliability coefficients** — at 3–20 cases the
   interval *is* the finding; case-level bootstrap CI is stdlib-feasible.
   (The paper's exemplar reports kappa = 0.83 [0.81, 0.85].)
3. **Sec 10.2 instrument-clarity recipe as a cheap builtin** — 3 cross-family
   LLM raters on an N=20 subsample, 4-way alpha vs 0.67: "does this rubric even
   admit consistent application?" — distinct from and cheaper than judge
   reliability.
4. **Low-alpha response wiring** — the paper's prescribed response to an
   unmeetable threshold is construct development; `/eval-optimize` is the
   natural consumer of a low-alpha/low-human-agreement signal and nothing
   connects them.
5. **Execution-path parity** — reliability lands only on the local scoring
   path; Harbor aggregation carries no stability data and EvalHub is untouched,
   quietly contradicting "same eval.yaml across three execution paths".
6. **MLflow persistence of the validity block** — P8's institutionalization
   implies the fields flow to the cross-run system of record; `log_results.py`
   never sees them as specified.
7. **Test oracle** — cross-check `reliability.py` fixtures against the paper's
   Apache-2.0 replication repo.
8. **Displayed-precision restraint (Appendix B.5)** — two decimal places from a
   ~0.35-validity pipeline is precision the measurement doesn't support.

## 6. Suggested sequencing

1. **Phase 1 — measurement over existing data (no new data collection):**
   V3-1 (reliability.py + IRR on stability matrices) and V2-1 (provenance
   ledger). Both are pure additions; V3-1 is the prerequisite for most else.
2. **Phase 2 — dataset validity:** v1-2 (null-agent probe — smallest, most
   striking) then v1-1 (audit + manifest).
3. **Phase 3 — human anchoring:** V3-2 (review verdicts → judge calibration),
   the paper's highest-leverage intervention.
4. **Phase 4 — reporting:** V3-3 validity section + MLflow routing (gap 6).
5. **Phase 5 — cross-family:** P4 judge panels (gap 1), V2-2 calibration
   shadow slice, V2-3 simulator shadows.

Every phase reuses existing machinery (thresholds engine, samples aggregation,
_merge_summary, report rendering, anova stats path); none changes the eval.yaml
contract for existing users — all new surfaces are opt-in.
