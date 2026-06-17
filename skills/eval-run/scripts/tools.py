#!/usr/bin/env python3
"""PreToolUse hook script for intercepting tools during headless eval.

Reads tool_handlers.yaml from ``--config`` path (or CWD as fallback).
Handlers contain resolved patterns and runtime checks (from natural
language ``match`` and ``prompt`` in eval.yaml, resolved by eval-run
at workspace setup time).

Supports:
- Auto-answering AskUserQuestion via per-case overrides
- Blocking tools based on env var checks (e.g., production Jira)
- Filtering Bash commands by content patterns
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description="PreToolUse hook interceptor")
    parser.add_argument("--config", default=None,
                        help="Path to tool_handlers.yaml (default: CWD)")
    parser.add_argument("--case-dir", default=None,
                        help="Dataset case directory containing answers.yaml")
    args = parser.parse_args()

    input_data = json.load(sys.stdin)
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    # Load handler config — from --config if provided, else CWD.
    # When --config is explicit, missing file is fail-closed (D5):
    # deny the tool call rather than silently passing through.
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            _deny(f"Hook config not found: {config_path}")
            return
    else:
        config_path = Path("tool_handlers.yaml")
        if not config_path.exists():
            sys.exit(0)

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    # Find matching handler
    handler = _find_handler(tool_name, tool_input, config.get("handlers", []))
    if not handler:
        sys.exit(0)  # No handler — pass through

    # --- AskUserQuestion: auto-answer ---
    if tool_name == "AskUserQuestion":
        _handle_ask_user(tool_input, config, handler, case_dir=args.case_dir)
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
    _deny(f"Blocked by policy: {handler.get('match', 'matched handler')}")


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


def _handle_ask_user(tool_input, config, handler, case_dir=None):
    """Auto-answer AskUserQuestion using case overrides, LLM, or first option.

    Resolution order for each question:
    1. Exact match in case_overrides (question text → answer)
    2. LLM-based answer (haiku) using the handler prompt + case context
    3. Fallback: pick the first option or "yes"
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

        # 1. Exact match
        answer = case_overrides.get(text)

        # 2. LLM-based answer
        if answer is None and options:
            answer = _llm_answer(text, options, prompt, model=hook_model,
                                 case_dir=case_dir)

        # 3. Fallback
        if answer is None:
            answer = options[0]["label"] if options else "yes"

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


def _llm_answer(question, options, handler_prompt, model=None, case_dir=None):
    """Use an LLM to pick the best answer for a question.

    Reads input.yaml from CWD and answers.yaml from ``case_dir`` (or CWD
    as fallback) for case-specific context.
    Returns the selected option label, or None if the API call fails.
    """
    # Load case context — input.yaml from CWD, answers.yaml from --case-dir
    case_context = ""
    input_path = Path("input.yaml")
    if input_path.exists():
        try:
            case_context += f"\n--- input.yaml ---\n{input_path.read_text()}\n"
        except OSError:
            pass
    answers_path = Path(case_dir) / "answers.yaml" if case_dir else Path("answers.yaml")
    if answers_path.exists():
        try:
            case_context += f"\n--- answers.yaml ---\n{answers_path.read_text()}\n"
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

    try:
        import anthropic
        client = anthropic.Anthropic(timeout=30.0)
        response = client.messages.create(
            model=model or "claude-haiku-4-5-20251001",
            max_tokens=256,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text.strip()
        # Verify the answer matches an option label
        if answer in option_labels:
            print(f"LLM answered: {answer!r}", file=sys.stderr)
            return answer
        # Try fuzzy match — LLM might have added quotes or slight variation
        answer_lower = answer.lower().strip('"\'')
        for label in option_labels:
            if label.lower() == answer_lower:
                print(f"LLM answered (fuzzy): {label!r}", file=sys.stderr)
                return label
        print(f"LLM answer {answer!r} not in options {option_labels}",
              file=sys.stderr)
    except Exception as e:
        print(f"LLM answer failed: {e}", file=sys.stderr)

    return None


def _deny(reason):
    """Deny the tool call with a reason."""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "reason": reason,
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
