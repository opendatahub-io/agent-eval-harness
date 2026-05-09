# Data Pipeline Reference

How data flows from dataset cases through execution, collection, and scoring.

## 1. Dataset → Workspace

**Input**: `dataset.path` from eval.yaml pointing to a directory of case subdirectories.

**What workspace.py does**:
- Reads each case directory. Finds the first `.yaml`/`.yml`/`.json` file and loads its full content.
- Builds `batch.yaml` — a list of one entry per case, each entry being the full parsed content of the input file. No field extraction — the entire input file content is included.
- Builds `case_order.yaml` — maps position to case ID (`[{case_id: "case-001-name"}, ...]`)
- Creates output directories from `outputs[].path` in eval.yaml
- Symlinks project resources (`.claude/`, `CLAUDE.md`, `scripts/`, `skills/`, `.context/`)
- If `inputs.tools` configured, generates `tool_handlers.yaml` and `.claude/settings.json` with hooks

**Workspace structure**:
```
/tmp/agent-eval/{run-id}/
  batch.yaml              # Full input data per case
  case_order.yaml         # Position → case ID mapping
  {output_dirs}/          # Empty dirs for skill outputs
  .claude/settings.json   # Generated hook config (if inputs.tools)
  hooks/tools.py          # Hook script (if inputs.tools)
  tool_handlers.yaml      # Handler config (if inputs.tools)
  scripts/ → symlink
  .claude/ → symlink (or generated)
  CLAUDE.md → symlink
  skills/ → symlink
```

### Case Mode (execution.mode: case)

When `execution.mode` is `case` (default), workspace.py creates a separate workspace per case:

```
/tmp/agent-eval/{run-id}/
  case_order.yaml         # [{case_id: "case-001-name"}, ...]
  cases/
    case-001-name/
      input.yaml          # Copied from dataset
      strategy.md         # Copied from dataset (companion files)
      answers.yaml        # Copied from dataset (if present)
      {output_dirs}/      # Empty dirs for skill outputs
      .claude/settings.json  # Generated (hooks + permissions)
      subagents/           # SubagentStop hook target
      scripts/ → symlink
      CLAUDE.md → symlink
      skills/ → symlink
    case-002-name/
      ...
```

Each case gets ALL files from the dataset case directory (not just input.yaml), plus symlinked project resources and hooks. The skill runs in the case workspace as its working directory.

## 2. Workspace → Execution

**What execute.py does**:
- Invokes the skill via the configured runner (e.g., `claude --print`)
- Passes `batch.yaml` content as the skill prompt via stdin
- Captures stdout (stream-json events) and stderr
- Writes `stdout.log` and `stderr.log` to `$AGENT_EVAL_RUNS_DIR/{id}/`
- Parses the final `result` event for token usage and cost
- Writes `run_result.json` with exit_code, duration_s, token_usage, cost_usd

**What the skill sees (batch mode)**:
- Working directory: the workspace
- Input: batch.yaml content (via skill prompt)
- Output directories: created and ready
- Hooks: if configured, AskUserQuestion gets auto-answered, external services get checked

**What the skill sees (case mode)**:
- Working directory: the case workspace (`workspace/cases/{case_id}/`)
- Input: case-specific arguments resolved from `execution.arguments` template + all case files on disk
- Output directories: created and ready
- Hooks: per-case hooks with SubagentStop for transcript capture

**Case mode execution**:
- execute.py iterates `case_order.yaml`, invoking the skill once per case
- When `execution.parallelism` > 1 (or `--parallelism` CLI), cases run concurrently via thread pool; each case gets a per-case log prefix (e.g., `eval:case-003`)
- Each case gets its own stdout.log, stderr.log, and subagent transcripts
- Results are saved to `$AGENT_EVAL_RUNS_DIR/{id}/cases/{case_id}/stdout.log`
- run_result.json includes `execution_mode: "case"`, `per_case` breakdown, `duration_s` (sum of per-case durations), and `wall_clock_s` (actual elapsed time)

## 3. Execution → Collection

**What collect.py does**:
- Reads `case_order.yaml` to know which case is at which position
- For each `outputs[].path` in eval.yaml, scans the workspace output directory
- Groups files by detected prefix pattern (e.g., "RFE-001", "RFE-002") or by position
- Copies each group's files to `$AGENT_EVAL_RUNS_DIR/{id}/cases/{case-id}/{output-path}/`
- Writes `collection.json` with per-case artifact counts

**Per-case output structure**:
```
$AGENT_EVAL_RUNS_DIR/{id}/
  run_result.json         # Execution metadata
  stdout.log              # Full stdout
  stderr.log              # Full stderr
  collection.json         # Per-case artifact counts
  cases/
    case-001-name/
      artifacts/          # Files from outputs[0].path
        RFE-001-slug.md
      artifacts/reviews/  # Files from outputs[1].path
        RFE-001-review.md
      _modified/          # In-place edits (auto-detected via git diff)
        source.md
    case-002-name/
      artifacts/
        RFE-002-slug.md
      _modified/
        source.md
```

### In-place file edits

Skills that modify input files using the Edit tool (rather than writing to an output directory) are handled automatically. `workspace.py` creates a git commit of the initial workspace state before execution. After execution, `collect.py` runs `git diff HEAD` to detect modified files and copies them to a `_modified/` directory under each case's run output. No extra `outputs` config is needed.

## 4. Collection → Scoring

**What score.py's `load_case_record()` builds for each case**:

```python
{
    # --- File artifacts (from outputs with path) ---
    "files": {
        "artifacts/RFE-001-slug.md": "<full file content>",
        "artifacts/reviews/RFE-001-review.md": "<full file content>",
        # In-place edits (auto-collected via git diff)
        "_modified/source.md": "<edited file content>",
    },
    "artifacts_content": "<content of first file in artifacts/>",
    "artifacts_file": "/path/to/artifacts/RFE-001-slug.md",
    "reviews_content": "<content of first file in reviews/>",

    # --- In-place edits (convenience access) ---
    # Same content as files["_modified/..."] but keyed by filename only.
    # Use this for direct access: outputs.get("modified_files", {}).get("source.md")
    "modified_files": {
        "source.md": "<edited file content>",
    },

    # --- Structured events (if traces.events enabled, default: true) ---
    # Flat list of typed event dicts parsed from stdout.log JSONL.
    # Event types: assistant, tool_result, system, result.
    # Subagent events carry parent_tool_use_id and agent_id tags.
    # Tool results include content (capped at traces.event_result_cap).
    "events": [
        {"type": "assistant", "text": "Let me check...", "tools": [...], "timestamp": "..."},
        {"type": "tool_result", "tool_use_id": "tu_001", "tool_name": "Read",
         "content": "file contents", "is_error": False, "timestamp": "..."},
        {"type": "result", "cost_usd": 0.15, "num_turns": 12, "timestamp": None},
    ],

    # --- Tool calls (from outputs with tool, derived from events) ---
    "tool_calls": [
        {
            "name": "mcp__atlassian__create_issue",
            "input": {"title": "...", "description": "..."}
        }
    ],

    # --- Execution metadata (if traces.metrics enabled) ---
    "exit_code": 0,
    "duration_s": 45.2,
    "token_usage": {"input": 5000, "output": 2000},
    "cost_usd": 0.15,
    "num_turns": 12,

    # --- Logs (if traces.stderr enabled) ---
    "stderr": "<full stderr.log content>",

    # --- Annotations (from dataset case directory) ---
    "annotations": {"expected_voice": "technical", "difficulty": "easy"},

    # --- Context ---
    "case_dir": "/absolute/path/to/case"
}
```

**Note on `{{ outputs }}` in LLM judges**: The `{{ outputs }}` template variable renders ALL entries in `files`, including `_modified/` entries. Each file appears as a markdown section with its path as the heading. Modified files will appear as `### _modified/source.md` followed by the edited content.

**Note on `{{ stdout }}` in LLM judges**: The `{{ stdout }}` template variable extracts assistant conversation text from the JSONL stdout log. It filters for top-level assistant text blocks (skipping subagent messages, tool calls, system events, and user input), then concatenates the extracted text. For non-JSONL stdout (e.g., from non-Claude runners), the raw content is passed through. If stdout is empty or missing, it renders as `(no stdout captured)`. Use `{{ stdout }}` for skills that produce their primary output via conversation text rather than file artifacts.

**Key naming convention for convenience keys**: for an output with `path: "artifacts/rfe-tasks"`, the convenience key is `rfe-tasks_content` (the last directory component + `_content`). For `path: "."`, the key is `main_content`.

## 5. Scoring → Judges

### Three judge types

**Inline check** (`check` field in eval.yaml):
- Python snippet wrapped in a function by `score.py`
- Receives the full record dict as `outputs` parameter
- Must return `(bool, str)` — pass/fail + rationale
- Example accessing file content: `outputs["artifacts_content"]`
- Example accessing traces: `outputs.get("cost_usd", 0)`
- Example accessing tool calls: `outputs.get("tool_calls", [])`

**LLM judge** (`prompt` or `prompt_file` field):
- Created via `mlflow.genai.judges.make_judge()`
- Receives the record as `outputs` kwarg
- `context` files are appended to the prompt
- `feedback_type` is optional — MLflow infers from response

**External code judge** (`module` + `function` field):
- Imported via `importlib` from the project
- Receives the record as `outputs` kwarg
- Can return `Feedback` object, `(bool, str)` tuple, or primitive

### How aggregation works

For each judge across all cases:
- **Boolean values**: aggregated as `pass_rate` (fraction True)
- **Numeric values**: aggregated as `mean`
- Results written to `summary.yaml` with `per_case` and `aggregated` sections

## Traces Configuration

| `traces` field | What it controls | Where data comes from | What judges access |
|----------------|------------------|-----------------------|-------------------|
| `stdout: true` | Keep raw stdout.log on disk | `stdout.log` in case/run dir | Debugging only (not loaded into record) |
| `stderr: true` | Capture full stderr | `stderr.log` in case/run dir | `outputs["stderr"]` |
| `events: true` | Parse JSONL into events.json | Parsed from stdout.log at collection time | `outputs["events"]` (list of event dicts) |
| `event_result_cap: 50000` | Max chars per tool result/input | Applied during parsing | Truncated content has `truncated: true` and `original_length` |
| `metrics: true` | Capture execution metadata | `run_result.json` | `outputs["exit_code"]`, `["duration_s"]`, `["cost_usd"]`, `["num_turns"]`, `["token_usage"]` |

Note: `events.json` is generated at collection time by `collect.py` and includes merged subagent transcripts from `subagents/*.jsonl`. Tool calls in `outputs["tool_calls"]` are derived from events.

### Important: stdout format for Claude Code runner

When `runner.type: claude-code`, `outputs["stdout"]` contains the **raw JSONL event stream** from `claude --print`, not plain text. Each line is a JSON object with `type` (`user`, `assistant`, `system`, `result`). Assistant text is in objects like:

```json
{"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
```

**For check judges**: use `outputs["events"]` to access structured events. Filter by type and iterate:

```python
# Get all root-level assistant text
texts = [e["text"] for e in outputs.get("events", [])
         if e["type"] == "assistant" and not e.get("parent_tool_use_id") and e.get("text")]

# Get tool call sequence
tools = []
for e in outputs.get("events", []):
    if e["type"] == "assistant" and not e.get("parent_tool_use_id"):
        tools.extend(t["name"] for t in e.get("tools", []))

# Separate root from subagent events
root_events = [e for e in outputs.get("events", []) if not e.get("parent_tool_use_id")]
sub_events = [e for e in outputs.get("events", []) if e.get("parent_tool_use_id")]
```

**For LLM judges**: use `{{ conversation }}` to render root-level assistant text (excludes subagent text). Use `{{ outputs }}` for file artifacts and modified files. If the skill edits files in-place, the rewritten content appears in `{{ outputs }}` via the `_modified/` collection.
