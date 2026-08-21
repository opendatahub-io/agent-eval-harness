#!/usr/bin/env python3
"""PreToolUse hook script for intercepting tools during headless eval.

Reads tool_handlers.yaml from the workspace. Handlers contain resolved
patterns and runtime checks (from natural language `match` and `prompt`
in eval.yaml, resolved by eval-run at workspace setup time).

Supports:
- Auto-answering AskUserQuestion via per-case overrides
- Blocking tools based on env var checks (e.g., production Jira)
- Filtering Bash commands by content patterns
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Answer-provenance ledger. Anchored to this script's own directory — the
# hook is copied to <case_ws>/hooks/tools.py in case AND in-repo mode, and
# <workspace>/hooks/tools.py in batch mode, but the hook's CWD varies (it is
# the user's repo root in in-repo mode), so a CWD-relative path would pollute
# the repo. One JSON object per line; O_APPEND single-line writes are
# interleaving-safe for practical line sizes when cases run in parallel.
_LEDGER = Path(__file__).resolve().parent / "hook_answers.jsonl"


def _log_answer(record):
    """Append one provenance record to the ledger, best-effort.

    Never raises: a crashed PreToolUse hook is treated as pass-through by
    the CLI, silently disabling ALL interception — a logging failure must
    never break interception (uses only json/Path/datetime, so it is safe
    to call even from the PyYAML-missing path below).
    """
    try:
        record.setdefault(
            "ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with open(_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


# This script is COPIED into <workspace>/hooks/ and executed by the Claude
# Code hook runner as a bare `python3 .../tools.py` — agent_eval is usually
# not importable there, and a PreToolUse hook that crashes is treated as
# pass-through by the CLI, which silently disables ALL interception (observed
# on a real eval run). The bootstrap is only a venv-activation convenience:
# use it when available, never require it.
try:
    import agent_eval._bootstrap  # noqa: F401 — auto-activate venv
except ImportError:
    pass

try:
    import yaml
except ImportError:
    # Report only the confirmed failure: the bootstrap import above may have
    # succeeded — what is certain here is that PyYAML cannot be imported.
    print(
        "tools.py: PyYAML is not importable — tool interception is DISABLED "
        "for this call (pass-through). Install PyYAML for the interpreter "
        "running hooks, or make the agent_eval venv importable so the "
        "bootstrap can activate it.",
        file=sys.stderr,
    )
    _log_answer({"tier": "disabled", "reason": "pyyaml-missing"})
    sys.exit(0)


def main():
    input_data = json.load(sys.stdin)
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    # Load handler config from workspace. CWD-relative lookup first (case and
    # batch mode run the agent in the workspace where the file is written),
    # then fall back to resolving relative to this script's own location —
    # tool_handlers.yaml is generated next to the hooks/ dir that holds this
    # script. Without the fallback, in-repo mode (agent CWD = the user's repo
    # root, handlers written to the case workspace) was silently pass-through.
    script_dir = Path(__file__).resolve().parent
    config_path = None
    for candidate in (Path("tool_handlers.yaml"),
                      script_dir / "tool_handlers.yaml",
                      script_dir.parent / "tool_handlers.yaml"):
        if candidate.exists():
            config_path = candidate
            break
    if config_path is None:
        _log_answer({"tier": "disabled", "reason": "tool-handlers-missing",
                     "cwd": os.getcwd()})
        sys.exit(0)

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    # Find matching handler
    handler = _find_handler(tool_name, tool_input, config.get("handlers", []))
    if not handler:
        sys.exit(0)  # No handler — pass through

    # --- AskUserQuestion: auto-answer ---
    if tool_name == "AskUserQuestion":
        _handle_ask_user(tool_input, config, handler)
        return

    # --- Env checks: block if environment doesn't match ---
    env_checks = handler.get("env_checks", {})
    if env_checks:
        for var_name, check in env_checks.items():
            value = os.environ.get(var_name, "")
            must_contain = check.get("must_contain", [])
            if must_contain and not any(m in value.lower() for m in must_contain):
                _deny(f"Env {var_name}='{value}' doesn't match required: {must_contain}")
                return
        # All env checks passed — allow
        sys.exit(0)

    # --- Default for matched tools without specific handling: block ---
    _deny(f"Blocked by eval harness: {handler.get('match', 'matched handler')}")


def _find_handler(tool_name, tool_input, handlers):
    """Find the first handler that matches the tool call.

    Bash handlers REQUIRE input_filters — without them, a handler with
    "Bash" in patterns would silently match every Bash command and the
    default-deny in main() would block the entire skill. To prevent that
    footgun, Bash handlers without input_filters are treated as
    misconfigured: emit a stderr warning and skip (pass-through).
    Resolve them in eval-run Step 3b before relying on the handler.
    """
    for h in handlers:
        patterns = h.get("patterns", [])
        input_filters = h.get("input_filters", [])

        if tool_name == "Bash" and "Bash" in patterns:
            if not input_filters:
                print(
                    f"tool_handlers.yaml: handler {h.get('match', '?')!r} "
                    "has 'Bash' in patterns but no input_filters — "
                    "skipping (would deny all Bash). Resolve in eval-run "
                    "Step 3b.",
                    file=sys.stderr,
                )
                continue
            command = tool_input.get("command", "")
            if any(re.search(f, command, re.IGNORECASE) for f in input_filters):
                return h
            continue  # Bash matched pattern but not filter — skip

        # For other tools: match by pattern only
        for pattern in patterns:
            if pattern == tool_name:
                return h
            if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
                return h

    return None


def _handle_ask_user(tool_input, config, handler):
    """Auto-answer AskUserQuestion using case overrides, LLM, or first option.

    Resolution order for each question:
    1. Exact match in case_overrides (question text → answer)
    2. LLM-based answer (haiku) using the handler prompt + case context
    3. Fallback: pick the first option or "yes"

    Every answered question is recorded to the hook_answers.jsonl provenance
    ledger with its tier, so scoring can tell a case-specific answer (tiers
    1-2) from an arbitrary one (tier 3).
    """
    case_overrides = config.get("case_overrides", {})
    hook_model = config.get("hook_model")
    prompt = handler.get("prompt", "")
    if not prompt and handler.get("prompt_file"):
        try:
            prompt = Path(handler["prompt_file"]).read_text()
        except OSError:
            pass
    answers = {}
    for q in tool_input.get("questions", []):
        text = q.get("question", "")
        options = q.get("options", [])
        entry = {
            "question": text,
            "options": [o.get("label", "") for o in options
                        if isinstance(o, dict)],
        }
        llm_meta = None

        # 1. Exact match
        answer = case_overrides.get(text)
        if answer is not None:
            entry["tier"] = "override"

        # 2. LLM-based answer
        if answer is None and options:
            answer, llm_meta = _llm_answer(text, options, prompt,
                                           model=hook_model)
            if answer is not None:
                entry["tier"] = "llm"

        # 3. Fallback. Announce it: tiers 1 and 2 are the ones that answer
        # *for this case*, so landing here means the agent under test was
        # handed an arbitrary answer — and because the run still completes,
        # nothing downstream distinguishes that from a real answer.
        if answer is None:
            entry["tier"] = "fallback"
            answer = options[0]["label"] if options else "yes"
            print(f"AskUserQuestion fallback: no case override and no LLM "
                  f"answer for {text!r} — defaulting to {answer!r}",
                  file=sys.stderr)

        # Copy the LLM attempt's details into the record (llm tier, or a
        # fallback reached after a failed/rejected LLM attempt).
        if llm_meta:
            entry["hook_model"] = llm_meta.get("model")
            entry["match"] = llm_meta.get("match")
            for key in ("llm_raw", "error", "temperature_stripped"):
                if key in llm_meta:
                    entry[key] = llm_meta[key]

        entry["answer"] = answer
        _log_answer(entry)
        answers[text] = answer

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "questions": tool_input["questions"],
                "answers": answers,
            },
        }
    }
    json.dump(output, sys.stdout)


def _create_message(client, meta=None, **kwargs):
    """messages.create, retrying once without `temperature` if it is rejected.

    Anthropic removed the sampling parameters on Opus 4.7 and later (and
    Sonnet 5 accepts only the default), so `temperature=0` is a 400 there.
    That matters more here than anywhere else in the harness: the caller
    swallows every exception and falls back to the first option, so an
    unhandled 400 would feed the agent under test an unvetted answer while
    the eval still reported a pass.

    The check is behavioral rather than a model-name allowlist on purpose —
    `models.hook` may be a gateway alias (a LiteLLM virtual key, a vLLM
    --served-model-name) that no substring test can classify.

    ``meta`` is an optional provenance dict (never forwarded to the API):
    the strip-retry sets ``meta["temperature_stripped"] = True`` so the
    ledger records the decoding change.
    """
    try:
        return client.messages.create(**kwargs)
    except Exception as exc:
        rejected = (getattr(exc, "status_code", None) == 400
                    and "temperature" in str(exc).lower())
        if not rejected or "temperature" not in kwargs:
            raise
        print(f"hook model {kwargs.get('model')!r} rejects 'temperature' — "
              "retrying without it", file=sys.stderr)
        if meta is not None:
            meta["temperature_stripped"] = True
        return client.messages.create(
            **{k: v for k, v in kwargs.items() if k != "temperature"})


def _llm_answer(question, options, handler_prompt, model=None):
    """Use an LLM to pick the best answer for a question.

    Reads input.yaml and answers.yaml from CWD for case-specific context.
    Returns ``(label, meta)`` — the selected option label (or None if the
    reply was rejected or the API call failed) and a provenance dict for the
    ledger: always ``model``; ``match`` ("exact"/"fuzzy") on success;
    ``llm_raw`` (truncated reply) when the reply matched no option;
    ``error`` on API failure; ``temperature_stripped`` when the strip-retry
    fired.
    """
    # Load case context
    case_context = ""
    for fname in ("input.yaml", "answers.yaml"):
        p = Path(fname)
        if p.exists():
            try:
                case_context += f"\n--- {fname} ---\n{p.read_text()}\n"
            except OSError:
                pass

    option_labels = [o["label"] for o in options]
    option_list = "\n".join(
        f"  {i+1}. {o['label']}: {o.get('description', '')}"
        for i, o in enumerate(options)
    )

    prompt = f"""You are answering a question on behalf of a user during an automated evaluation run.

Handler instructions: {handler_prompt}

Case context:
{case_context}

Question: {question}

Available options:
{option_list}

Based on the handler instructions and case context, which option should be selected?
Reply with ONLY the option label text, nothing else."""

    meta = {"model": model or "claude-haiku-4-5-20251001"}
    try:
        import anthropic
        client = anthropic.Anthropic(timeout=30.0)
        response = _create_message(
            client,
            meta=meta,
            model=meta["model"],
            max_tokens=256,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text.strip()
        # Verify the answer matches an option label
        if answer in option_labels:
            print(f"LLM answered: {answer!r}", file=sys.stderr)
            meta["match"] = "exact"
            return answer, meta
        # Try fuzzy match — LLM might have added quotes or slight variation
        answer_lower = answer.lower().strip('"\'')
        for label in option_labels:
            if label.lower() == answer_lower:
                print(f"LLM answered (fuzzy): {label!r}", file=sys.stderr)
                meta["match"] = "fuzzy"
                return label, meta
        print(f"LLM answer {answer!r} not in options {option_labels}",
              file=sys.stderr)
        meta["llm_raw"] = answer[:500]
    except Exception as e:
        print(f"LLM answer failed: {e}", file=sys.stderr)
        meta["error"] = str(e)[:500]

    return None, meta


def _deny(reason):
    """Deny the tool call with a reason.

    Claude Code reads ``permissionDecisionReason``; emitting only ``reason``
    (the old key) makes the agent see the generic "Hook PreToolUse denied this
    tool" with the handler's carefully crafted guidance silently dropped — on
    a real eval run the agents had to reverse-engineer why the call was
    blocked. Keep ``reason`` too for anything still reading the old key.
    """
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "reason": reason,
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
