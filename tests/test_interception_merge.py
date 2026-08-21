"""The harness-owned knob merge onto tool_handlers.yaml (PR9).

`merge_handler_knobs` is the choke-point fix: eval.yaml owns the runtime
knobs (`hook_model` from models.hook, `calibration` from inputs.tools), the
resolved tool_handlers.yaml owns patterns/input_filters/env_checks/
case_overrides. The RESOLVED-file branch of `generate_interception`
bypassed `build_handlers` entirely, so a build_handlers-only serialization
would silently drop the knobs exactly on the path that carries
case_overrides — these tests pin the merge on BOTH sources, the in-repo
mirror, the hook_model setdefault notice, and the explicit per-hook
`timeout` at every settings-generation site.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.tools.interception import (  # noqa: E402
    HOOK_TIMEOUT_SECONDS, build_handlers, generate_interception,
    merge_handler_knobs,
)

# conftest.py puts skills/eval-run/scripts on sys.path
import workspace  # noqa: E402


def _config(tmp_path, *, hook_model="claude-haiku-4-5", calibration=True):
    body = f"""
name: t
execution:
  skill: s
models:
  hook: {hook_model}
inputs:
  tools:
    - match: Questions asked to the user via AskUserQuestion.
      prompt: answer from input.yaml
      calibration: {str(calibration).lower()}
"""
    p = tmp_path / "eval.yaml"
    p.write_text(body)
    return EvalConfig.from_yaml(p)


RESOLVED = {
    "handlers": [{
        "match": "Questions asked to the user via AskUserQuestion.",
        "patterns": ["AskUserQuestion"],
        "prompt": "answer from input.yaml",
    }],
    "case_overrides_source": "human",
    "case_overrides": {
        "What priority?": "Normal",
        "Which region?": {"answer": "eu-west", "source": "human"},
    },
}


def _write_resolved(tmp_path, data=None):
    p = tmp_path / "resolved" / "tool_handlers.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data or RESOLVED, sort_keys=False))
    return p


def _generated(tmp_path, config, resolved_path=None):
    target = tmp_path / "ws"
    target.mkdir(exist_ok=True)
    generate_interception(target, config, "python3 hooks/tools.py",
                          resolved_handlers_path=resolved_path)
    handlers = yaml.safe_load((target / "tool_handlers.yaml").read_text())
    settings = json.loads((target / ".claude" / "settings.json").read_text())
    return handlers, settings


# --- the resolved-file bypass is fixed ---------------------------------------

def test_resolved_file_gets_calibration_and_hook_model(tmp_path, capsys):
    config = _config(tmp_path)
    handlers, _ = _generated(tmp_path, config,
                             resolved_path=_write_resolved(tmp_path))
    handler = handlers["handlers"][0]
    assert handler["calibration"] is True
    # provenance keys preserved verbatim
    assert handlers["case_overrides_source"] == "human"
    assert handlers["case_overrides"]["Which region?"] == {
        "answer": "eu-west", "source": "human"}
    # hook_model setdefault fired — loudly (deliberate behavior fix: the
    # resolved file used to fall back to the hardcoded haiku default).
    assert handlers["hook_model"] == "claude-haiku-4-5"
    assert "lacked hook_model" in capsys.readouterr().err


def test_resolved_file_hook_model_wins_over_models_hook(tmp_path, capsys):
    config = _config(tmp_path)
    resolved = dict(RESOLVED)
    resolved["hook_model"] = "my-gateway-alias"
    handlers, _ = _generated(tmp_path, config,
                             resolved_path=_write_resolved(tmp_path, resolved))
    assert handlers["hook_model"] == "my-gateway-alias"
    assert "lacked hook_model" not in capsys.readouterr().err


def test_heuristic_branch_gets_the_same_knobs(tmp_path, capsys):
    config = _config(tmp_path)
    handlers, _ = _generated(tmp_path, config)  # no resolved file
    assert handlers["handlers"][0]["calibration"] is True
    assert handlers["hook_model"] == "claude-haiku-4-5"
    # build_handlers already set hook_model — no setdefault notice.
    assert "lacked hook_model" not in capsys.readouterr().err


def test_rewritten_match_text_falls_back_to_ask_user_patterns(tmp_path,
                                                              capsys):
    """The eval-run agent may rewrite a handler's match text in Step 3a —
    the knob still lands on every AskUserQuestion-matching handler, with a
    stderr warning naming the unjoined match."""
    config = _config(tmp_path)
    resolved = {
        "handlers": [
            {"match": "REWRITTEN by the agent",
             "patterns": ["AskUserQuestion"]},
            {"match": "jira", "patterns": ["Bash"],
             "input_filters": ["jira"]},
        ],
    }
    handlers, _ = _generated(tmp_path, config,
                             resolved_path=_write_resolved(tmp_path, resolved))
    assert handlers["handlers"][0]["calibration"] is True
    assert "calibration" not in handlers["handlers"][1]
    err = capsys.readouterr().err
    assert "no handler matches inputs.tools entry" in err


def test_calibration_false_never_stamps_the_knob(tmp_path):
    config = _config(tmp_path, calibration=False)
    handlers, _ = _generated(tmp_path, config,
                             resolved_path=_write_resolved(tmp_path))
    assert "calibration" not in handlers["handlers"][0]


def test_merge_helper_wildcard_patterns_count_as_ask_user(tmp_path):
    """A bare '*' handler prefix-matches every tool at runtime — the
    fallback join treats it as an AskUserQuestion handler too."""
    config = _config(tmp_path)
    handler_data = {"handlers": [{"match": "everything", "patterns": ["*"]}]}
    merged = merge_handler_knobs(handler_data, config)
    assert merged["handlers"][0]["calibration"] is True


# --- explicit per-hook timeout at every settings-generation site -------------

def test_case_and_batch_settings_carry_the_hook_timeout(tmp_path):
    """generate_interception is the single settings writer for case AND
    batch workspaces (plus Harbor task packages) — every PreToolUse entry
    carries the explicit worst-case timeout."""
    config = _config(tmp_path)
    _, settings = _generated(tmp_path, config)
    entries = settings["hooks"]["PreToolUse"]
    assert entries
    for entry in entries:
        for hook in entry["hooks"]:
            assert hook["timeout"] == HOOK_TIMEOUT_SECONDS


def test_in_repo_mirror_writes_knobs_and_timeout(tmp_path):
    """_setup_in_repo_tool_hooks builds handlers without
    generate_interception — it must mirror the same merge and the same
    per-hook timeout."""
    config = _config(tmp_path)
    case_ws = tmp_path / "case_ws"
    (case_ws / "hooks").mkdir(parents=True)
    settings = {}
    workspace._setup_in_repo_tool_hooks(case_ws, config, settings)

    handlers = yaml.safe_load((case_ws / "tool_handlers.yaml").read_text())
    assert handlers["handlers"][0]["calibration"] is True
    assert handlers["hook_model"] == "claude-haiku-4-5"

    entries = settings["hooks"]["PreToolUse"]
    assert entries
    for entry in entries:
        for hook in entry["hooks"]:
            assert hook["timeout"] == HOOK_TIMEOUT_SECONDS
    # interceptor copied next to the handlers it reads
    assert (case_ws / "hooks" / "tools.py").is_file()


def test_in_repo_mirror_matches_build_handlers_output(tmp_path):
    """The in-repo handler set is byte-equal to the shared build_handlers +
    merge pipeline — the two paths cannot drift."""
    config = _config(tmp_path)
    case_ws = tmp_path / "case_ws"
    case_ws.mkdir()
    workspace._setup_in_repo_tool_hooks(case_ws, config, {})
    written = yaml.safe_load((case_ws / "tool_handlers.yaml").read_text())

    expected, _ = build_handlers(config)
    expected = merge_handler_knobs(expected, config)
    assert written == expected
