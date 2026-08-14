"""Integration tests for builtin judge resolution in the scoring pipeline."""

from types import SimpleNamespace
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig, JudgeConfig, ModelsConfig, OutputConfig
from score import load_judges, score_cases


class TestLoadJudgesBuiltin:

    def test_builtin_python_judge(self):
        config = EvalConfig(name="test", skill="test")
        config.models = ModelsConfig(judge="claude-sonnet-4-6")
        config.judges = [
            JudgeConfig(name="budget", builtin="cost_budget",
                        arguments={"max_cost_usd": 0.50}),
        ]
        judges = load_judges(config)
        assert len(judges) == 1
        name, scorer, condition, judge_type, _samples = judges[0]
        assert name == "budget"
        assert judge_type == "builtin"
        assert condition == ""

        # Test the scorer
        result = scorer(outputs={"cost_usd": 0.30})
        assert isinstance(result, tuple)
        assert result[0] is True
        assert "$0.30" in result[1]

    def test_builtin_python_judge_fail(self):
        config = EvalConfig(name="test", skill="test")
        config.models = ModelsConfig(judge="claude-sonnet-4-6")
        config.judges = [
            JudgeConfig(name="budget", builtin="cost_budget",
                        arguments={"max_cost_usd": 0.10}),
        ]
        judges = load_judges(config)
        _, scorer, _, _, _ = judges[0]
        result = scorer(outputs={"cost_usd": 0.50})
        assert result[0] is False
        assert "exceeds" in result[1]

    def test_builtin_fqn_resolution(self):
        config = EvalConfig(name="test", skill="test")
        config.models = ModelsConfig(judge="claude-sonnet-4-6")
        config.judges = [
            JudgeConfig(name="budget", builtin="efficiency/cost_budget"),
        ]
        judges = load_judges(config)
        assert len(judges) == 1
        assert judges[0][3] == "builtin"

    def test_unknown_builtin_raises(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="bad", builtin="nonexistent_judge"),
        ]
        with pytest.raises(ValueError, match="Unknown builtin judge"):
            load_judges(config)

    def test_mutual_exclusivity_check(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="bad", builtin="cost_budget",
                        check="return (True, 'ok')"),
        ]
        with pytest.raises(ValueError, match=r"mutually exclusive.*check"):
            load_judges(config)

    def test_mutual_exclusivity_prompt(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="bad", builtin="cost_budget",
                        prompt="evaluate this"),
        ]
        with pytest.raises(ValueError, match=r"mutually exclusive.*prompt"):
            load_judges(config)

    def test_mutual_exclusivity_prompt_file(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="bad", builtin="cost_budget",
                        prompt_file="some/file.md"),
        ]
        with pytest.raises(ValueError, match="mutually exclusive.*prompt_file"):
            load_judges(config)

    def test_mutual_exclusivity_module(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="bad", builtin="cost_budget",
                        module="some.module", function="judge"),
        ]
        with pytest.raises(ValueError, match=r"mutually exclusive.*module, function"):
            load_judges(config)

    def test_arguments_passed_to_python_judge(self):
        config = EvalConfig(name="test", skill="test")
        config.models = ModelsConfig(judge="claude-sonnet-4-6")
        config.judges = [
            JudgeConfig(name="budget", builtin="cost_budget",
                        arguments={"max_cost_usd": 2.0}),
        ]
        judges = load_judges(config)
        _, scorer, _, _, _ = judges[0]
        result = scorer(outputs={"cost_usd": 1.50})
        assert result[0] is True
        assert "$2.00" in result[1]


    def test_builtin_llm_judge_creates_scorer(self):
        config = EvalConfig(name="test", skill="test")
        config.models = ModelsConfig(judge="claude-sonnet-4-6")
        config.judges = [
            JudgeConfig(name="safety", builtin="no_harmful_content",
                        arguments={"categories": ["malware"]}),
        ]
        judges = load_judges(config)
        assert len(judges) == 1
        name, scorer, condition, judge_type, _samples = judges[0]
        assert name == "safety"
        assert judge_type == "builtin"

        with patch("score._call_structured_judge",
                   return_value=(True, "ok")) as mock_call:
            result = scorer(outputs={"conversation": "test", "files": {}})
            assert result == (True, "ok")
            rendered_prompt = mock_call.call_args[0][0]
            assert "malware" in rendered_prompt
            assert "test" in rendered_prompt
            # builtin judges are pass/fail
            assert mock_call.call_args[0][2] == "bool"


class TestParsers:

    def test_parse_bool_true(self):
        from score import _parse_bool_response
        result = _parse_bool_response('{"passed": true, "rationale": "looks good"}')
        assert result == (True, "looks good")

    def test_parse_bool_false(self):
        from score import _parse_bool_response
        result = _parse_bool_response('{"passed": false, "rationale": "found issues"}')
        assert result == (False, "found issues")

    def test_parse_bool_unparseable(self):
        from score import _parse_bool_response
        passed, rationale = _parse_bool_response("no json here")
        assert passed is False
        assert "Could not parse" in rationale

    def test_parse_score_json(self):
        from score import _parse_score_response
        result = _parse_score_response('{"score": 4, "rationale": "mostly good"}')
        assert result == (4, "mostly good")

    def test_parse_score_fallback_pattern(self):
        from score import _parse_score_response
        score, _ = _parse_score_response("Overall score: 3 out of 5")
        assert score == 3

    def test_parse_score_last_resort(self):
        from score import _parse_score_response
        score, _ = _parse_score_response("The quality is moderate, I'd say 4")
        assert score == 4

    def test_parse_score_unparseable(self):
        # A response with no on-scale score raises rather than defaulting to a
        # made-up 3 that would count toward the judge's mean (issue #182).
        from score import _parse_score_response
        with pytest.raises(ValueError, match="could not parse a score"):
            _parse_score_response("no numbers here at all")

    def test_parse_score_prose_keeps_full_rationale(self):
        # Judge returned markdown prose instead of JSON: the score is still
        # extracted and the FULL text is kept as the rationale (not truncated
        # to 200 chars mid-word).
        from score import _parse_score_response
        prose = ("## Assessment\n\n**WHAT:** clear. " + ("detail " * 80)
                 + "\n\n**Total: 4/5**")
        score, rationale = _parse_score_response(prose)
        assert score == 4
        assert len(rationale) > 200
        assert rationale.endswith("**Total: 4/5**")

    def test_parse_score_rationale_with_embedded_quotes(self):
        from score import _parse_score_response
        raw = ('{"score": 5, "rationale": "Names \\"Acme Corp\\" and quantifies '
               'impact across all criteria."}')
        score, rationale = _parse_score_response(raw)
        assert score == 5
        assert '"Acme Corp"' in rationale

    def test_parse_bool_prose_keeps_full_rationale(self):
        from score import _parse_bool_response
        prose = '{"passed": true} because ' + ("reason " * 80)
        passed, rationale = _parse_bool_response(prose)
        assert passed is True
        assert len(rationale) > 200


class TestStructuredJudge:

    def _resp(self, *blocks):
        return type("R", (), {"content": list(blocks)})()

    def _tool_use(self, name, data):
        return type("B", (), {"type": "tool_use", "name": name, "input": data})()

    def _text(self, txt):
        return type("B", (), {"type": "text", "text": txt})()

    def test_structured_score_from_tool_use(self):
        import score
        resp = self._resp(self._tool_use(
            "submit_score", {"score": 4, "rationale": "solid across criteria"}))
        with patch("score._get_anthropic_client") as mock_client:
            mock_client.return_value.messages.create.return_value = resp
            val, rat = score._call_structured_judge("p", "m", "score")
        assert val == 4 and rat == "solid across criteria"

    def test_structured_bool_from_tool_use(self):
        import score
        resp = self._resp(self._tool_use(
            "submit_evaluation", {"passed": False, "rationale": "missing field"}))
        with patch("score._get_anthropic_client") as mock_client:
            mock_client.return_value.messages.create.return_value = resp
            val, rat = score._call_structured_judge("p", "m", "bool")
        assert val is False and rat == "missing field"

    def test_structured_falls_back_to_text(self):
        # No tool_use block (model emitted text despite tool_choice) → parse text.
        import score
        resp = self._resp(self._text('{"score": 3, "rationale": "adequate"}'))
        with patch("score._get_anthropic_client") as mock_client:
            mock_client.return_value.messages.create.return_value = resp
            val, rat = score._call_structured_judge("p", "m", "score")
        assert val == 3 and rat == "adequate"


class TestSampleAggregation:

    def test_score_reduces_to_median_and_records_spread(self):
        import score
        runs = [{"value": 4, "rationale": "r4a"},
                {"value": 5, "rationale": "r5"},
                {"value": 4, "rationale": "r4b"}]
        out = score._aggregate_samples(runs, "llm")
        assert out["value"] == 4                      # median_low of [4,5,4]
        assert out["rationale"] in ("r4a", "r4b")     # a sample matching the value
        st = out["stability"]
        assert st["min"] == 4 and st["max"] == 5 and st["stable"] is False
        assert st["samples"] == 3

    def test_score_unanimous_is_stable(self):
        import score
        out = score._aggregate_samples(
            [{"value": 5, "rationale": "a"}, {"value": 5, "rationale": "b"}], "llm")
        assert out["value"] == 5
        assert out["stability"]["stable"] is True

    def test_bool_majority_vote(self):
        import score
        out = score._aggregate_samples(
            [{"value": True, "rationale": "ok"},
             {"value": False, "rationale": "no"},
             {"value": True, "rationale": "ok2"}], "llm")
        assert out["value"] is True                   # 2/3 pass
        assert out["stability"]["pass_count"] == 2
        assert out["stability"]["stable"] is False

    def test_all_samples_failed(self):
        import score
        out = score._aggregate_samples(
            [{"value": None, "error": "boom"}, {"value": None, "error": "boom2"}], "llm")
        assert out["value"] is None
        assert "boom" in out["error"]

    def test_normalize_result_shapes(self):
        import score
        assert score._normalize_result((4, "why")) == (4, "why")
        assert score._normalize_result(True) == (True, "")


class TestOutputsProxy:

    def test_str_renders_files(self):
        from score import _OutputsProxy
        proxy = _OutputsProxy({
            "files": {
                "main.py": "print('hello')",
                "readme.md": "# Title",
            }
        })
        text = str(proxy)
        assert "### main.py" in text
        assert "print('hello')" in text
        assert "### readme.md" in text

    def test_str_handles_binary(self):
        from score import _OutputsProxy
        proxy = _OutputsProxy({
            "files": {
                "image.dat": {"_binary": True, "name": "image.dat", "path": "/tmp/x"},
            }
        })
        text = str(proxy)
        assert "<binary: image.dat>" in text

    def test_dict_access_preserved(self):
        from score import _OutputsProxy
        proxy = _OutputsProxy({"files": {"a.txt": "content"}, "cost_usd": 0.5})
        assert proxy["cost_usd"] == 0.5
        assert proxy.get("files") == {"a.txt": "content"}

    def test_jinja2_renders_bare_outputs(self):
        from score import _render_jinja2_template
        template = "Files: {{ outputs }}"
        result = _render_jinja2_template(
            template, {},
            {"files": {"test.py": "code"}},
        )
        assert "### test.py" in result
        assert "code" in result

    def test_jinja2_renders_dict_access(self):
        from score import _render_jinja2_template
        template = "Cost: {{ outputs.cost_usd }}"
        result = _render_jinja2_template(template, {}, {"cost_usd": 0.42})
        assert "0.42" in result

    def test_jinja2_annotations_variable(self):
        from score import _render_jinja2_template
        # annotations_text provides formatted output, annotations provides dict access
        template = "Annotations:\n{{ annotations_text }}"
        result = _render_jinja2_template(
            template, {},
            {"annotations": {"key1": "val1", "key2": "val2"}},
        )
        assert "**key1**: val1" in result
        assert "**key2**: val2" in result

    def test_jinja2_conversation_variable(self):
        from score import _render_jinja2_template
        template = "Conversation: {{ conversation }}"
        result = _render_jinja2_template(
            template, {},
            {"conversation": "Hello, I completed the task."},
        )
        assert "Hello, I completed the task." in result


class TestLoadJudgesDuplicateValidation:

    def test_duplicate_names_raise(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="same_name", check="return (True, 'ok')"),
            JudgeConfig(name="same_name", check="return (False, 'bad')"),
        ]
        with pytest.raises(ValueError, match="Duplicate judge name 'same_name'"):
            load_judges(config)

    def test_unique_names_ok(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="judge_a", check="return (True, 'ok')"),
            JudgeConfig(name="judge_b", check="return (True, 'ok')"),
        ]
        judges = load_judges(config)
        assert len(judges) == 2


class TestLoadJudgesTypes:

    def test_check_judge_returns_5_tuple(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="test_check", check="return (True, 'ok')"),
        ]
        judges = load_judges(config)
        assert len(judges) == 1
        name, scorer, condition, judge_type, _samples = judges[0]
        assert name == "test_check"
        assert judge_type == "check"

    def test_check_judge_with_arguments(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(
                name="size_check",
                check='limit = arguments.get("max_chars", 10000)\nreturn (len(outputs.get("content", "")) <= limit, "ok")',
                arguments={"max_chars": 5},
            ),
        ]
        judges = load_judges(config)
        _, scorer, _, _, _ = judges[0]
        result = scorer(outputs={"content": "hi"})
        assert result[0] is True

        result = scorer(outputs={"content": "this is too long"})
        assert result[0] is False


class TestInlineJudgeFieldValidation:

    def test_warns_before_scoring_when_frontmatter_field_is_stale(
            self, tmp_path, capsys):
        config = EvalConfig(name="test", skill="test")
        config.outputs = [OutputConfig(path="artifacts")]
        config.judges = [
            JudgeConfig(
                name="frontmatter_valid",
                check=(
                    "import yaml\n"
                    "task = outputs['artifacts_content']\n"
                    "fm = yaml.safe_load(task.split('---', 2)[1])\n"
                    "if not fm.get('strat_key'):\n"
                    "    return False, 'bad strat_key'\n"
                    "return True, 'ok'"
                ),
            ),
        ]
        case_dir = tmp_path / "case-001"
        artifact_dir = case_dir / "artifacts"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "result.md").write_text(
            "---\nsource_key: alpha\n---\nbody\n"
        )

        judges = load_judges(config)
        score_cases(judges, [case_dir], config)

        captured = capsys.readouterr()
        assert "frontmatter_valid" in captured.err
        assert "strat_key" in captured.err
        assert "source_key" in captured.err

    @pytest.mark.parametrize("label,snippet,expected", [
        # Required — the shape issue #33 is about, in its three spellings.
        ("negated get", "if not fm.get('strat_key'): return False, 'b'",
         ["strat_key"]),
        ("bool get", "return bool(fm.get('strat_key')), 'x'", ["strat_key"]),
        ("subscript", "return bool(fm['title']), 'x'", ["title"]),
        # `"x" not in fm` is what README.md, the skill-batch cookbook and the
        # eval.yaml template all use — the template seeds generated evals.
        ("not in", "if 'score' not in fm: return False, 'm'", ["score"]),
        ("in", "if 'score' in fm: return True, 'ok'", ["score"]),
        # Not required, and warning here fires on a healthy run.
        ("explicit default", "return True, fm.get('priority', 'normal')", []),
        ("expects absence", "if fm.get('deprecated'): return False, 'd'", []),
    ])
    def test_which_references_count_as_required(self, label, snippet, expected):
        from score import _extract_frontmatter_field_refs
        source = f"fm = outputs['a_content']\n{snippet}\nreturn True, 'ok'"
        assert _extract_frontmatter_field_refs(source) == expected, label

    def test_an_untraceable_frontmatter_source_stays_silent(self):
        """`fm` built by iterating outputs['files'] — the shape strat-creator
        and the docs use. The artifact cannot be identified, so the old
        fallback blamed an unrelated `*_content` read and printed ITS keys as
        available. Silence is the only honest output."""
        from score import _extract_frontmatter_content_keys
        source = (
            "files = outputs.get('files', {})\n"
            "rev = next((c for p, c in files.items() "
            "if p.endswith('-review.md')), None)\n"
            "notes = outputs['notes_content']\n"
            "fm = yaml.safe_load(rev.split('---', 2)[1])\n"
            "return bool(fm.get('title')), notes"
        )
        assert _extract_frontmatter_content_keys(source) == []

    def test_the_conventional_aliases_are_recognised(self):
        from score import _extract_frontmatter_field_refs as refs
        for name in ("fm", "frontmatter", "meta"):
            source = (f"{name} = outputs['a_content']\n"
                      f"return bool({name}.get('title')), 'x'")
            assert refs(source) == ["title"], name

    def _stale_field_config(self, condition=None):
        config = EvalConfig(name="test", skill="test")
        config.outputs = [OutputConfig(path="artifacts")]
        config.judges = [
            JudgeConfig(
                name="frontmatter_valid",
                condition=condition,
                check=(
                    "import yaml\n"
                    "fm = yaml.safe_load("
                    "outputs['artifacts_content'].split('---', 2)[1]) or {}\n"
                    "if not fm.get('strat_key'):\n"
                    "    return False, 'bad'\n"
                    "return True, 'ok'"
                ),
            ),
        ]
        return config

    def _run_cases(self, tmp_path, cases, condition=None):
        dirs = []
        for name, front in cases:
            art = tmp_path / name / "artifacts"
            art.mkdir(parents=True)
            if front is not None:
                (art / "r.md").write_text(f"---\n{front}\n---\nbody\n")
            dirs.append(tmp_path / name)
        config = self._stale_field_config(condition)
        score_cases(load_judges(config), sorted(dirs), config)

    def test_drift_across_every_case_warns(self, tmp_path, capsys):
        self._run_cases(tmp_path, [("case-001", "source_key: a"),
                                   ("case-002", "source_key: b")])
        assert "strat_key" in capsys.readouterr().err

    def test_one_empty_case_does_not_warn_for_the_whole_run(self, tmp_path,
                                                            capsys):
        """Probing only case-001 meant a case that produced nothing spoke for
        every other case — and a partly-failed run is exactly when someone is
        trying to tell a skill regression from a judge bug."""
        self._run_cases(tmp_path, [("case-001", None),
                                   ("case-002", "strat_key: b")])
        assert "strat_key" not in capsys.readouterr().err

    def test_a_healthy_run_is_silent(self, tmp_path, capsys):
        self._run_cases(tmp_path, [("case-001", "strat_key: a"),
                                   ("case-002", "strat_key: b")])
        assert capsys.readouterr().err.count("requires frontmatter") == 0

    def test_a_judge_skipped_everywhere_is_not_reported(self, tmp_path, capsys):
        """An `if:`-gated judge that never runs cannot be stale against
        artifacts it never reads."""
        self._run_cases(tmp_path, [("case-001", "source_key: a")],
                        condition="annotations.get('kind') == 'never'")
        assert "strat_key" not in capsys.readouterr().err

    def test_unparseable_frontmatter_does_not_abort_the_run(self, tmp_path):
        """`yaml.safe_load` raises a bare ValueError — not YAMLError — for an
        out-of-range date, and the probe runs before any judge. Uncaught, it
        destroyed a run that main completes with a per-case error."""
        config = EvalConfig(name="test", skill="test")
        config.outputs = [OutputConfig(path="artifacts")]
        config.judges = [
            JudgeConfig(
                name="frontmatter_valid",
                check=(
                    "import yaml\n"
                    "fm = yaml.safe_load("
                    "outputs['artifacts_content'].split('---', 2)[1]) or {}\n"
                    "return bool(fm.get('title')), 'checked'"
                ),
            ),
        ]
        cases = []
        for name, front in (("case-001", "due: 2026-02-30\ntitle: a"),
                            ("case-002", "title: b")):
            art = tmp_path / name / "artifacts"
            art.mkdir(parents=True)
            (art / "r.md").write_text(f"---\n{front}\n---\nbody\n")
            cases.append(tmp_path / name)

        result = score_cases(load_judges(config), cases, config)

        # The good case still scores; the bad one errors, as it does on main.
        assert result["per_case"]["case-002"]["frontmatter_valid"]["value"] is True
        assert result["per_case"]["case-001"]["frontmatter_valid"]["value"] is None

    def test_unreadable_frontmatter_is_unknown_but_absent_is_empty(self):
        """Two different things. No frontmatter block means the fields really
        are absent, which is worth saying. Frontmatter that will not parse
        means we know nothing — reporting every field missing there is a wall
        of warnings caused by one bad date."""
        from score import _extract_yaml_frontmatter_keys as keys
        assert keys("body, no frontmatter") == set()          # absent
        assert keys("---\ndue: 2026-02-30\n---\nb") is None    # unreadable
        assert keys("---\ntitle: a\nbody") is None             # unclosed
        assert keys("---\ntitle: a\n---\nb") == {"title"}

    def test_a_bad_artifact_does_not_silence_the_warning_for_others(
            self, tmp_path, capsys):
        config = EvalConfig(name="test", skill="test")
        config.outputs = [OutputConfig(path="artifacts")]
        config.judges = [
            JudgeConfig(
                name="frontmatter_valid",
                check=(
                    "import yaml\n"
                    "fm = yaml.safe_load("
                    "outputs['artifacts_content'].split('---', 2)[1]) or {}\n"
                    "return bool(fm.get('strat_key')), 'x'"
                ),
            ),
        ]
        art = tmp_path / "case-001" / "artifacts"
        art.mkdir(parents=True)
        (art / "r.md").write_text("---\nsource_key: a\n---\nbody\n")
        score_cases(load_judges(config), [tmp_path / "case-001"], config)
        assert "strat_key" in capsys.readouterr().err

    def test_warning_probe_does_not_abort_scoring_on_loader_error(
            self, tmp_path):
        config = EvalConfig(name="test", skill="test")
        config.outputs = [OutputConfig(path="..")]
        config.judges = [
            JudgeConfig(
                name="frontmatter_valid",
                check=(
                    "fm = {}\n"
                    "if not fm.get('strat_key'):\n"
                    "    return False, 'bad strat_key'\n"
                    "return True, 'ok'"
                ),
            ),
        ]
        case_dir = tmp_path / "case-001"
        case_dir.mkdir()

        judges = load_judges(config)
        results = score_cases(judges, [case_dir], config)

        assert "Path escapes root directory" in (
            results["per_case"]["case-001"]["frontmatter_valid"]["error"]
        )

    def test_referenced_artifact_field_is_checked_independently(
            self, tmp_path, capsys):
        config = EvalConfig(name="test", skill="test")
        config.outputs = [
            OutputConfig(path="artifacts"),
            OutputConfig(path="other"),
        ]
        config.judges = [
            JudgeConfig(
                name="frontmatter_valid",
                check=(
                    "import yaml\n"
                    "task = outputs['artifacts_content']\n"
                    "fm = yaml.safe_load(task.split('---', 2)[1])\n"
                    "if not fm.get('strat_key'):\n"
                    "    return False, 'bad strat_key'\n"
                    "return True, 'ok'"
                ),
            ),
        ]
        case_dir = tmp_path / "case-001"
        artifact_dir = case_dir / "artifacts"
        other_dir = case_dir / "other"
        artifact_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        (artifact_dir / "result.md").write_text(
            "---\nsource_key: alpha\n---\nbody\n"
        )
        (other_dir / "result.md").write_text(
            "---\nstrat_key: legacy\n---\nbody\n"
        )

        judges = load_judges(config)
        score_cases(judges, [case_dir], config)

        captured = capsys.readouterr()
        assert "frontmatter_valid" in captured.err
        assert "strat_key" in captured.err
        assert "source_key" in captured.err

    def test_commented_field_reference_does_not_warn(self, tmp_path, capsys):
        config = EvalConfig(name="test", skill="test")
        config.outputs = [OutputConfig(path="artifacts")]
        config.judges = [
            JudgeConfig(
                name="frontmatter_valid",
                check=(
                    "import yaml\n"
                    "task = outputs['artifacts_content']\n"
                    "fm = yaml.safe_load(task.split('---', 2)[1])\n"
                    "# fm.get('strat_key') used to be checked here\n"
                    "if not fm.get('source_key'):\n"
                    "    return False, 'bad source_key'\n"
                    "return True, 'ok'"
                ),
            ),
        ]
        case_dir = tmp_path / "case-001"
        artifact_dir = case_dir / "artifacts"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "result.md").write_text(
            "---\nsource_key: alpha\n---\nbody\n"
        )

        judges = load_judges(config)
        score_cases(judges, [case_dir], config)

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_referenced_artifact_without_frontmatter_warns(
            self, tmp_path, capsys):
        config = EvalConfig(name="test", skill="test")
        config.outputs = [OutputConfig(path="artifacts")]
        config.judges = [
            JudgeConfig(
                name="frontmatter_valid",
                check=(
                    "import yaml\n"
                    "task = outputs['artifacts_content']\n"
                    "fm = yaml.safe_load(task.split('---', 2)[1]) or {}\n"
                    "if not fm.get('strat_key'):\n"
                    "    return False, 'bad strat_key'\n"
                    "return True, 'ok'"
                ),
            ),
        ]
        case_dir = tmp_path / "case-001"
        artifact_dir = case_dir / "artifacts"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "result.md").write_text("body without frontmatter\n")

        judges = load_judges(config)
        score_cases(judges, [case_dir], config)

        captured = capsys.readouterr()
        assert "frontmatter_valid" in captured.err
        assert "strat_key" in captured.err

    def test_extra_content_read_does_not_imply_frontmatter_source(
            self, tmp_path, capsys):
        config = EvalConfig(name="test", skill="test")
        config.outputs = [
            OutputConfig(path="artifacts"),
            OutputConfig(path="other"),
        ]
        config.judges = [
            JudgeConfig(
                name="frontmatter_valid",
                check=(
                    "import yaml\n"
                    "task = outputs['artifacts_content']\n"
                    "comparison = outputs['other_content']\n"
                    "fm = yaml.safe_load(task.split('---', 2)[1])\n"
                    "if comparison and not fm.get('strat_key'):\n"
                    "    return False, 'bad strat_key'\n"
                    "return True, 'ok'"
                ),
            ),
        ]
        case_dir = tmp_path / "case-001"
        artifact_dir = case_dir / "artifacts"
        other_dir = case_dir / "other"
        artifact_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        (artifact_dir / "result.md").write_text(
            "---\nstrat_key: alpha\n---\nbody\n"
        )
        (other_dir / "result.md").write_text(
            "---\nsource_key: beta\n---\nbody\n"
        )

        judges = load_judges(config)
        score_cases(judges, [case_dir], config)

        captured = capsys.readouterr()
        assert captured.err == ""


class TestJudgeTypeMetadata:

    def test_builtin_type_in_4tuple(self):
        config = EvalConfig(name="test", skill="test")
        config.models = ModelsConfig(judge="claude-sonnet-4-6")
        config.judges = [
            JudgeConfig(name="budget", builtin="cost_budget"),
        ]
        judges = load_judges(config)
        assert judges[0][3] == "builtin"

    def test_check_type_in_4tuple(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="test", check="return (True, 'ok')"),
        ]
        judges = load_judges(config)
        assert judges[0][3] == "check"

    def test_mixed_types_distinguishable(self):
        config = EvalConfig(name="test", skill="test")
        config.models = ModelsConfig(judge="claude-sonnet-4-6")
        config.judges = [
            JudgeConfig(name="budget", builtin="cost_budget"),
            JudgeConfig(name="inline", check="return (True, 'ok')"),
        ]
        judges = load_judges(config)
        types = {name: jtype for name, _, _, jtype, _ in judges}
        assert types["budget"] == "builtin"
        assert types["inline"] == "check"


class TestVendoringPattern:

    def test_vendored_python_judge(self, tmp_path):
        """A copied Python judge works via module/function."""
        import shutil
        src = (Path(__file__).parent.parent / "agent_eval" / "judges"
               / "efficiency" / "cost_budget.py")
        vendor_dir = tmp_path / "eval" / "judges"
        vendor_dir.mkdir(parents=True)
        (vendor_dir.parent / "__init__.py").write_text("")
        (vendor_dir / "__init__.py").write_text("")
        shutil.copy(src, vendor_dir / "cost_budget.py")

        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="vendored_budget",
                        module="eval.judges.cost_budget",
                        function="judge",
                        arguments={"max_cost_usd": 5.0}),
        ]
        judges = load_judges(config, project_root=tmp_path)
        assert len(judges) == 1
        _, scorer, _, judge_type, _ = judges[0]
        assert judge_type == "code"
        result = scorer(outputs={"cost_usd": 3.0})
        assert result[0] is True
        assert "$5.00" in result[1]


class TestNumericBounds:
    """A judge's declared `score_range` must reach the model and be enforced."""

    def test_defaults_to_1_5_when_undeclared(self):
        from score import _numeric_bounds
        assert _numeric_bounds(JudgeConfig(name="j")) == (1, 5, True)

    def test_declared_range_wins(self):
        from score import _numeric_bounds
        jc = JudgeConfig(name="j", feedback_type="int", score_range=[0.0, 2.0])
        assert _numeric_bounds(jc) == (0.0, 2.0, True)

    def test_float_feedback_type_is_not_int(self):
        from score import _numeric_bounds
        jc = JudgeConfig(name="j", feedback_type="float", score_range=[0.0, 1.0])
        assert _numeric_bounds(jc) == (0.0, 1.0, False)

    def test_bool_judge_has_no_bounds(self):
        from score import _numeric_bounds
        assert _numeric_bounds(JudgeConfig(name="j", feedback_type="bool")) is None

    def test_bounds_render_without_trailing_zero(self):
        # config coerces score_range to floats; "0.0-2.0" invites fractions.
        from score import _score_system_prompt
        assert "0-2" in _score_system_prompt((0.0, 2.0, True))


class TestJudgeRequestPayload:
    """The bug in #182 was in the REQUEST, which nothing asserted on."""

    def _capture(self, bounds):
        import score
        resp = type("R", (), {"content": [type("B", (), {
            "type": "tool_use", "name": "submit_score",
            "input": {"score": 1, "rationale": "r"}})()]})()
        with patch("score._get_anthropic_client") as mock_client:
            create = mock_client.return_value.messages.create
            create.return_value = resp
            score._call_structured_judge("p", "m", "score", bounds=bounds)
        return create.call_args.kwargs

    def test_declared_range_reaches_system_prompt_and_schema(self):
        kwargs = self._capture((0.0, 2.0, True))
        assert "0-2" in kwargs["system"]
        assert "1-5" not in kwargs["system"]
        schema = kwargs["tools"][0]["input_schema"]["properties"]["score"]
        assert (schema["minimum"], schema["maximum"]) == (0.0, 2.0)
        assert schema["type"] == "integer"

    def test_float_judge_gets_number_schema(self):
        kwargs = self._capture((0.0, 1.0, False))
        assert kwargs["tools"][0]["input_schema"]["properties"]["score"]["type"] == "number"

    def test_undeclared_range_keeps_the_1_5_default(self):
        kwargs = self._capture(None)
        assert "1-5" in kwargs["system"]
        schema = kwargs["tools"][0]["input_schema"]["properties"]["score"]
        assert (schema["minimum"], schema["maximum"]) == (1, 5)


class TestParseScoreResponseBounds:

    def test_prose_fraction_uses_the_declared_top(self):
        from score import _parse_score_response
        val, _ = _parse_score_response("I give it 2/2 overall", (0, 2, True))
        assert val == 2

    def test_loose_scan_ignores_off_scale_numbers(self):
        from score import _parse_score_response
        # "4" is not on a 0-2 scale; "1" is.
        val, _ = _parse_score_response("of the 4 criteria, quality is 1", (0, 2, True))
        assert val == 1

    def test_unparseable_raises_rather_than_inventing_a_score(self):
        from score import _parse_score_response
        with pytest.raises(ValueError) as exc:
            _parse_score_response("no numbers here at all", (0, 2, True))
        assert "[0, 2]" in str(exc.value)
        assert "no numbers here at all" in str(exc.value)

    def test_float_judge_keeps_the_decimal(self):
        from score import _parse_score_response
        val, _ = _parse_score_response('{"score": 0.75}', (0.0, 1.0, False))
        assert val == 0.75


class TestEnforceBounds:

    def test_in_range_value_passes_through(self):
        from score import _enforce_bounds
        assert _enforce_bounds(2, (0, 2, True), "j") == 2

    def test_above_range_raises_naming_the_judge_and_scale(self):
        from score import ScoreRangeError, _enforce_bounds
        with pytest.raises(ScoreRangeError) as exc:
            _enforce_bounds(4, (0, 2, True), "testability_score")
        assert "testability_score" in str(exc.value)
        assert "[0, 2]" in str(exc.value)

    def test_below_range_raises(self):
        from score import ScoreRangeError, _enforce_bounds
        with pytest.raises(ScoreRangeError):
            _enforce_bounds(-1, (0, 2, True), "j")

    def test_bools_and_undeclared_judges_are_untouched(self):
        from score import _enforce_bounds
        assert _enforce_bounds(True, (0, 2, True), "j") is True
        assert _enforce_bounds(42, None, "j") == 42


class TestFractionalScaleWithoutFeedbackType:
    """`feedback_type` is optional. A fractional `score_range` declared without
    it used to produce an integer schema whose maximum was unreachable."""

    def _bounds(self, score_range, feedback_type=""):
        import score
        jc = SimpleNamespace(name="j", score_range=score_range,
                             feedback_type=feedback_type)
        return score._numeric_bounds(jc)

    def test_fractional_bounds_ask_for_a_number(self):
        lo, hi, is_int = self._bounds([0, 2.5])
        assert (lo, hi, is_int) == (0, 2.5, False)
        import score
        tool = score._score_judge_tool((lo, hi, is_int))
        assert tool["input_schema"]["properties"]["score"]["type"] == "number"
        assert "a numeric score 0-2.5" in score._score_system_prompt((lo, hi, is_int))

    def test_whole_bounds_still_ask_for_an_integer(self):
        assert self._bounds([0, 2])[2] is True
        assert self._bounds(None)[2] is True  # the [1, 5] default

    def test_an_explicit_feedback_type_still_wins(self):
        assert self._bounds([0, 2], "float")[2] is False
        assert self._bounds([0, 2], "int")[2] is True


class TestLLMJudgeWiring:
    """The declared scale must survive the trip from eval.yaml to the request.

    `TestJudgeRequestPayload` hands `_call_structured_judge` its bounds
    directly, so it cannot see whether anything resolves them from the
    JudgeConfig. Deleting `bounds=bounds` in `_load_llm_judge` — reverting the
    #182 fix outright — left the whole suite green.
    """

    def _request(self, **judge_kwargs):
        import os
        import score
        config = EvalConfig(name="t", skill="t")
        config.models = ModelsConfig(judge="claude-sonnet-4-6")
        jc = JudgeConfig(name="j", prompt="rate it", **judge_kwargs)
        resp = type("R", (), {"content": [type("B", (), {
            "type": "tool_use", "name": "submit_score",
            "input": {"score": 1, "rationale": "r"}})()]})()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                patch("score._get_anthropic_client") as mock_client:
            mock_client.return_value.messages.create.return_value = resp
            scorer = score._load_llm_judge(jc, config)
            scorer(outputs={})
            return mock_client.return_value.messages.create.call_args.kwargs

    def test_declared_range_reaches_the_system_prompt_and_schema(self):
        kwargs = self._request(feedback_type="int", score_range=[0, 2])
        assert "0-2" in kwargs["system"] and "1-5" not in kwargs["system"]
        prop = kwargs["tools"][0]["input_schema"]["properties"]["score"]
        assert (prop["minimum"], prop["maximum"]) == (0, 2)

    def test_the_default_scale_is_still_one_to_five(self):
        kwargs = self._request(feedback_type="int")
        assert "1-5" in kwargs["system"]
        prop = kwargs["tools"][0]["input_schema"]["properties"]["score"]
        assert (prop["minimum"], prop["maximum"]) == (1, 5)

    def test_a_float_judge_asks_for_a_number(self):
        kwargs = self._request(feedback_type="float", score_range=[0, 1])
        assert kwargs["tools"][0]["input_schema"]["properties"]["score"]["type"] == "number"


class TestSignedScale:
    """A preference judge on [-1, 1] must not have its verdict inverted."""

    BOUNDS = (-1.0, 1.0, True)

    def test_prose_negative_scores_keep_their_sign(self):
        from score import _parse_score_response
        for text in ("Overall score: -1", "score = -1", "The result is -1"):
            val, _ = _parse_score_response(text, self.BOUNDS)
            assert val == -1, text

    def test_positive_scores_are_unaffected(self):
        from score import _parse_score_response
        assert _parse_score_response("Overall score: 1", self.BOUNDS)[0] == 1

    def test_an_unsigned_scale_reads_a_minus_as_off_scale(self):
        """A "-1" on a [0, 2] judge is off-scale, not a 1. Unsigned patterns
        read it as 1 and invented an in-range score from an invalid one."""
        from score import _parse_score_response
        with pytest.raises(ValueError):
            _parse_score_response("the answer is -1", (0.0, 2.0, True))

    def test_a_hyphen_inside_a_token_is_not_a_sign(self):
        from score import _parse_score_response
        assert _parse_score_response("case x-1 scored 2", (0.0, 2.0, True))[0] == 2

    def test_the_prompt_spells_out_a_signed_range(self):
        from score import _score_system_prompt
        assert "from -1 to 1" in _score_system_prompt(self.BOUNDS)
