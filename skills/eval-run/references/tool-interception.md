# Tool Interception Reference

This documents how `inputs.tools` handlers are resolved and executed during headless eval runs.

## Flow

1. **eval.yaml** defines handlers with `match` (what to intercept) and `prompt` (how to handle)
2. **workspace.py** extracts basic tool name patterns from `match` text, writes `tool_handlers.yaml` and `hooks/tools.py` to the harness directory (a sibling of the workspace at `<run-id>._harness/`), and includes `hook_model` from `models.hook`. In case mode, each case gets its own harness subdirectory (`<harness>/<case-id>/`).
3. **eval-run agent (Step 3b)** reads `<harness>/tool_handlers.yaml`, interprets the `prompt` field, and adds concrete runtime checks
4. **tools.py** (PreToolUse hook, invoked with `--config <harness>/tool_handlers.yaml`) executes the checks at runtime. AskUserQuestion uses a 3-tier resolution: exact `case_overrides` match → LLM call (using `hook_model`, reading `answers.yaml` from `--case-dir`) → first-option fallback. All other tool checks are deterministic.

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
3. **Tier 2 — LLM call**: if no exact match and options are available, call the `hook_model` with the question, options, handler `prompt`, and case context (`input.yaml` from CWD + `answers.yaml` from `--case-dir` or CWD). The LLM picks the best option based on context. **Note**: case files are sent to the LLM API — do not put secrets, credentials, or PII in `input.yaml` or `answers.yaml`.
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

## What eval-run Agent Does in Step 3b

Read each handler in `tool_handlers.yaml` and resolve the `prompt` into concrete fields:

1. **For AskUserQuestion**: The LLM answerer handles most questions automatically using the handler `prompt` + case context. Only add `case_overrides` entries for questions that need exact deterministic answers. The `answers.yaml` file in each case directory provides guidance the LLM reads at runtime — you don't need to load it into `case_overrides`.

2. **For service interception** (Jira, Slack, etc.): Read the prompt and add:
   - `env_checks`: which env vars to validate and what values indicate test instances
   - `input_filters`: regex patterns to match relevant Bash commands

3. **For blocking**: If the prompt says "block" or "deny", the default deny behavior is sufficient — just ensure the patterns match correctly.

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
