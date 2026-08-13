"""End-to-end range enforcement in score_cases (issue #182).

A judge that returns a value off its declared `score_range` is recorded as an
error sample and drops out of the aggregate — clamping it would turn a 4 from a
0-2 judge into a perfect 2. A judge that declares no range is left alone: an
inline check returning a raw count must not be forced into the default [1, 5].
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import score as sc  # noqa: E402
from agent_eval.config import EvalConfig  # noqa: E402


def _config(tmp_path, judges_yaml):
    p = tmp_path / "eval.yaml"
    p.write_text(
        f"name: t\nexecution: {{mode: case}}\n"
        f"dataset: {{path: {tmp_path}/cases}}\njudges:\n{judges_yaml}")
    return EvalConfig.from_yaml(p)


def _case(tmp_path):
    cd = tmp_path / "cases" / "case-1"
    cd.mkdir(parents=True)
    return cd


def test_out_of_range_value_becomes_an_error_sample(tmp_path, capsys):
    config = _config(tmp_path, "  - {name: testability, feedback_type: int, "
                               "score_range: [0, 2], check: \"return (4, 'r')\"}\n")
    result = sc.score_cases(sc.load_judges(config), [_case(tmp_path)], config)
    entry = result["per_case"]["case-1"]["testability"]
    assert entry["value"] is None
    assert "outside its declared score_range [0, 2]" in entry["error"]
    # A scale breach is the one judge error that also prints: it is a
    # prompt/config bug that recurs every run and is worth seeing in the log.
    assert "WARNING" in capsys.readouterr().err


def test_out_of_range_value_is_excluded_from_the_aggregate(tmp_path):
    config = _config(tmp_path, "  - {name: testability, feedback_type: int, "
                               "score_range: [0, 2], check: \"return (4, 'r')\"}\n")
    result = sc.score_cases(sc.load_judges(config), [_case(tmp_path)], config)
    assert result["aggregated"]["testability"]["values"] == []


def test_in_range_value_is_kept(tmp_path):
    config = _config(tmp_path, "  - {name: testability, feedback_type: int, "
                               "score_range: [0, 2], check: \"return (1, 'r')\"}\n")
    result = sc.score_cases(sc.load_judges(config), [_case(tmp_path)], config)
    entry = result["per_case"]["case-1"]["testability"]
    assert entry["value"] == 1
    assert "error" not in entry


def test_judge_without_a_declared_range_is_untouched(tmp_path):
    config = _config(tmp_path, "  - {name: attempts, check: \"return (7, 'r')\"}\n")
    result = sc.score_cases(sc.load_judges(config), [_case(tmp_path)], config)
    entry = result["per_case"]["case-1"]["attempts"]
    assert entry["value"] == 7
    assert "error" not in entry


def test_one_bad_sample_does_not_cost_the_case():
    """A sampled judge (samples: 3) survives one off-scale sample.

    The breach errors that sample only; the case still reduces to the median of
    the survivors, so enforcement costs a stability flag rather than a value.
    """
    runs = [{"value": 1, "rationale": "a"},
            {"value": 2, "rationale": "b"},
            {"value": None, "error": "judge 'j' returned 4, outside [0, 2]"}]
    agg = sc._aggregate_samples(runs, "llm")
    assert agg["value"] == 1  # median_low of the two survivors
    assert agg["stability"]["error_count"] == 1
    assert agg["stability"]["stable"] is False


def test_boolean_judge_is_untouched(tmp_path):
    config = _config(tmp_path, "  - {name: files_exist, feedback_type: bool, "
                               "check: \"return (True, 'r')\"}\n")
    result = sc.score_cases(sc.load_judges(config), [_case(tmp_path)], config)
    assert result["per_case"]["case-1"]["files_exist"]["value"] is True


def test_a_deterministic_judge_keeps_its_own_fractional_value(tmp_path):
    """Declaring a `score_range` asks for validation and report bands, not for
    the judge's arithmetic to be rounded.

    `_enforce_bounds` used to end in `_coerce_number`, and `is_int` is true for
    any `feedback_type` other than "float" — including the unset default — so a
    `check:` judge returning 0.75 on a [0, 1] scale was recorded as 1.
    """
    config = _config(tmp_path, "  - {name: ratio, score_range: [0, 1], "
                               "check: \"return (0.75, 'r')\"}\n")
    result = sc.score_cases(sc.load_judges(config), [_case(tmp_path)], config)
    entry = result["per_case"]["case-1"]["ratio"]
    assert entry["value"] == 0.75
    assert entry.get("error") is None


def test_the_mlflow_fallback_judge_is_told_its_scale(monkeypatch):
    """`_enforce_bounds` applies by judge name whatever produced the value, so
    a judge on this path would be failed against a scale it never received."""
    import sys
    import types

    captured = {}

    def fake_make_judge(**kwargs):
        captured.update(kwargs)
        return lambda **_: (1, "r")

    mod = types.ModuleType("mlflow.genai.judges")
    mod.make_judge = fake_make_judge
    monkeypatch.setitem(sys.modules, "mlflow.genai.judges", mod)
    for var in ("ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    from agent_eval.config import EvalConfig, JudgeConfig, ModelsConfig
    config = EvalConfig(name="t", skill="t")
    config.models = ModelsConfig(judge="claude-sonnet-4-6")
    jc = JudgeConfig(name="j", prompt="rate it", feedback_type="int",
                     score_range=[0, 2])
    sc._load_llm_judge(jc, config)
    assert "between 0 and 2" in captured["instructions"]


def test_a_non_finite_value_is_rejected(tmp_path):
    """NaN compares False against every bound, so `value < lo or value > hi`
    waved it through and one sample turned the judge's whole mean into NaN."""
    config = _config(tmp_path, "  - {name: nanny, score_range: [0, 2], "
                               "check: \"return (float('nan'), 'r')\"}\n")
    result = sc.score_cases(sc.load_judges(config), [_case(tmp_path)], config)
    entry = result["per_case"]["case-1"]["nanny"]
    assert entry["value"] is None
    assert "outside its declared score_range" in entry["error"]


def test_an_all_errored_judge_reports_the_real_cause(tmp_path):
    """"skipped for all cases" hid the actionable cause, and score_range
    enforcement makes an all-errored judge a realistic outcome."""
    current = {"q": {"mean": None, "values": [], "errored_cases": 3}}
    regs = sc.detect_regressions(current, {"q": {"min_mean": 1.0}})
    assert "errored on 3 cases" in regs[0].detail
    assert "skipped" not in regs[0].detail


def test_a_genuinely_skipped_judge_still_says_skipped(tmp_path):
    current = {"q": {"mean": None, "values": [], "errored_cases": 0}}
    regs = sc.detect_regressions(current, {"q": {"min_mean": 1.0}})
    assert "skipped for all cases" in regs[0].detail


def test_max_error_rate_gates_a_shrinking_sample(tmp_path):
    """A judge that errors on SOME cases still yields a mean over the
    survivors, so `min_mean` alone lets one good score and nine errors pass."""
    current = {"q": {"mean": 2.0, "values": [2.0], "errored_cases": 9}}
    assert sc.detect_regressions(current, {"q": {"min_mean": 1.0}}) == []
    regs = sc.detect_regressions(current, {"q": {"min_mean": 1.0,
                                                 "max_error_rate": 0.2}})
    assert [r.metric for r in regs] == ["error_rate"]
    assert regs[0].current_value == "0.900"
    assert "9 of 10 cases errored" in regs[0].detail


def test_max_error_rate_tolerates_what_it_allows(tmp_path):
    current = {"q": {"mean": 2.0, "values": [2.0] * 9, "errored_cases": 1}}
    assert sc.detect_regressions(current, {"q": {"min_mean": 1.0,
                                                 "max_error_rate": 0.2}}) == []


def test_error_counts_reach_the_aggregate(tmp_path):
    """detect_regressions can only distinguish the two causes if score_cases
    records them."""
    config = _config(tmp_path, "  - {name: q, score_range: [0, 2], "
                               "check: \"return (7, 'off scale')\"}\n")
    result = sc.score_cases(sc.load_judges(config), [_case(tmp_path)], config)
    assert result["aggregated"]["q"]["errored_cases"] == 1


def test_max_error_rate_works_on_a_persisted_summary(tmp_path):
    """`values` is stripped before summary.yaml is written, so a gate that
    derived its denominator from it read every judge as 100% errored on the
    standalone `score.py regression` path — the one CI actually calls."""
    persisted = {"q": {"mean": 4.0, "scored_cases": 9, "errored_cases": 1}}
    assert sc.detect_regressions(persisted, {"q": {"max_error_rate": 0.2}}) == []

    bad = {"q": {"mean": 4.0, "scored_cases": 1, "errored_cases": 9}}
    regs = sc.detect_regressions(bad, {"q": {"max_error_rate": 0.2}})
    assert regs[0].current_value == "0.900"


def test_scored_cases_survives_into_the_summary(tmp_path):
    config = _config(tmp_path, "  - {name: q, score_range: [0, 2], "
                               "check: \"return (1, 'ok')\"}\n")
    result = sc.score_cases(sc.load_judges(config), [_case(tmp_path)], config)
    agg = result["aggregated"]["q"]
    assert agg["scored_cases"] == 1
    # What cmd_judges actually persists — no `values` key.
    assert "scored_cases" in {k: v for k, v in agg.items() if k != "values"}
