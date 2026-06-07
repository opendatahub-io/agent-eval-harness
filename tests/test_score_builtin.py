"""Integration tests for builtin judge resolution in the scoring pipeline."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig, JudgeConfig, ModelsConfig
from score import load_judges


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
        name, scorer, condition, judge_type = judges[0]
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
        _, scorer, _, _ = judges[0]
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
        _, scorer, _, _ = judges[0]
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
        name, scorer, condition, judge_type = judges[0]
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
        from score import _parse_score_response
        score, rationale = _parse_score_response("no numbers here at all")
        assert score == 3
        assert "Could not parse" in rationale

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
        template = "Annotations: {{ annotations }}"
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

    def test_check_judge_returns_4_tuple(self):
        config = EvalConfig(name="test", skill="test")
        config.judges = [
            JudgeConfig(name="test_check", check="return (True, 'ok')"),
        ]
        judges = load_judges(config)
        assert len(judges) == 1
        name, scorer, condition, judge_type = judges[0]
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
        _, scorer, _, _ = judges[0]
        result = scorer(outputs={"content": "hi"})
        assert result[0] is True

        result = scorer(outputs={"content": "this is too long"})
        assert result[0] is False


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
        types = {name: jtype for name, _, _, jtype in judges}
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
        _, scorer, _, judge_type = judges[0]
        assert judge_type == "code"
        result = scorer(outputs={"cost_usd": 3.0})
        assert result[0] is True
        assert "$5.00" in result[1]
