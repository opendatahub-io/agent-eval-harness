"""`score.py clarity` — the instrument-clarity diagnostic (paper Sec 10.2).

m rater models re-rate a deterministic (sorted + strided, seedless) case
subsample with the judge's own rubric; the m-way chance-corrected alpha is
compared against the 0.67 exploratory floor. Explicitly instrument clarity —
does the rubric admit consistent application? — never rater validity, and
never a CI gate. Fully mocked: no network.
"""

import argparse
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import score  # noqa: E402
from score import (  # noqa: E402
    CLARITY_FLOOR, CLARITY_LABEL, _clarity_sort_key, _stride_subsample,
    cmd_clarity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONFIG_YAML = """
name: t
execution: {skill: s}
judges:
  - {name: q, llm_rubric: score it, feedback_type: bool}
"""


class _FakeRubricScorer:
    """LLM-judge stand-in exposing the per-model call path (`for_model`)."""

    def __init__(self, script, calls):
        self._script = script  # callable(rater, case_id) -> value | Exception
        self.calls = calls     # appended (rater, case_id) per call

    def for_model(self, rater):
        def scorer(outputs=None, **kwargs):
            cid = Path((outputs or {})["case_dir"]).name
            self.calls.append((rater, cid))
            v = self._script(rater, cid)
            if isinstance(v, Exception):
                raise v
            return v, f"{rater}:{cid}"
        return scorer

    def __call__(self, outputs=None, **kwargs):
        raise AssertionError("clarity must rate through for_model")


def _setup_run(tmp_path, monkeypatch, n_cases, per_case=None,
               config_yaml=CONFIG_YAML):
    runs_root = tmp_path / "runs"
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs_root))
    run_dir = runs_root / "s" / "r1"
    for i in range(1, n_cases + 1):
        (run_dir / "cases" / f"case-{i:03d}").mkdir(parents=True)
    if per_case is not None:
        (run_dir / "summary.yaml").write_text(
            yaml.safe_dump({"run_id": "r1", "per_case": per_case}))
    cfg = tmp_path / "eval.yaml"
    cfg.write_text(config_yaml)
    return cfg, run_dir


def _patch_judges(monkeypatch, scorer, name="q", judge_type="llm"):
    monkeypatch.setattr(
        score, "load_judges",
        lambda config, root=None: [(name, scorer, "", judge_type, 1)])


def _args(cfg, raters="m1,m2,m3", judge=None, max_cases=20, samples=1):
    return argparse.Namespace(run_id="r1", config=str(cfg), raters=raters,
                              judge=judge, max_cases=max_cases,
                              samples=samples)


def _clarity_block(run_dir):
    return yaml.safe_load((run_dir / "summary.yaml").read_text())["clarity"]


# ---------------------------------------------------------------------------
# Seedless deterministic subsampling primitives
# ---------------------------------------------------------------------------

def test_stride_subsample_is_deterministic_and_spread():
    items = list(range(30))
    picked = _stride_subsample(items, 10)
    assert picked == _stride_subsample(items, 10)  # no random anywhere
    assert len(picked) == 10
    assert picked == sorted(set(picked))  # distinct, order-preserving
    assert picked[0] == 0 and picked[-1] >= 27  # spans the sorted range


def test_stride_subsample_returns_small_inputs_unchanged():
    assert _stride_subsample([1, 2, 3], 20) == [1, 2, 3]


def test_clarity_sort_key_groups_by_verdict_then_case_id():
    keys = [_clarity_sort_key(v, c) for v, c in
            [(True, "b"), (False, "a"), (True, "a"), (None, "z")]]
    assert sorted(keys) == [
        (0, 0.0, "a"), (0, 1.0, "a"), (0, 1.0, "b"), (1, "None", "z")]


# ---------------------------------------------------------------------------
# cmd_clarity end-to-end (mocked raters)
# ---------------------------------------------------------------------------

def test_deterministic_stratified_subsample(tmp_path, monkeypatch, capsys):
    """30 candidates, 15 per verdict stratum, max 10: the sorted + strided
    subsample takes 5 from each stratum, identically on every invocation."""
    per_case = {f"case-{i:03d}": {"q": {"value": i > 15}}
                for i in range(1, 31)}
    cfg, run_dir = _setup_run(tmp_path, monkeypatch, 30, per_case)
    rated_sets = []
    for _ in range(2):
        calls = []
        _patch_judges(monkeypatch, _FakeRubricScorer(
            lambda r, c: int(c[-2:]) > 15, calls))
        cmd_clarity(_args(cfg, max_cases=10))
        rated_sets.append({c for _, c in calls})
    assert rated_sets[0] == rated_sets[1]  # seedless determinism
    assert len(rated_sets[0]) == 10
    false_stratum = {c for c in rated_sets[0] if int(c[-2:]) <= 15}
    assert len(false_stratum) == 5  # proportional coverage of both strata
    out = capsys.readouterr().out
    assert "judge call(s) planned" in out  # preflight call count


def test_m_way_alpha_floor_label_and_persistence(tmp_path, monkeypatch,
                                                 capsys):
    per_case = {f"case-{i:03d}": {"q": {"value": i % 2 == 0}}
                for i in range(1, 9)}
    cfg, run_dir = _setup_run(tmp_path, monkeypatch, 8, per_case)
    calls = []
    # All three raters agree per case, with variance across cases -> alpha 1.
    _patch_judges(monkeypatch, _FakeRubricScorer(
        lambda r, c: int(c[-1]) % 2 == 0, calls))
    cmd_clarity(_args(cfg))

    clarity = _clarity_block(run_dir)
    assert clarity["label"] == CLARITY_LABEL
    assert clarity["raters"] == ["m1", "m2", "m3"]
    assert clarity["families"] == {"unknown": 3}
    assert clarity["floor"] == CLARITY_FLOOR == 0.67
    assert clarity["n_cases"] == 8
    block = clarity["judges"]["q"]
    assert block["metric"] == "krippendorff_alpha"
    assert block["level"] == "nominal"
    assert block["value"] == 1.0
    assert block["meets_floor"] is True
    assert block["label"] == CLARITY_LABEL
    assert block["n_units"] == 8
    # Full case x rater matrix persisted for drill-down.
    assert block["cases"]["case-001"] == {"m1": False, "m2": False,
                                          "m3": False}
    out = capsys.readouterr().out
    assert "meets the 0.67 exploratory floor" in out
    # 8 cases x 3 raters x 1 sample.
    assert len(calls) == 24


def test_skipped_and_errored_cases_are_excluded(tmp_path, monkeypatch):
    per_case = {
        "case-001": {"q": {"value": None,
                           "rationale": "Skipped: condition false"}},
        "case-002": {"q": {"value": None, "error": "boom"}},
    }
    for i in range(3, 9):
        per_case[f"case-{i:03d}"] = {"q": {"value": i % 2 == 0}}
    cfg, run_dir = _setup_run(tmp_path, monkeypatch, 8, per_case)
    calls = []
    _patch_judges(monkeypatch, _FakeRubricScorer(
        lambda r, c: True, calls))
    cmd_clarity(_args(cfg))
    rated = {c for _, c in calls}
    assert "case-001" not in rated and "case-002" not in rated
    assert len(rated) == 6


def test_rater_errors_become_missing_ratings(tmp_path, monkeypatch):
    per_case = {f"case-{i:03d}": {"q": {"value": i % 2 == 0}}
                for i in range(1, 7)}
    cfg, run_dir = _setup_run(tmp_path, monkeypatch, 6, per_case)
    calls = []

    def script(rater, cid):
        if rater == "m3":
            return RuntimeError("api down")
        return int(cid[-1]) % 2 == 0

    _patch_judges(monkeypatch, _FakeRubricScorer(script, calls))
    cmd_clarity(_args(cfg))
    block = _clarity_block(run_dir)["judges"]["q"]
    assert block["cases"]["case-001"]["m3"] is None  # missing, not a category
    assert block["value"] == 1.0  # computed over the surviving raters


def test_below_small_n_floor_no_coefficient_raw_table(tmp_path, monkeypatch,
                                                      capsys):
    per_case = {f"case-{i:03d}": {"q": {"value": i % 2 == 0}}
                for i in range(1, 4)}
    cfg, run_dir = _setup_run(tmp_path, monkeypatch, 3, per_case)
    _patch_judges(monkeypatch, _FakeRubricScorer(
        lambda r, c: int(c[-1]) % 2 == 0, []))
    cmd_clarity(_args(cfg))
    block = _clarity_block(run_dir)["judges"]["q"]
    assert block["value"] is None
    assert block["reason_code"] == "below_floor"
    assert block["meets_floor"] is None
    out = capsys.readouterr().out
    assert "raw case x rater table" in out
    assert "case-001" in out


def test_single_family_raters_warn(tmp_path, monkeypatch, capsys):
    per_case = {f"case-{i:03d}": {"q": {"value": i % 2 == 0}}
                for i in range(1, 7)}
    cfg, _ = _setup_run(tmp_path, monkeypatch, 6, per_case)
    _patch_judges(monkeypatch, _FakeRubricScorer(lambda r, c: True, []))
    cmd_clarity(_args(
        cfg, raters="claude-sonnet-4-5,claude-haiku-4-5,claude-opus-4-8"))
    err = capsys.readouterr().err
    assert "one provider family" in err
    assert "spuriously high" in err


def test_unknown_alias_raters_do_not_warn(tmp_path, monkeypatch, capsys):
    per_case = {f"case-{i:03d}": {"q": {"value": i % 2 == 0}}
                for i in range(1, 7)}
    cfg, _ = _setup_run(tmp_path, monkeypatch, 6, per_case)
    _patch_judges(monkeypatch, _FakeRubricScorer(lambda r, c: True, []))
    cmd_clarity(_args(cfg, raters="claude-sonnet-4-5,my-gateway-alias"))
    assert "one provider family" not in capsys.readouterr().err


@pytest.mark.parametrize("raters,match", [
    ("m1", "at least 2 rater models"),
    ("m1,m1", "duplicate rater"),
])
def test_bad_raters_exit_loudly(tmp_path, monkeypatch, capsys, raters,
                                match):
    cfg, _ = _setup_run(tmp_path, monkeypatch, 3, {})
    _patch_judges(monkeypatch, _FakeRubricScorer(lambda r, c: True, []))
    with pytest.raises(SystemExit):
        cmd_clarity(_args(cfg, raters=raters))
    assert match in capsys.readouterr().err


def test_unknown_judge_exits_loudly(tmp_path, monkeypatch, capsys):
    cfg, _ = _setup_run(tmp_path, monkeypatch, 3, {})
    _patch_judges(monkeypatch, _FakeRubricScorer(lambda r, c: True, []))
    with pytest.raises(SystemExit):
        cmd_clarity(_args(cfg, judge=["nope"]))
    assert "unknown judge 'nope'" in capsys.readouterr().err


def test_non_llm_judge_selected_explicitly_exits_loudly(tmp_path,
                                                        monkeypatch, capsys):
    cfg, _ = _setup_run(tmp_path, monkeypatch, 3, {})
    plain = lambda outputs=None, **kw: (True, "ok")  # noqa: E731 — no for_model
    monkeypatch.setattr(score, "load_judges",
                        lambda config, root=None: [("q", plain, "", "check",
                                                    1)])
    with pytest.raises(SystemExit):
        cmd_clarity(_args(cfg, judge=["q"]))
    assert "no per-model call path" in capsys.readouterr().err


def test_samples_reduce_per_rater_before_the_alpha(tmp_path, monkeypatch):
    per_case = {f"case-{i:03d}": {"q": {"value": True}}
                for i in range(1, 7)}
    cfg, run_dir = _setup_run(tmp_path, monkeypatch, 6, per_case)
    calls = []
    # m2 flips its first draw per case; majority over 3 draws still True.
    seen = {}

    def script(rater, cid):
        i = seen.get((rater, cid), 0)
        seen[(rater, cid)] = i + 1
        if rater == "m2" and i == 0:
            return False
        return True

    _patch_judges(monkeypatch, _FakeRubricScorer(script, calls))
    cmd_clarity(_args(cfg, raters="m1,m2", samples=3))
    block = _clarity_block(run_dir)["judges"]["q"]
    assert block["cases"]["case-001"] == {"m1": True, "m2": True}
    assert len(calls) == 6 * 2 * 3  # cases x raters x samples


# ---------------------------------------------------------------------------
# cmd_judges' re-score note names clarity too
# ---------------------------------------------------------------------------

def _judges_args(cfg):
    return argparse.Namespace(run_id="r1", config=str(cfg), workspace=None,
                              model=None, samples=None, no_llm_judges=False)


def _patch_scoring_noop(monkeypatch):
    monkeypatch.setattr(score, "load_judges",
                        lambda config, root=None: [])
    monkeypatch.setattr(
        score, "score_cases",
        lambda judges, case_dirs, config, run_id=None,
        samples_override=None: {"per_case": {}, "aggregated": {}})


def test_rescore_note_names_clarity_when_a_clarity_block_exists(
        tmp_path, monkeypatch, capsys):
    cfg, run_dir = _setup_run(tmp_path, monkeypatch, 2, None)
    (run_dir / "summary.yaml").write_text(yaml.safe_dump(
        {"run_id": "r1", "clarity": {"judges": {"q": {"value": 0.8}}}}))
    _patch_scoring_noop(monkeypatch)
    score.cmd_judges(_judges_args(cfg))
    err = capsys.readouterr().err
    assert "instrument clarity — re-run: score.py clarity" in err
    assert "score.py calibration" not in err


def test_rescore_note_names_both_when_both_exist(tmp_path, monkeypatch,
                                                 capsys):
    cfg, run_dir = _setup_run(tmp_path, monkeypatch, 2, None)
    (run_dir / "summary.yaml").write_text(yaml.safe_dump(
        {"run_id": "r1",
         "human_calibration": {"judges": ["q"]},
         "clarity": {"judges": {"q": {"value": 0.8}}}}))
    _patch_scoring_noop(monkeypatch)
    score.cmd_judges(_judges_args(cfg))
    err = capsys.readouterr().err
    assert "judge calibration — re-run: score.py calibration" in err
    assert "instrument clarity — re-run: score.py clarity" in err


def test_no_rescore_note_on_a_fresh_run(tmp_path, monkeypatch, capsys):
    cfg, run_dir = _setup_run(tmp_path, monkeypatch, 2, None)
    _patch_scoring_noop(monkeypatch)
    score.cmd_judges(_judges_args(cfg))
    assert "re-scoring invalidated" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Report: compact clarity table under the scoring summary
# ---------------------------------------------------------------------------

def test_report_renders_the_clarity_table():
    import report
    summary = {
        "judges": {"q": {"mean": 4.0, "scored_cases": 6}},
        "per_case": {},
        "clarity": {
            "label": CLARITY_LABEL,
            "raters": ["m1", "m2", "m3"],
            "families": {"unknown": 3},
            "n_raters": 3, "floor": 0.67, "n_cases": 6, "samples": 1,
            "judges": {"q": {"metric": "krippendorff_alpha",
                             "level": "nominal", "value": 0.71,
                             "n_units": 6, "n_cases": 6,
                             "meets_floor": True,
                             "label": CLARITY_LABEL, "rationale": "r"}},
        },
    }
    html = report._render_scoring_summary(summary, {"judges": []})
    assert "Instrument clarity" in html
    assert "not rater validity" in html
    assert "0.710" in html
    assert "meets floor" in html


def test_report_renders_nothing_without_a_clarity_block():
    import report
    html = report._render_scoring_summary(
        {"judges": {"q": {"mean": 4.0, "scored_cases": 6}}, "per_case": {}},
        {"judges": []})
    assert "Instrument clarity" not in html
