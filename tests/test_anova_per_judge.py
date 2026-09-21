"""Tests for the opt-in per-judge ANOVA fan-out.

The fan-out runs the composite's own single/multi-factor analysis once per
judge, then Benjamini-Hochberg-corrects the ONE family spanning every
(judge, term) raw p-value. All fixtures are constructed dataframes / synthetic
summary.yaml runs with a known structure — deterministic by design.
"""

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

_scripts_dir = str(Path(__file__).parent.parent / "skills" / "eval-anova" / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from analyze import analyze_runs, load_conditions_from_runs  # noqa: E402


def _mk_run(runs_dir, run_id, model, per_case):
    """A standard run dir whose per_case maps case -> {judge: value}."""
    rd = runs_dir / run_id
    rd.mkdir(parents=True)
    cases = {}
    for case_id, judges in per_case.items():
        cases[case_id] = {
            j: {"value": v,
                "judge_type": "boolean" if isinstance(v, bool) else "numeric"}
            for j, v in judges.items()
        }
    (rd / "summary.yaml").write_text(yaml.dump({"run_id": run_id, "per_case": cases}))
    (rd / "condition.json").write_text(
        '{"levels": {"model": "%s"}}' % model)
    return rd


def _two_judge_runs(runs):
    """Two models × 4 cases × 2 scored judges (one numeric, one boolean),
    plus a constant judge that must be excluded from the family."""
    _mk_run(runs, "r-a", "model-a", {
        "c1": {"quality": 4, "tests_pass": True, "always_five": 5},
        "c2": {"quality": 5, "tests_pass": True, "always_five": 5},
        "c3": {"quality": 4, "tests_pass": True, "always_five": 5},
        "c4": {"quality": 5, "tests_pass": False, "always_five": 5},
    })
    _mk_run(runs, "r-b", "model-b", {
        "c1": {"quality": 2, "tests_pass": False, "always_five": 5},
        "c2": {"quality": 1, "tests_pass": False, "always_five": 5},
        "c3": {"quality": 3, "tests_pass": True, "always_five": 5},
        "c4": {"quality": 1, "tests_pass": False, "always_five": 5},
    })


def test_fan_out_produces_per_judge_terms(tmp_path):
    runs = tmp_path / "eval"
    _two_judge_runs(runs)
    analysis, _ = analyze_runs(runs, NS(reward=None), per_judge=True)

    pj = analysis["per_judge"]
    assert set(pj["judges"]) == {"quality", "tests_pass"}
    for entry in pj["judges"].values():
        assert set(entry["terms"]) == {"model"}
        cell = entry["terms"]["model"]
        assert 0.0 <= cell["p_raw"] <= 1.0
        assert cell["p_adjusted"] >= cell["p_raw"] - 1e-15
        assert isinstance(cell["significant"], bool)
        assert entry["n_cases"] == 4 and entry["n_conditions"] == 2
        assert "method" in entry


def test_bh_family_spans_judges_by_terms(tmp_path):
    """The correction family is the whole judges×terms set — the adjusted
    values match a hand-computed BH over the two raw p-values together."""
    runs = tmp_path / "eval"
    _two_judge_runs(runs)
    analysis, _ = analyze_runs(runs, NS(reward=None), per_judge=True)

    pj = analysis["per_judge"]
    assert pj["correction"] == "bh"
    assert pj["family_size"] == 2  # quality.model + tests_pass.model
    raws = sorted(e["terms"]["model"]["p_raw"] for e in pj["judges"].values())
    # BH over m=2: q_(2) = p_(2); q_(1) = min(p_(2), 2 * p_(1))
    expected = {raws[1]: raws[1], raws[0]: min(raws[1], 2 * raws[0])}
    for entry in pj["judges"].values():
        cell = entry["terms"]["model"]
        assert cell["p_adjusted"] == pytest.approx(expected[cell["p_raw"]])


def test_constant_judge_excluded_with_reason_not_in_family(tmp_path):
    runs = tmp_path / "eval"
    _two_judge_runs(runs)
    analysis, _ = analyze_runs(runs, NS(reward=None), per_judge=True)

    pj = analysis["per_judge"]
    assert "always_five" not in pj["judges"]
    reasons = {e["judge"]: e["reason"] for e in pj["excluded"]}
    assert "constant value" in reasons["always_five"]
    # the family counts only real tests — the excluded judge never inflates it
    assert pj["family_size"] == 2


def test_bool_judges_coerced_to_zero_one(tmp_path):
    runs = tmp_path / "eval"
    _two_judge_runs(runs)
    rows, _, _ = load_conditions_from_runs(runs, NS(reward=None))
    values = {r["judge:tests_pass"] for r in rows}
    assert values == {0.0, 1.0}
    assert all(isinstance(v, float) for v in values)


def test_opt_in_off_means_no_per_judge_key(tmp_path):
    runs = tmp_path / "eval"
    _two_judge_runs(runs)
    analysis, artifact = analyze_runs(runs, NS(reward=None))
    assert "per_judge" not in analysis
    assert '"per_judge"' not in artifact.read_text()


def test_composite_results_identical_with_and_without_per_judge(tmp_path):
    runs = tmp_path / "eval"
    _two_judge_runs(runs)
    without, _ = analyze_runs(runs, NS(reward=None))
    with_pj, _ = analyze_runs(runs, NS(reward=None), per_judge=True)

    assert with_pj["anova"] == without["anova"]
    assert with_pj["condition_summaries"] == without["condition_summaries"]
    assert with_pj["per_case"] == without["per_case"]
    assert with_pj["design"] == without["design"]


def test_error_and_none_samples_are_skipped_not_zeroed(tmp_path):
    """A judge that errored on a case yields no observation for that row —
    and a judge left with < 2 fully-crossed cases is excluded, with the
    exclusion reason saying so."""
    runs = tmp_path / "eval"
    rd = _mk_run(runs, "r-a", "model-a", {
        "c1": {"quality": 4, "sparse": 3},
        "c2": {"quality": 5, "sparse": 2},
        "c3": {"quality": 4},
    })
    # an errored sample: value None + error, must not become a 0.0
    summary = yaml.safe_load((rd / "summary.yaml").read_text())
    summary["per_case"]["c3"]["sparse"] = {
        "value": None, "error": "judge crashed", "judge_type": "numeric"}
    (rd / "summary.yaml").write_text(yaml.dump(summary))
    _mk_run(runs, "r-b", "model-b", {
        "c1": {"quality": 2, "sparse": 1},
        "c2": {"quality": 1},
        "c3": {"quality": 2, "sparse": 4},
    })

    rows, _, _ = load_conditions_from_runs(runs, NS(reward=None))
    by_case = {(r["model"], r["case_id"]): r for r in rows}
    assert "judge:sparse" not in by_case[("model-a", "c3")]
    assert "judge:sparse" not in by_case[("model-b", "c2")]

    analysis, _ = analyze_runs(runs, NS(reward=None), per_judge=True)
    pj = analysis["per_judge"]
    # only c1 is scored by "sparse" under BOTH conditions -> degenerate design
    assert "sparse" not in pj["judges"]
    reasons = {e["judge"]: e["reason"] for e in pj["excluded"]}
    assert "under every condition" in reasons["sparse"]


def test_pairwise_judges_never_become_observations(tmp_path):
    runs = tmp_path / "eval"
    rd = _mk_run(runs, "r-a", "model-a", {"c1": {"quality": 4}})
    summary = yaml.safe_load((rd / "summary.yaml").read_text())
    summary["per_case"]["c1"]["preference"] = {
        "value": 1, "judge_type": "pairwise"}
    (rd / "summary.yaml").write_text(yaml.dump(summary))

    rows, _, _ = load_conditions_from_runs(runs, NS(reward=None))
    assert "judge:preference" not in rows[0]
    assert "judge:quality" in rows[0]


def test_multi_factor_family_includes_interactions(tmp_path):
    """Two factors -> each judge contributes model, context and model:context
    terms; the BH family spans all judges × all terms."""
    import warnings

    runs = tmp_path / "eval"
    # 2×2 conditions over 6 cases; deterministic values with real variance on
    # both judges (case baseline + factor effects), so every Wald test exists.
    base = {f"c{i}": i for i in range(6)}
    for model in ("a", "b"):
        for context in ("off", "on"):
            per_case = {}
            for case_id, b in base.items():
                q = 1.0 + 0.3 * b + (1.5 if model == "b" else 0.0) \
                    + (0.7 if context == "on" else 0.0) \
                    + (0.2 * b if (model, context) == ("b", "on") else 0.0)
                s = 5.0 - 0.2 * b - (1.0 if model == "b" else 0.0) \
                    + (0.1 * b if context == "on" else 0.0)
                per_case[case_id] = {"quality": round(q, 3), "speed": round(s, 3)}
            rd = runs / f"r-{model}-{context}"
            rd.mkdir(parents=True)
            cases = {c: {j: {"value": v, "judge_type": "numeric"}
                         for j, v in js.items()} for c, js in per_case.items()}
            (rd / "summary.yaml").write_text(yaml.dump({"run_id": rd.name,
                                                        "per_case": cases}))
            (rd / "condition.json").write_text(
                '{"levels": {"model": "%s", "context": "%s"}}' % (model, context))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # mixedlm convergence chatter at small n
        analysis, _ = analyze_runs(runs, NS(reward=None), per_judge=True)

    pj = analysis["per_judge"]
    assert set(pj["judges"]) == {"quality", "speed"}
    for entry in pj["judges"].values():
        # factors are analysed in sorted order, so the interaction term is
        # context:model
        assert set(entry["terms"]) == {"model", "context", "context:model"}
        assert "mixed" in entry["method"].lower()
    tested = [t for e in pj["judges"].values()
              for t in e["terms"].values() if t["p_raw"] is not None]
    assert pj["family_size"] == len(tested) > 0
