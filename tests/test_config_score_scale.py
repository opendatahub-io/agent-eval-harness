"""Config-load validation of a judge's declared score scale (issue #182).

Each combination below was previously accepted and then ignored at scoring
time — the config said one thing and the judge did another, with nothing in
the run to say so. Failing at load turns a silent wrong number into an error
before any model is called.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.judges import builtin_judge_kind, builtin_judge_names  # noqa: E402


def _config(tmp_path, judges_yaml):
    p = tmp_path / "eval.yaml"
    p.write_text(
        f"name: t\nexecution: {{mode: case}}\n"
        f"dataset: {{path: {tmp_path}/cases}}\njudges:\n{judges_yaml}")
    return EvalConfig.from_yaml(p)


def test_bool_judge_may_not_declare_a_score_range(tmp_path):
    with pytest.raises(ValueError, match="no meaning with 'feedback_type: bool'"):
        _config(tmp_path, "  - {name: j, feedback_type: bool, score_range: [0, 2], "
                          "prompt: 'p'}\n")


def test_bool_judge_without_a_score_range_is_fine(tmp_path):
    cfg = _config(tmp_path, "  - {name: j, feedback_type: bool, prompt: 'p'}\n")
    assert cfg.judges[0].score_range is None


def test_int_judge_may_not_declare_fractional_bounds(tmp_path):
    with pytest.raises(ValueError, match="cannot express the fractional"):
        _config(tmp_path, "  - {name: j, feedback_type: int, score_range: [0, 2.5], "
                          "prompt: 'p'}\n")


def test_float_judge_may_declare_fractional_bounds(tmp_path):
    cfg = _config(tmp_path, "  - {name: j, feedback_type: float, "
                            "score_range: [0, 2.5], prompt: 'p'}\n")
    assert cfg.judges[0].score_range == [0.0, 2.5]


def test_int_judge_with_whole_float_bounds_is_fine(tmp_path):
    """YAML `[0, 2]` and `[0.0, 2.0]` describe the same integer scale."""
    cfg = _config(tmp_path, "  - {name: j, feedback_type: int, "
                            "score_range: [0.0, 2.0], prompt: 'p'}\n")
    assert cfg.judges[0].score_range == [0.0, 2.0]


def test_builtin_llm_judge_may_not_declare_a_feedback_type(tmp_path):
    with pytest.raises(ValueError, match="always scored as pass/fail"):
        _config(tmp_path, "  - {name: j, builtin: output_completeness, "
                          "feedback_type: int}\n")


def test_builtin_llm_judge_may_not_declare_a_score_range(tmp_path):
    with pytest.raises(ValueError, match="always scored as pass/fail"):
        _config(tmp_path, "  - {name: j, builtin: quality/output_completeness, "
                          "score_range: [1, 5]}\n")


def test_builtin_llm_judge_on_its_own_is_fine(tmp_path):
    cfg = _config(tmp_path, "  - {name: j, builtin: output_completeness}\n")
    assert cfg.judges[0].builtin == "output_completeness"


def test_builtin_llm_judge_may_restate_bool(tmp_path):
    """Redundant, but it agrees with what the judge does."""
    cfg = _config(tmp_path, "  - {name: j, builtin: output_completeness, "
                            "feedback_type: bool}\n")
    assert cfg.judges[0].feedback_type == "bool"


def test_builtin_python_judge_may_be_numeric(tmp_path):
    """A Python builtin returns whatever its function returns — the pass/fail
    contract belongs to the LLM prompts only."""
    cfg = _config(tmp_path, "  - {name: j, builtin: cost_budget, "
                            "feedback_type: int, score_range: [0, 2]}\n")
    assert cfg.judges[0].score_range == [0.0, 2.0]


def test_unknown_builtin_is_rejected_at_load(tmp_path):
    """A typo used to survive until scoring, after the run had been paid for."""
    with pytest.raises(ValueError, match="unknown builtin judge 'cost_budgets'"):
        _config(tmp_path, "  - {name: j, builtin: cost_budgets}\n")


def test_unknown_builtin_error_lists_what_exists(tmp_path):
    with pytest.raises(ValueError, match="quality/output_completeness"):
        _config(tmp_path, "  - {name: j, builtin: nope}\n")


def test_builtin_in_the_wrong_category_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown builtin judge"):
        _config(tmp_path, "  - {name: j, builtin: safety/cost_budget}\n")


def test_numeric_llm_judge_without_a_range_warns(tmp_path):
    with pytest.warns(UserWarning, match="no 'score_range'"):
        _config(tmp_path, "  - {name: j, feedback_type: int, prompt: 'p'}\n")


def test_llm_judge_with_no_feedback_type_and_no_range_warns(tmp_path):
    """The commonest shape, and the one the warning most needs to reach.

    `feedback_type` is optional and score.py treats anything but "bool" as
    numeric, so this judge is scored on the unenforced [1, 5] default.
    Gating the warning on ("int", "float") meant it never fired here.
    """
    with pytest.warns(UserWarning, match="no 'score_range'"):
        _config(tmp_path, "  - {name: j, prompt: 'p'}\n")


def test_a_bool_judge_without_a_range_is_silent(tmp_path):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _config(tmp_path, "  - {name: j, feedback_type: bool, prompt: 'p'}\n")


def test_numeric_llm_judge_with_a_range_is_silent(tmp_path):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _config(tmp_path, "  - {name: j, feedback_type: int, score_range: [0, 2], "
                          "prompt: 'p'}\n")


def test_inline_check_without_a_range_is_silent(tmp_path):
    """An inline check computes its own value; there is no model to bound."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _config(tmp_path, "  - {name: attempts, feedback_type: int, "
                          "check: \"return (7, 'r')\"}\n")


class TestBuiltinJudgeKind:
    """Resolution has to work without `discover()`, which execs every Python
    judge module — far too much to do to validate a config."""

    def test_resolves_an_llm_judge(self):
        assert builtin_judge_kind("output_completeness") == "llm"

    def test_resolves_a_python_judge(self):
        assert builtin_judge_kind("cost_budget") == "python"

    def test_resolves_a_qualified_name(self):
        assert builtin_judge_kind("quality/output_completeness") == "llm"

    def test_rejects_a_wrong_category(self):
        assert builtin_judge_kind("safety/output_completeness") is None

    def test_unknown_name_resolves_to_nothing(self):
        assert builtin_judge_kind("nope") is None

    def test_does_not_escape_the_package(self, tmp_path):
        assert builtin_judge_kind("../config") is None
        assert builtin_judge_kind("..") is None
        assert builtin_judge_kind("") is None

    def test_ignores_package_internals(self):
        assert builtin_judge_kind("__init__") is None

    def test_names_are_qualified_and_exclude_internals(self):
        names = builtin_judge_names()
        assert "quality/output_completeness" in names
        assert "efficiency/cost_budget" in names
        assert not any(n.rsplit("/", 1)[1].startswith("_") for n in names)
