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


def _valid_judge_fields():
    """The `valid_judge_fields` set literal in validate_eval.check_config.

    Parsed rather than imported: the set is a local, not a module constant.
    """
    tree = ast.parse(VALIDATE_EVAL.read_text(), filename=str(VALIDATE_EVAL))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "valid_judge_fields"
                        for t in node.targets)):
            return {e.value for e in node.value.elts}
    pytest.fail("could not find valid_judge_fields in validate_eval.py")


class TestJudgeFieldContract:
    """The allowlist drifted from JudgeConfig and `/eval-analyze --validate` then
    called a valid eval.yaml invalid — `score_range`, `step` and `agent` were all
    real fields the validator rejected."""

    def test_every_judge_config_field_is_accepted(self):
        import dataclasses
        sys.path.insert(0, str(REPO_ROOT))
        from agent_eval.config import JudgeConfig

        allowed = _valid_judge_fields()
        # `condition` is spelled `if` in YAML; `panel_models` is derived from
        # a list-valued `model:` and is never its own YAML key; everything
        # else matches verbatim.
        expected = {f.name for f in dataclasses.fields(JudgeConfig)}
        expected = (expected - {"condition", "panel_models"}) | {"if"}
        assert expected <= allowed, (
            f"validate_eval rejects real JudgeConfig field(s): "
            f"{sorted(expected - allowed)}"
        )

    def test_the_allowlist_invents_nothing(self):
        import dataclasses
        sys.path.insert(0, str(REPO_ROOT))
        from agent_eval.config import JudgeConfig

        # `condition` must NOT be accepted — YAML spells it `if`. Leaving it in
        # `known` would let an accidental `condition` entry pass this test.
        # `panel_models` must NOT be accepted either — it is derived from a
        # list-valued `model:`, never written in YAML.
        known = ({f.name for f in dataclasses.fields(JudgeConfig)}
                 - {"condition", "panel_models"}) | {"if"}
        assert _valid_judge_fields() == known, (
            f"validate_eval accepts field(s) JudgeConfig does not define: "
            f"{sorted(_valid_judge_fields() - known)}"
        )


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

        assert validate_eval._resolve_plugin_dir("plugins/ci") == plugin.resolve()

    def test_uses_repo_root_even_when_config_relative_path_exists(
            self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        config_dir = repo / "evals"
        plugin = config_dir / "plugin"
        config_dir.mkdir(parents=True)
        plugin.mkdir()
        monkeypatch.chdir(repo)

        assert validate_eval._resolve_plugin_dir("plugin") == (repo / "plugin").resolve()

    def test_allows_declared_parent_path_outside_project(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        config_dir = repo / "evals"
        outside = tmp_path / "outside"
        config_dir.mkdir(parents=True)
        outside.mkdir()
        monkeypatch.chdir(repo)
        assert validate_eval._resolve_plugin_dir("../outside", repo) == outside.resolve()

    def test_rejects_relative_symlink_escape(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / "plugin-link").symlink_to(outside, target_is_directory=True)
        monkeypatch.chdir(repo)
        with pytest.raises(ValueError, match="must not escape"):
            validate_eval._resolve_plugin_dir("plugin-link", repo)

    def test_does_not_prefer_existing_path_from_unrelated_cwd(
            self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        config_dir = repo / "evals"
        config_plugin = config_dir / "plugin"
        unrelated = tmp_path / "unrelated"
        unrelated_plugin = unrelated / "plugin"
        config_plugin.mkdir(parents=True)
        unrelated_plugin.mkdir(parents=True)
        monkeypatch.chdir(unrelated)
        assert validate_eval._resolve_plugin_dir("plugin", repo) == (repo / "plugin").resolve()


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


class TestPluginLayoutRequiresPluginDirs:
    """A plugin-packaged skill is invisible to the agent runtime without
    --plugin-dir, and the resulting run is silently green: every case returns
    "Unknown command: /<skill>", 0 turns, $0.00 and exit code 0. find_skill()
    resolves such a skill happily (it searches plugin manifests and a top-level
    skills/ directory), so the config used to validate clean while being
    unrunnable. These pin the gap shut.
    """

    CONFIG = """\
name: t
execution:
  mode: case
  skill: myskill
  arguments: "{prompt}"
__RUNNER__
models:
  skill: m
  judge: m
dataset:
  path: cases
  schema: one case dir per test
outputs:
  - path: out
    schema: artifacts
judges:
  - name: j
    description: d
    check: |
      return (True, "ok")
"""

    def _project(self, tmp_path, *, plugin_layout, plugin_dirs, unrelated_plugin=False):
        repo = tmp_path / "repo"
        if plugin_layout:
            (repo / "skills" / "myskill").mkdir(parents=True)
            (repo / "skills" / "myskill" / "SKILL.md").write_text("---\nname: myskill\n---\n")
            (repo / ".claude-plugin").mkdir(parents=True)
            (repo / ".claude-plugin" / "plugin.json").write_text('{"name": "p"}')
        else:
            (repo / ".claude" / "skills" / "myskill").mkdir(parents=True)
            (repo / ".claude" / "skills" / "myskill" / "SKILL.md").write_text(
                "---\nname: myskill\n---\n")
        if unrelated_plugin:
            other = repo / "plugins" / "other"
            (other / "skills" / "somethingelse").mkdir(parents=True)
            (other / "skills" / "somethingelse" / "SKILL.md").write_text(
                "---\nname: somethingelse\n---\n")
            (other / ".claude-plugin").mkdir(parents=True)
            (other / ".claude-plugin" / "plugin.json").write_text('{"name": "other"}')
        (repo / "cases" / "case-001").mkdir(parents=True)
        (repo / "cases" / "case-001" / "input.yaml").write_text("prompt: hi\n")
        runner = "runner:\n  type: claude-code\n"
        if plugin_dirs:
            runner += "  plugin_dirs:\n"
            for d in (plugin_dirs if isinstance(plugin_dirs, list) else ["."]):
                runner += f"    - \"{d}\"\n"
        (repo / "eval.yaml").write_text(self.CONFIG.replace("__RUNNER__", runner.rstrip()))
        return repo

    def _run(self, repo, monkeypatch, capsys):
        monkeypatch.chdir(repo)
        try:
            validate_eval.validate_config("eval.yaml")
        except SystemExit as exc:
            return exc.code, capsys.readouterr().out
        return 0, capsys.readouterr().out

    def test_plugin_layout_without_plugin_dirs_is_an_error(
            self, tmp_path, monkeypatch, capsys):
        repo = self._project(tmp_path, plugin_layout=True, plugin_dirs=False)
        code, out = self._run(repo, monkeypatch, capsys)
        assert code == 1, out
        assert "runner.plugin_dirs" in out, out
        assert "Unknown command: /myskill" in out, out

    def test_plugin_layout_with_plugin_dirs_passes(
            self, tmp_path, monkeypatch, capsys):
        repo = self._project(tmp_path, plugin_layout=True, plugin_dirs=True)
        _, out = self._run(repo, monkeypatch, capsys)
        assert "runner.plugin_dirs" not in out, out

    def test_dot_claude_skills_layout_needs_no_plugin_dirs(
            self, tmp_path, monkeypatch, capsys):
        """The conventional layout is auto-discovered, so it must not be flagged."""
        repo = self._project(tmp_path, plugin_layout=False, plugin_dirs=False)
        _, out = self._run(repo, monkeypatch, capsys)
        assert "runner.plugin_dirs" not in out, out

    def test_unresolvable_skill_stays_a_warning(
            self, tmp_path, monkeypatch, capsys):
        """A skill that resolves nowhere is a different failure — don't upgrade
        it to a plugin-layout error."""
        repo = self._project(tmp_path, plugin_layout=False, plugin_dirs=False)
        import shutil
        shutil.rmtree(repo / ".claude" / "skills" / "myskill")
        _, out = self._run(repo, monkeypatch, capsys)
        assert "not found in project" in out, out
        assert "runner.plugin_dirs" not in out, out

    def test_plugin_dirs_that_do_not_export_the_skill_are_an_error(
            self, tmp_path, monkeypatch, capsys):
        """A non-empty plugin_dirs must not blanket-suppress the diagnostic: the
        runner only passes --plugin-dir for those directories, so a plugin that
        exports unrelated skills leaves this one just as undiscoverable."""
        repo = self._project(tmp_path, plugin_layout=True,
                             plugin_dirs=["plugins/other"], unrelated_plugin=True)
        code, out = self._run(repo, monkeypatch, capsys)
        assert code == 1, out
        assert "none of the configured runner.plugin_dirs" in out, out
        assert "plugins/other" in out, out

    def test_plugin_dirs_that_export_the_skill_pass(
            self, tmp_path, monkeypatch, capsys):
        """The matching directory alongside an unrelated one is accepted."""
        repo = self._project(tmp_path, plugin_layout=True,
                             plugin_dirs=["plugins/other", "."],
                             unrelated_plugin=True)
        code, out = self._run(repo, monkeypatch, capsys)
        assert code == 0, out
        assert "plugin_dirs" not in out, out

    def test_uninspectable_plugin_dir_does_not_error(
            self, tmp_path, monkeypatch, capsys):
        """A missing plugin dir is a real misconfiguration, but the runner fails
        fast on it with a better message — don't guess here."""
        repo = self._project(tmp_path, plugin_layout=True,
                             plugin_dirs=["plugins/does-not-exist"])
        _, out = self._run(repo, monkeypatch, capsys)
        assert "none of the configured runner.plugin_dirs" not in out, out
