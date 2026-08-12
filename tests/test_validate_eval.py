"""Tests for skills/eval-analyze/scripts/validate_eval.py judge-template checks.

The validator hard-rejects a judge prompt that references a variable the scoring
renderer won't inject, so its idea of the "standard variables" has to match
score.py exactly. That list has drifted before in both directions — it once
carried a phantom `input` (rejecting the only correct spelling, `inputs`) and a
phantom `events` (accepting a name score.py never passes, which then renders as a
blank section because score.py uses a logging Undefined, not StrictUndefined).

test_standard_vars_match_score_renderer pins the two together so neither can move
without the other.
"""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_EVAL = REPO_ROOT / "skills" / "eval-analyze" / "scripts" / "validate_eval.py"
SCORE_PY = REPO_ROOT / "skills" / "eval-run" / "scripts" / "score.py"


def _load(path, name):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate_eval = _load(VALIDATE_EVAL, "_validate_eval_under_test")


def _score_render_kwargs():
    """Keyword names of the `template.render(...)` call in _render_jinja2_template.

    Parsed rather than imported: score.py pulls in the whole scoring stack, and
    this only needs the contract at the bottom of one function.
    """
    tree = ast.parse(SCORE_PY.read_text(), filename=str(SCORE_PY))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == "_render_jinja2_template"):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "render"):
                return {kw.arg for kw in call.keywords if kw.arg}
    pytest.fail("could not find template.render(...) in _render_jinja2_template")


class TestStandardVarsContract:
    def test_standard_vars_match_score_renderer(self):
        assert validate_eval.STANDARD_TEMPLATE_VARS == _score_render_kwargs(), (
            "validate_eval.STANDARD_TEMPLATE_VARS has drifted from the kwargs "
            "score._render_jinja2_template actually injects. Accepting a name "
            "score.py doesn't pass makes a judge prompt render a blank section; "
            "rejecting one it does pass blocks a valid eval.yaml."
        )

    def test_inputs_is_standard_and_input_is_not(self):
        """`input` is the execution.arguments variable (config.resolve_arguments),
        never a judge one — the two namespaces are distinct."""
        assert "inputs" in validate_eval.STANDARD_TEMPLATE_VARS
        assert "input" not in validate_eval.STANDARD_TEMPLATE_VARS

    def test_events_is_not_standard(self):
        """Only reachable as `outputs.events`; score.py injects no root-level name."""
        assert "events" not in validate_eval.STANDARD_TEMPLATE_VARS

    def test_mock_render_data_covers_every_standard_var(self):
        """_test_render_judge_templates must be able to render anything the
        variable check accepts, or valid configs fail at the render step."""
        errors, warnings = [], []
        judge = {"name": "probe", "prompt": " ".join(
            "{{ %s }}" % v for v in sorted(validate_eval.STANDARD_TEMPLATE_VARS))}
        validate_eval._test_render_judge_templates([judge], [], errors, warnings)
        assert errors == [], errors


class TestPluginDirResolution:
    def test_prefers_repo_root_relative_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        plugin = repo / "plugins" / "ci"
        config_dir = repo / "plugins" / "ci" / "evals"
        plugin.mkdir(parents=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(repo)

        assert validate_eval._resolve_plugin_dir(
            "plugins/ci", config_dir) == plugin.resolve()

    def test_falls_back_to_config_relative_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        config_dir = repo / "evals"
        plugin = config_dir / "plugin"
        config_dir.mkdir(parents=True)
        plugin.mkdir()
        monkeypatch.chdir(repo)

        assert validate_eval._resolve_plugin_dir(
            "plugin", config_dir) == plugin


def _check(prompt):
    """Run the variable check over a single LLM judge; return (errors, warnings)."""
    errors, warnings = [], []
    validate_eval._validate_template_variables(
        [{"name": "quality", "prompt": prompt}],
        [],                       # no declared outputs
        "input.yaml: {prompt}",   # dataset schema documents input.yaml
        errors, warnings,
    )
    return errors, warnings


class TestJudgeTemplateValidation:
    def test_inputs_validates_clean(self):
        errors, _ = _check("Rate this: {{ inputs }}")
        assert errors == [], errors

    def test_reasoning_and_evidence_validate_clean(self):
        errors, _ = _check("{{ reasoning }} {{ evidence }} {{ tool_trace }}")
        assert errors == [], errors

    def test_phantom_input_is_rejected(self):
        errors, _ = _check("Rate this: {{ input.prompt }}")
        assert any("input" in e for e in errors), errors

    def test_events_is_rejected(self):
        errors, _ = _check("Look at {{ events }}")
        assert any("events" in e for e in errors), errors

    def test_unknown_variable_is_rejected(self):
        errors, _ = _check("Rate {{ definitely_not_a_variable }}")
        assert any("definitely_not_a_variable" in e for e in errors), errors

    def test_declared_output_is_accepted(self):
        errors, warnings = [], []
        validate_eval._validate_template_variables(
            [{"name": "quality", "prompt": "Check {{ report }}"}],
            [{"name": "report", "path": "out/"}],
            "input.yaml: {prompt}",
            errors, warnings,
        )
        assert errors == [], errors

    def test_guidance_lists_the_real_variable_set(self):
        """The 'Standard variables:' hint is generated from the same constant, so
        it can't advertise a name the validator rejects."""
        errors, _ = _check("{{ nope }}")
        hint = "\n".join(errors)
        assert "inputs" in hint
        assert "events" not in hint
