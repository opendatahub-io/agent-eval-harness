# 013 — Cross-provider judges (judge provider decoupled from runner)

Status: proposed
Base: stacked on PR #213 (`feat: add Cursor as an eval runner`)
Supersedes: the minimal in-PR fix for PR #213 finding #1

## Problem

An LLM judge is an independent API call, but PR #213 tied the judge's provider to
the eval runner and to which credentials happen to be exported:

- `_load_llm_judge` computes `use_anthropic = anthropic_auth AND is_anthropic_model(judge_model)`
  and routes **every** non-Anthropic judge model through
  `_call_structured_judge_via_runner` → `RUNNERS[config.runner.type].execute(--model <judge_model>)`.
  A `gpt-4o` judge on a `claude-code` runner becomes `claude --model gpt-4o` and
  errors on every case (PR #213 review finding #1).
- The MLflow `make_judge` fallback is gated behind `is_anthropic_model`, so a real
  OpenAI id can never reach it.
- `is_anthropic_model` matches only `{opus,sonnet,haiku}` exactly, a `claude`
  substring, or an `anthropic/` prefix — so `sonnet-4-5` / `opus[1m]` misclassify
  as non-Anthropic and get shoved through the runner (finding #5).
- The `make_judge` scale instruction is gated on `if bounds:` (always truthy for a
  numeric judge) while enforcement only covers **declared**-`score_range` judges,
  so an undeclared-range numeric judge is told a false "1–5, rejected" rule that
  nothing enforces (finding #2).

Goal: **use any judge provider with any runner, and vice-versa.** An Anthropic
judge with a Cursor/Codex runner, and an OpenAI (or OpenAI-compatible) judge with a
claude-code/Codex runner.

## Why not `mlflow.genai.make_judge`

`make_judge` looks like a general multi-provider backend (`"<provider>:/<model>"`
URIs, gateway for OpenAI/Anthropic/Gemini/Azure/Bedrock). But empirically
(mlflow 3.12.0):

- It **rejects static instructions** — `"Instructions template must contain at
  least one variable (e.g. {{ inputs }}, {{ outputs }}, {{ trace }},
  {{ expectations }}, {{ conversation }})"`.
- It **owns templating** and resolves `{{ conversation }}` / `{{ outputs }}`
  itself, and the returned `Judge` is invoked as
  `judge(*, inputs, outputs, expectations, trace, session)` — there is no
  `conversation` kwarg; `{{ conversation }}` is resolved from a `trace`.

The harness renders its own prompt (`_render_jinja2_template`, `{{ conversation }}`)
and calls the scorer with `outputs=<case record>`. That does not line up with
make_judge's data model, so the PR #213 make_judge path does not actually feed the
judge the conversation. Making it work would mean fabricating an mlflow `Trace`
per case or a dummy-variable hack, and would forfeit the harness's tool-forced
structured output, in-harness bounds enforcement, rationale-first ordering, and
image evidence.

Decision: reach non-Anthropic providers with a **direct OpenAI-SDK structured
judge** that mirrors `_call_structured_judge`, keeping identical judge semantics
across providers. `openai>=1.70` is already a declared optional dependency. Native
Gemini/Bedrock and local models are reached through an OpenAI-compatible gateway
(`OPENAI_BASE_URL`) — which is how this project's LiteLLM/MLflow gateway is
deployed — or through the runner (`runner:/…`).

## Design

The judge model string carries the provider, LiteLLM/mlflow-style
`"<provider>:/<model>"`. Routing keys on the **judge model**, never on
`config.runner.type`.

### `agent_eval/prompt_backends.py`

- `split_model_uri(model) -> (provider|None, bare_model)` — splits on `":/"`.
- `is_anthropic_model(model)` — fixed: URI-aware (`provider == "anthropic"`), and
  for bare ids matches a `claude` substring, an `anthropic/` prefix, or a
  `opus`/`sonnet`/`haiku` **prefix** (so `sonnet-4-5`, `opus[1m]` classify
  correctly). Finding #5.
- `resolve_judge_backend(model) -> (backend, model_arg)` where
  `backend ∈ {"anthropic", "openai", "runner"}`:
  - `runner:/X` → `("runner", X)` (explicit opt-in for runner-managed ids such as
    Cursor's `gpt-5.4-medium`).
  - `anthropic:/X` or bare Claude → `("anthropic", X)`.
  - `openai:/X`, a bare OpenAI-family id, or **any other bare id** (a
    custom/gateway model name) → `("openai", X)`. The OpenAI SDK also serves
    OpenAI-compatible gateways via `OPENAI_BASE_URL`, so an arbitrary
    gateway-served model id routes here rather than failing.
  - any other explicit `provider:/X` (e.g. `gemini:/…`) → `ValueError` guiding to
    `openai:/` + `OPENAI_BASE_URL`, or `runner:/X`. Only an explicit unsupported
    provider fails; a bare id never does.

### `skills/eval-run/scripts/score.py`

- New `_get_openai_client()` — lazy `openai` import with an actionable error;
  honors `OPENAI_API_KEY` and `OPENAI_BASE_URL`.
- New `_call_structured_judge_openai(prompt, model, feedback_type, images, bounds)`
  — mirrors `_call_structured_judge`: same `submit_evaluation` / `submit_score`
  tool (converted to OpenAI function-tool shape via `_to_openai_tool`), same
  system prompt, forced `tool_choice`, `_coerce_number`, and the same
  `_parse_bool_response` / `_parse_score_response` text fallback. Images inline as
  `image_url` data URIs.
- `_load_llm_judge`: replace the `use_anthropic`/`make_judge` block with
  `backend, model_arg = resolve_judge_backend(judge_model)` and dispatch the
  per-case scorer to anthropic / openai / runner. Remove the make_judge branch.
- `_make_builtin_scorer` (llm): same three-way dispatch.
- Finding #2: the make_judge scale block is gone; the remaining backends already
  state the scale only through `_numeric_bounds`/`_score_judge_tool`, and
  enforcement stays gated on a **declared** `score_range` (`judge_bounds`,
  unchanged). Stated ⟺ enforced.

### `skills/eval-dataset/scripts/generate_synthetic.py`

- Use `split_model_uri` + fixed `is_anthropic_model`: strip the provider prefix
  before the Anthropic SDK call; keep the runner fallback for non-Anthropic
  (`runner:/…` and bare runner ids). An explicit `openai:/…` generation model
  raises a clear "not supported; use a Claude model or `runner:/…`" (OpenAI-native
  generation is out of scope — generation is free-form, not judge-shaped).

### `scripts/ensure_deps.py`

- When LLM judges are configured, also install `openai>=1.70` if any judge model
  (`models.judge` or a per-judge `model:`) looks non-Anthropic. Best-effort in the
  YAML path; the runtime error in `_get_openai_client` covers a miss.

### Config validation

- `EvalConfig.from_yaml` calls `resolve_judge_backend` on each resolvable judge
  model and surfaces `ValueError` as a config-load error (fail fast on an
  ambiguous/unsupported provider). Skipped when the model is only known via env at
  run time.

## Behavior changes / migration

- A **Claude** judge with no Anthropic credentials no longer silently falls back
  to OpenAI's default model via make_judge; it errors. To judge with OpenAI, set
  `models.judge: openai:/gpt-4o` explicitly.
- A **bare non-Claude** judge model now defaults to the OpenAI/gateway backend
  (previously #213 forced it through the runner). A runner-managed id such as
  Cursor's `gpt-5.4-medium` must therefore be written `runner:/gpt-5.4-medium` to
  grade through the configured runner.
- Non-Anthropic judges require the `openai` package in `.eval-venv`.

## Config examples

```yaml
models:
  judge: openai:/gpt-4o          # OpenAI (or OPENAI_BASE_URL gateway); any runner
  # judge: anthropic:/claude-sonnet-4-5
  # judge: sonnet                # bare Claude alias, direct Anthropic SDK
  # judge: runner:/gpt-5.4-medium  # explicit: run through the configured runner
```

## Tests

- `tests/test_prompt_backends.py`: `split_model_uri`, `is_anthropic_model`
  (incl. `sonnet-4-5`, `opus[1m]`, `anthropic:/…`, `openai:/…`),
  `resolve_judge_backend` (every branch incl. the two `ValueError`s).
- Judge dispatch: `_load_llm_judge` / `_make_builtin_scorer` route to the right
  `_call_*` per backend (monkeypatched).
- `_call_structured_judge_openai`: bool + numeric via a stubbed OpenAI client
  returning a `tool_calls` arguments payload, and the text fallback.
- `generate_synthetic`: URI stripping + backend selection.

## Known limitations

- `_call_structured_judge_openai` sends `max_tokens` (accepted by gpt-4o-class
  models and OpenAI-compatible gateways). OpenAI reasoning models (o1/o3) on the
  chat-completions endpoint expect `max_completion_tokens` and may reject the
  call; use a gpt-class judge or a gateway that normalizes the parameter.
- Image evidence reaches OpenAI judges as vision `image_url` blocks (works on
  vision models) and runner judges as staged files; make_judge-style non-vision
  providers do not see images.

## Out of scope (future)

- Native Gemini/Bedrock judge SDKs (today: via OpenAI-compatible gateway).
- OpenAI-native synthetic generation.
- Per-judge `backend:` override (the model URI is the contract).
