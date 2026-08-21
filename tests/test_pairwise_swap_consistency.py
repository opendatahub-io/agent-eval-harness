"""Pairwise position-swap persistence (measurement-validity PR2, slim scope).

compare_runs keeps the raw pref_ab/pref_ba verdicts per case (they used to be
dropped once the winner was derived) and reports a swap_consistency rate.
Errored comparisons are excluded from the rate denominator, never counted as
a verdict category. Headline wins/ties counts are unchanged; no pairwise
alpha and no exit-code changes (both deferred).
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import score  # noqa: E402
from score import PairwiseResult, _swap_consistency  # noqa: E402


def _r(case_id, ab=None, ba=None, error=None):
    return PairwiseResult(case_id=case_id, pref_ab=ab, pref_ba=ba, error=error)


def test_consistent_pairs_are_the_derivable_winners():
    results = [
        _r("c1", "A", "B"),      # A wins — consistent
        _r("c2", "B", "A"),      # B wins — consistent
        _r("c3", "tie", "tie"),  # tie — consistent
        _r("c4", "A", "A"),      # position bias — inconsistent
        _r("c5", "tie", "B"),    # partial — inconsistent
    ]
    sc = _swap_consistency(results)
    assert sc == {"consistent": 3, "inconsistent": 2, "errors": 0,
                  "rate": 0.6}


def test_errors_are_excluded_from_the_denominator():
    results = [
        _r("c1", "A", "B"),
        _r("c2", "A", "A"),
        _r("c3", error="AB failed: boom"),  # winner == "error"
        _r("c4", "A", None),                # missing BA -> winner "error"
    ]
    sc = _swap_consistency(results)
    assert sc["errors"] == 2
    assert sc["consistent"] + sc["inconsistent"] == 2
    assert sc["rate"] == 0.5  # 1 of 2 non-errored, errors never in the rate


def test_all_errored_run_has_no_rate():
    sc = _swap_consistency([_r("c1", error="x"), _r("c2", error="y")])
    assert sc["rate"] is None
    assert sc["errors"] == 2


def test_compare_runs_persists_prefs_and_swap_consistency(tmp_path, monkeypatch):
    """per_case carries pref_ab/pref_ba verbatim; headline counts unchanged."""
    scripted = {
        "c1": ("A", "B"),    # A
        "c2": ("A", "A"),    # tie (position bias), inconsistent
        "c3": ("tie", "tie"),
    }

    monkeypatch.setattr(score, "_get_anthropic_client", lambda: object())
    monkeypatch.setattr(score, "load_case_record",
                        lambda case_dir, config: {"case": Path(case_dir).name})
    monkeypatch.setattr(score, "_format_outputs_for_pairwise",
                        lambda record: f"output of {record['case']}")

    calls = {}

    def fake_call_judge(client, prompt, message, model):
        # First call per case is AB, second is BA — keyed on the A-side text.
        for cid in scripted:
            if cid in message:
                n = calls.get(cid, 0)
                calls[cid] = n + 1
                return ({"preferred": scripted[cid][n],
                         "reasoning": f"{cid} call {n}"}, None)
        raise AssertionError(f"unexpected message: {message}")

    monkeypatch.setattr(score, "_call_judge", fake_call_judge)

    run_a = tmp_path / "run-a"
    run_b = tmp_path / "run-b"
    result = score.compare_runs(run_a, run_b, config=None,
                                case_ids=["c1", "c2", "c3"], model="m")

    per_case = {pc["case_id"]: pc for pc in result["per_case"]}
    assert per_case["c1"]["pref_ab"] == "A" and per_case["c1"]["pref_ba"] == "B"
    assert per_case["c2"]["pref_ab"] == "A" and per_case["c2"]["pref_ba"] == "A"
    assert per_case["c1"]["winner"] == "A"
    # Headline counts unchanged: swap-inconsistency still folds into ties.
    assert (result["wins_a"], result["wins_b"], result["ties"],
            result["errors"]) == (1, 0, 2, 0)
    assert result["swap_consistency"] == {
        "consistent": 2, "inconsistent": 1, "errors": 0, "rate": 0.667}
