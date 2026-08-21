# Tool Interception Reference

This documents how `inputs.tools` handlers are resolved and executed during headless eval runs.

## Flow

1. **eval.yaml** defines handlers with `match` (what to intercept) and `prompt` (how to handle)
2. **workspace.py** extracts basic tool name patterns from `match` text, writes `tool_handlers.yaml`, and includes `hook_model` from `models.hook`
3. **eval-run agent (Step 3b)** reads `tool_handlers.yaml`, interprets the `prompt` field, and adds concrete runtime checks
4. **tools.py** (PreToolUse hook) executes the checks at runtime. AskUserQuestion uses a 3-tier resolution: exact `case_overrides` match → LLM call (using `hook_model`) → first-option fallback. All other tool checks are deterministic.

## tool_handlers.yaml Format

```yaml
handlers:
  # AskUserQuestion handler
  - match: "Questions asked to the user via AskUserQuestion."
    patterns: ["AskUserQuestion"]
    prompt: |
      Answer based on the test case context in input.yaml and answers.yaml.
      Use answers.yaml guidance for domain-specific decisions.
      Default: pick the first option or answer "yes" for confirmations.

  # External service handler (Jira via MCP AND scripts)
  - match: "Any Jira interaction via MCP tools or scripts."
    patterns: ["Bash", "mcp__atlassian__*"]
    input_filters: ["jira", "JIRA_SERVER", "jira-python"]
    env_checks:
      JIRA_SERVER:
        must_contain: ["localhost", "emulator", "127.0.0.1", "test"]
    prompt: "Only allow if JIRA_SERVER points to a test instance."

# Model for LLM-based AskUserQuestion answering (from models.hook)
hook_model: claude-haiku-4-5-20251001

# Per-case exact-match answer overrides (optional — LLM answering is preferred)
case_overrides:
  "What priority should this have?": "Normal"
```

### Fields

| Field | Set by | Used by | Purpose |
|-------|--------|---------|---------|
| `match` | workspace.py (from eval.yaml) | eval-run agent | Natural language description of what to intercept |
| `patterns` | workspace.py (heuristic extraction) | tools.py | Tool name patterns for matching (exact or glob) |
| `input_filters` | eval-run agent (Step 3b) | tools.py | Regex patterns to match Bash command content. When present with "Bash" in patterns, BOTH must match. |
| `env_checks` | eval-run agent (Step 3b) | tools.py | Env var validation. Each key is a var name, `must_contain` lists required substrings. All must pass for the tool call to be allowed. |
| `prompt` | workspace.py (from eval.yaml) | eval-run agent, tools.py | Natural language instruction — the agent reads this to generate concrete checks. Also passed to the LLM answerer as context for AskUserQuestion. |
| `hook_model` | workspace.py (from models.hook) | tools.py | Model ID for LLM-based AskUserQuestion answering. Defaults to `claude-haiku-4-5-20251001`. |
| `case_overrides` | eval-run agent (optional) | tools.py | Question → answer map for AskUserQuestion. Exact-match tier — checked before LLM and fallback. |

## How tools.py Handles Each Tool Type

### AskUserQuestion

1. Match by pattern: `patterns: ["AskUserQuestion"]`
2. **Tier 1 — exact match**: look up the question text in `case_overrides`
3. **Tier 2 — LLM call**: if no exact match and options are available, call the `hook_model` with the question, options, handler `prompt`, and case context (`input.yaml` + `answers.yaml` from CWD). The LLM picks the best option based on context. **Note**: case files are sent to the LLM API — do not put secrets, credentials, or PII in `input.yaml` or `answers.yaml`.
4. **Tier 3 — fallback**: pick the first option, or "yes"
5. Return `permissionDecision: "allow"` with `updatedInput` containing answers

### MCP Tools (e.g., mcp__atlassian__*)

1. Match by pattern: `mcp__atlassian__*` matches any tool starting with `mcp__atlassian__`
2. If `env_checks` present: validate each env var. All must pass → allow. Any fails → deny with reason.
3. If no env_checks: deny by default (matched but no check defined)

### Bash Commands (Script-based interception)

1. Match requires BOTH: "Bash" in `patterns` AND command matches at least one `input_filters` regex
2. `input_filters: ["jira", "JIRA_SERVER"]` means the Bash command must contain "jira" or "JIRA_SERVER" (case-insensitive)
3. A `ls -la` command won't match even though "Bash" is in patterns
4. If matched and `env_checks` present: same env validation as MCP tools
5. **A handler with `Bash` in patterns but no `input_filters` is treated as misconfigured**: the hook logs a stderr warning and skips it (pass-through). Without filters, every Bash call would otherwise hit the default-deny in `main()` and the skill could not run.

### Unmatched Tools

Tools with no matching handler pass through (exit 0, no interception).

### How tools.py finds tool_handlers.yaml

The hook resolves `tool_handlers.yaml` in order: CWD-relative first (case and
batch mode run the agent inside the workspace where the file is written —
backward compatible), then relative to the script's own location
(`Path(__file__)`: the `hooks/` dir itself, then its parent — the workspace
root where every generation site writes the file). The `__file__` fallback
repairs **in-repo mode**, where the agent's CWD is the user's repo root and
interception was previously silently pass-through. When every lookup misses,
the hook writes a `disabled` ledger record (see below) and passes through.

## Answer provenance ledger (hook_answers.jsonl)

Every intercepted AskUserQuestion question is recorded — one JSON object per
line — to `hook_answers.jsonl` **next to the interceptor script itself**
(`Path(__file__).parent`, never CWD, so in-repo runs cannot pollute the user's
repo):

- **case / in-repo mode**: `<case_ws>/hooks/hook_answers.jsonl`
- **batch mode**: `<workspace>/hooks/hook_answers.jsonl` (one shared
  interceptor → run-level, unattributed)

All ledger writes are best-effort appends inside the hook's never-crash
envelope: a logging failure can never break interception.

### Record schema (FROZEN)

| Field | Type | Present | Meaning |
|-------|------|---------|---------|
| `ts` | str | always | UTC ISO-8601 timestamp |
| `question` | str | answered records | The question text |
| `options` | list[str] | answered records | Option labels offered |
| `answer` | str | answered records | The answer injected into `updatedInput` |
| `tier` | str | always | `override` \| `llm` \| `fallback` \| `disabled` |
| `hook_model` | str | when an LLM attempt was made | Model used (or attempted) for tier-2 answering |
| `match` | str\|null | when an LLM attempt was made | `exact` \| `fuzzy` \| `null` (reply matched no option) |
| `llm_raw` | str | only when the LLM reply was rejected/unparseable | Truncated raw reply (≤500 chars) |
| `error` | str | only on API failure | Truncated exception text (≤500 chars) |
| `temperature_stripped` | bool | when the strip-retry fired | The `temperature` param was rejected and retried without |
| `reason` | str | `disabled` records | `pyyaml-missing` \| `tool-handlers-missing` |
| `cwd` | str | `tool-handlers-missing` records | The hook's CWD (surfaces resolution problems) |

**Reserved fields** (documented now, emitted by a later commit — do not
repurpose): `source: human|agent` (case_overrides provenance),
`calibration{gold, shadow, agree, held_out, error, decoding{temperature,
temperature_stripped}}` (shadow-run gold agreement), `shadows[{model, answer,
error, held_out}]` (cross-family shadow simulators).

### Tier semantics

- `override` — exact `case_overrides` match: deterministic, case-specific.
- `llm` — the `hook_model` picked an option from case context. Reported, not
  validated: an `llm` record is *provenance*, not calibration.
- `fallback` — no override and no usable LLM answer: the agent under test was
  handed an **arbitrary** answer (first option or "yes"). The record carries
  the failed LLM attempt's `error`/`llm_raw` when one was made.
- `disabled` — interception silently disabled for this call (PyYAML not
  importable, or `tool_handlers.yaml` not found by any lookup). Written on
  both silent-disable paths so pass-through is never invisible.

### Collection and judge exposure

`collect.py` copies the ledger to `cases/<case_id>/hook_answers.jsonl` (case
and in-repo mode) or to the **run root** `runs/<id>/hook_answers.jsonl`
(batch mode — run-level, unattributed). `hooks/` is in `_HARNESS_PATHS`, so
the ledger never leaks into `_modified/` artifacts.

`score.py`'s `load_case_record()` exposes:

- `outputs["hook_answers"]` — a list of records (possibly empty), or **None**
  when no ledger was found. The None-vs-`[]` distinction is load-bearing:
  None + AskUserQuestion calls in the trace = unrecorded simulation. Lookup
  order: `cases/<case_id>/hook_answers.jsonl` →
  `case_dir/hooks/hook_answers.jsonl` (in-container Harbor scoring, where
  case_dir IS the agent workspace) → run-root `hook_answers.jsonl` (batch).
- `outputs["hook_answers_scope"]` — `case` | `run` | None (`run` when only
  the run-root ledger matched).
- `outputs["interception_configured"]` — `bool(config.inputs.tools)`.

### Gating recipe: the simulator_provenance judge

```yaml
judges:
  - name: simulator_provenance
    builtin: process/simulator_provenance   # or bare: simulator_provenance
thresholds:
  simulator_provenance:
    min_pass_rate: 1.0   # no case may run on fallback/disabled/unrecorded answers
```

The judge fails on any `fallback`/`disabled`/`error` record, on partial
coverage (fewer recorded answers than questions in the trace, case scope
only), and on the fail-open trap: interception configured + AskUserQuestion
in the trace + no ledger. A pass certifies **answer-provenance coverage
only** — `tier: llm` answers are reported, not validated against human
answers. This is *not* simulator calibration.

> **Reward-gate note**: `simulator_provenance` is a bool judge, so adding it
> gates the **default reward composition** — a fallback-answered case scores
> 0. Teams using eval-anova composites or RL rewards should either exclude it
> via the `reward:` section (`judge:`/`formula:` naming other judges) or
> accept the gate deliberately.

### Blind spots and notes

- **External kill**: a PreToolUse hook killed from outside (crash, OOM,
  external timeout) is treated as pass-through by the CLI and cannot write a
  record from inside the dying process. The judge's missing-ledger check is
  the detectable signature; a kill *after* some questions were recorded is
  only caught by the coverage count.
- Deny-path reason text is not ledgered — it remains recoverable from the
  transcript (the ledger scope is AskUserQuestion answering + disabled
  records).
- The ledger lives in the agent-visible workspace under `hooks/`. The answers
  were already delivered to the agent in-band, so this leaks nothing new;
  optionally deny `Read(**/hook_answers.jsonl)` for hygiene.

## What eval-run Agent Does in Step 3b

Read each handler in `tool_handlers.yaml` and resolve the `prompt` into concrete fields:

1. **For AskUserQuestion**: The LLM answerer handles most questions automatically using the handler `prompt` + case context. Only add `case_overrides` entries for questions that need exact deterministic answers. The `answers.yaml` file in each case directory provides guidance the LLM reads at runtime — you don't need to load it into `case_overrides`.

2. **For service interception** (Jira, Slack, etc.): Read the prompt and add:
   - `env_checks`: which env vars to validate and what values indicate test instances
   - `input_filters`: regex patterns to match relevant Bash commands

3. **For blocking**: configure an explicit matcher — don't rely on default-deny for Bash. For Bash, add `input_filters` matching the commands to block; a handler with `Bash` in `patterns` but no `input_filters` is treated as misconfigured and skipped (pass-through, stderr warning only, per Line 75), so the intended block silently fails. For MCP/other tools, a matching pattern (exact or glob) with no `env_checks` is denied by default — the pattern alone suffices (Line 67). Verify the intended call is actually denied.

### Example Resolution

**Input** (from workspace.py):
```yaml
- match: "Any Jira interaction via MCP or scripts calling the Jira API."
  patterns: ["Bash", "mcp__atlassian__*"]
  prompt: "Only allow if JIRA_SERVER points to a test instance or emulator."
```

**After eval-run agent resolves** (Step 3b):
```yaml
- match: "Any Jira interaction via MCP or scripts calling the Jira API."
  patterns: ["Bash", "mcp__atlassian__*"]
  input_filters: ["jira", "JIRA_SERVER", "atlassian", "jira-python"]
  env_checks:
    JIRA_SERVER:
      must_contain: ["localhost", "emulator", "127.0.0.1", "test", "staging"]
  prompt: "Only allow if JIRA_SERVER points to a test instance or emulator."
```

## How Judges Access Tool Call Data

`score.py`'s `load_case_record()` extracts tool calls from the stdout stream-json events. For each `outputs` entry with a `tool:` field, matching tool calls are added to `outputs["tool_calls"]`:

```python
# What judges receive
{
    "tool_calls": [
        {
            "name": "mcp__atlassian__create_issue",
            "input": {"title": "...", "description": "..."}
        }
    ],
    "files": {...},
    "annotations": {...},
    "exit_code": 0,
    "cost_usd": 0.15,
    ...
}
```

Judges can then check tool calls:
```yaml
- name: jira_created
  check: |
    calls = outputs.get("tool_calls", [])
    jira = [c for c in calls if "create_issue" in c.get("name", "")]
    if not jira:
        return False, "No Jira issue created"
    return True, f"Created: {jira[0]['input'].get('title', '?')}"
```
