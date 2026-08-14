"""The report and the MLflow tag must agree with the CLI's exit code.

`score.py::detect_regressions` is the gate CI exits on. `report.py` and
`log_results.py` each reimplemented a subset of its rules and had drifted to
two of the four threshold keys, so a run could exit 1 on `min_win_rate` or
`max_error_rate` while report.html showed no Regressions table and MLflow
tagged `regressions_detected=no`. Both now call the detector.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import report  # noqa: E402
from score import detect_regressions  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# judges aggregate, thresholds, and whether the CLI would regress
CASES = [
    ("min_mean", {"q": {"mean": 0.5, "scored_cases": 10}},
     {"q": {"min_mean": 1.0}}, True),
    ("min_pass_rate", {"q": {"pass_rate": 0.5}},
     {"q": {"min_pass_rate": 0.9}}, True),
    ("min_win_rate", {"pairwise": {"win_rate": 0.3}},
     {"pairwise": {"min_win_rate": 0.6}}, True),
    ("max_error_rate", {"q": {"scored_cases": 1, "errored_cases": 9}},
     {"q": {"max_error_rate": 0.2}}, True),
    ("clean", {"q": {"mean": 4.0, "scored_cases": 10, "errored_cases": 0}},
     {"q": {"min_mean": 1.0}}, False),
]


@pytest.mark.parametrize("key,judges,thresholds,regresses", CASES)
def test_the_cli_gate_behaves_as_the_table_says(key, judges, thresholds, regresses):
    assert bool(detect_regressions(judges, thresholds)) is regresses


@pytest.mark.parametrize("key,judges,thresholds,regresses", CASES)
def test_the_report_shows_every_breach_the_cli_exits_on(key, judges, thresholds,
                                                        regresses):
    html = report._render_regressions({"judges": judges},
                                      {"thresholds": thresholds})
    assert bool(html) is regresses, f"{key}: report and CLI disagree"
    if regresses:
        assert "Regressions" in html


@pytest.mark.parametrize("key,judges,thresholds,regresses", CASES)
def test_the_mlflow_tag_matches_the_cli(key, judges, thresholds, regresses):
    log_results = _load(
        REPO_ROOT / "skills" / "eval-mlflow" / "scripts" / "log_results.py",
        "_log_results_under_test")
    assert bool(log_results._detect_regressions(judges, thresholds)) is regresses


def test_a_judge_gated_only_on_coverage_is_not_reported_as_passing():
    """The summary table picks one bound to display; the PASS/FAIL verdict has
    to come from the detector, or a judge whose only gate is `max_error_rate`
    renders green while the run exits 1."""
    summary = {"judges": {"q": {"mean": 4.0, "scored_cases": 1,
                                "errored_cases": 9}}, "per_case": {}}
    html = report._render_scoring_summary(
        summary, {"thresholds": {"q": {"max_error_rate": 0.2}}, "judges": []})
    assert "FAIL" in html and ">PASS<" not in html


# --- a metric the table cannot display is still an authoritative breach ------

def _statuses(judges, thresholds):
    import re
    html = report._render_scoring_summary({"judges": judges, "per_case": {}},
                                          {"thresholds": thresholds, "judges": []})
    return re.findall(r">(PASS|FAIL|SKIP|ERROR)<", html)


@pytest.mark.parametrize("label,judges,thresholds,expected", [
    # win_rate and error rate are not columns in this table, so the old
    # "no mean and no pass_rate" branch swallowed them into SKIP.
    ("win_rate breach", {"pairwise": {"win_rate": 0.3}},
     {"pairwise": {"min_win_rate": 0.6}}, "FAIL"),
    ("win_rate satisfied", {"pairwise": {"win_rate": 0.8}},
     {"pairwise": {"min_win_rate": 0.6}}, "PASS"),
    ("coverage breach, every case errored",
     {"q": {"mean": None, "pass_rate": None, "scored_cases": 0,
            "errored_cases": 10}}, {"q": {"max_error_rate": 0.2}}, "FAIL"),
    ("min_mean with no surviving case",
     {"q": {"mean": None, "pass_rate": None, "scored_cases": 0,
            "errored_cases": 10}}, {"q": {"min_mean": 1.0}}, "FAIL"),
    ("clean numeric", {"q": {"mean": 4.0, "scored_cases": 10}},
     {"q": {"min_mean": 1.0}}, "PASS"),
])
def test_the_summary_status_matches_the_detector(label, judges, thresholds,
                                                 expected):
    assert _statuses(judges, thresholds) == [expected], label


# --- a detector that cannot run is not a clean run ---------------------------

def _break_detector(monkeypatch, module):
    def boom(*_a, **_k):
        raise RuntimeError("engine unavailable")
    monkeypatch.setattr(module, "_detect_regressions", boom)


def test_the_regressions_section_says_so_when_it_cannot_evaluate(monkeypatch):
    _break_detector(monkeypatch, report)
    html = report._render_regressions({"judges": {"q": {"mean": 0.5}}},
                                      {"thresholds": {"q": {"min_mean": 1.0}}})
    assert "Could not evaluate thresholds" in html
    assert "engine unavailable" in html


def test_the_summary_does_not_claim_pass_when_it_cannot_evaluate(monkeypatch):
    """An empty breach set has to mean "checked, none" — not "never checked"."""
    _break_detector(monkeypatch, report)
    assert _statuses({"q": {"mean": 4.0, "scored_cases": 10}},
                     {"q": {"min_mean": 1.0}}) != ["PASS"]


def test_the_mlflow_helper_raises_rather_than_reporting_clean(monkeypatch):
    """It used to swallow every failure and return [], which the caller then
    tagged `regressions_detected=no` — indistinguishable from a clean run."""
    log_results = _load(
        REPO_ROOT / "skills" / "eval-mlflow" / "scripts" / "log_results.py",
        "_log_results_tag_test")

    # A working call still returns a list.
    assert log_results._detect_regressions(
        {"q": {"mean": 0.5, "scored_cases": 10}}, {"q": {"min_mean": 1.0}})

    # An unreachable engine raises instead of looking clean.
    monkeypatch.setattr(log_results, "Path", _MissingEnginePath)
    with pytest.raises(FileNotFoundError):
        log_results._detect_regressions({"q": {"mean": 1.0}},
                                        {"q": {"min_mean": 1.0}})


class _MissingEnginePath(type(Path())):
    """A Path whose resolved root has no score.py."""

    def resolve(self):
        return Path("/nonexistent-harness-root/skills/x/scripts/log_results.py")
