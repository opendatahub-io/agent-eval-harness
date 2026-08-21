"""Calibration verdicts pushed as HUMAN-source MLflow assessments.

attach_feedback.py's push mode gains one additive block: when review.yaml
carries `verdicts`, each is logged as `{case_id}/{judge_name}/human` with
source_type HUMAN and source_id = the review's reviewer_id (fallback
'eval-review'). Everything else — including a review.yaml without verdicts —
behaves byte-identically to before.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytest.importorskip("mlflow")  # the script exits at import without it

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-mlflow" / "scripts"))

import attach_feedback  # noqa: E402


@pytest.fixture
def push(tmp_path, monkeypatch):
    """Run _push_feedback against a recorded log_feedback + one fake trace."""
    calls = []

    def _log_feedback(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(attach_feedback, "log_feedback", _log_feedback)
    monkeypatch.setattr(attach_feedback, "find_run_traces",
                        lambda *_a, **_k: [{"trace_id": "t1"}])

    def _run(review=None, summary=None, source="human"):
        run_dir = tmp_path / "run"
        run_dir.mkdir(exist_ok=True)
        if review is not None:
            (run_dir / "review.yaml").write_text(yaml.dump(review))
        if summary is not None:
            (run_dir / "summary.yaml").write_text(yaml.dump(summary))
        args = SimpleNamespace(run_id="r1", trace_id=None, source=source)
        attach_feedback._push_feedback(run_dir, "exp", None, args)
        return calls

    return _run


REVIEW = {
    "run_id": "r1",
    "reviewer_id": "antonin",
    "feedback": {"case-001": "solid", "case-002": ""},
    "verdicts": {
        "case-001": {"format_check": True, "quality": 4},
        "case-002": {"format_check": None},   # None -> skipped
        "case-003": "looks fine",             # non-dict -> skipped
    },
}


def _verdict_calls(calls):
    return [c for c in calls if c["name"].endswith("/human")]


def test_verdicts_are_pushed_as_human_source_assessments(push):
    calls = _verdict_calls(push(review=REVIEW))
    assert {c["name"] for c in calls} == {"case-001/format_check/human",
                                          "case-001/quality/human"}
    for c in calls:
        assert c["source_type"] == "HUMAN"
        assert c["source_id"] == "antonin"    # reviewer_id is the source
        assert c["trace_id"] == "t1"
    by_name = {c["name"]: c["value"] for c in calls}
    assert by_name["case-001/format_check/human"] is True
    assert by_name["case-001/quality/human"] == 4


def test_none_values_and_non_dict_case_entries_are_skipped(push, capsys):
    calls = _verdict_calls(push(review=REVIEW))
    names = {c["name"] for c in calls}
    assert not any(n.startswith("case-002/") for n in names)
    assert not any(n.startswith("case-003/") for n in names)
    assert "case-003" in capsys.readouterr().err  # skipped loudly


def test_missing_reviewer_id_falls_back_to_eval_review(push):
    review = dict(REVIEW)
    del review["reviewer_id"]
    calls = _verdict_calls(push(review=review))
    assert calls and all(c["source_id"] == "eval-review" for c in calls)


def test_a_review_without_verdicts_produces_the_pre_change_call_set(push):
    review = {"run_id": "r1", "feedback": {"case-001": "solid",
                                           "case-002": ""}}
    calls = push(review=review)
    # Exactly the legacy human_review push: one non-empty comment, one trace.
    assert [c["name"] for c in calls] == ["case-001/human_review"]
    assert calls[0]["source_type"] == "HUMAN"
    assert calls[0]["source_id"] == "eval-review"


def test_source_judge_does_not_push_verdicts(push):
    summary = {"per_case": {"case-001": {"format_check": {
        "value": True, "rationale": "ok"}}}}
    calls = push(review=REVIEW, summary=summary, source="judge")
    assert [c["name"] for c in calls] == ["case-001/format_check"]
    assert calls[0]["source_type"] == "CODE"
