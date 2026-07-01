"""Tests for parsing a Harbor job directory (agent_eval/harbor/results.py)."""

import json
from pathlib import Path

from agent_eval.harbor import results as R


def _make_trial(job: Path, name: str, reward: float, metrics: dict,
                per_judge: dict | None = None, errored: bool = False):
    tdir = job / name
    (tdir / "verifier").mkdir(parents=True)
    (tdir / "verifier" / "reward.json").write_text(
        json.dumps({"reward": reward, **metrics}))
    if per_judge is not None:
        (tdir / "verifier" / "judges.json").write_text(
            json.dumps({"reward": reward, "per_judge": per_judge}))
    if errored:
        (tdir / "exception.txt").write_text("boom")
    return tdir


def test_parse_trial_strips_id_and_reads_metrics(tmp_path):
    _make_trial(tmp_path, "case-001-foo__abc123", 0.75,
                {"files_exist": 1.0, "rfe_quality": 4.0},
                per_judge={"files_exist": {"value": True, "rationale": "ok"}})
    trial = R.parse_trial(tmp_path / "case-001-foo__abc123")
    assert trial["case_id"] == "case-001-foo"
    assert trial["reward"] == 0.75
    assert trial["metrics"] == {"files_exist": 1.0, "rfe_quality": 4.0}
    assert trial["per_judge"]["files_exist"]["value"] is True
    assert trial["errored"] is False


def test_parse_trial_none_without_reward(tmp_path):
    (tmp_path / "empty").mkdir()
    assert R.parse_trial(tmp_path / "empty") is None


def test_parse_job_aggregates(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    _make_trial(job, "case-001__a", 1.0, {"files_exist": 1.0, "rfe_quality": 5.0})
    _make_trial(job, "case-002__b", 0.0, {"files_exist": 1.0, "rfe_quality": 3.0},
                errored=True)
    # Non-trial dirs/files are ignored.
    (job / "logs").mkdir()
    (job / "result.json").write_text("{}")

    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 2
    assert parsed["n_errored"] == 1
    assert parsed["mean_reward"] == 0.5
    assert parsed["aggregated"]["files_exist"]["mean"] == 1.0
    assert parsed["aggregated"]["rfe_quality"]["mean"] == 4.0
    case_ids = sorted(t["case_id"] for t in parsed["trials"])
    assert case_ids == ["case-001", "case-002"]


# ---------------------------------------------------------------------------
# Multi-step: distinguish a missing verifier reward (infra/exec failure) from a
# genuine score of 0. A missing reward.json must NOT be counted as 0.
# ---------------------------------------------------------------------------

def _make_step(trial_dir: Path, step: str, reward: float | None = None):
    sdir = trial_dir / "steps" / step
    (sdir / "verifier").mkdir(parents=True)
    if reward is not None:
        (sdir / "verifier" / "reward.json").write_text(json.dumps({"reward": reward}))
    return sdir


def test_multistep_missing_reward_is_infra_not_zero(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    trial = job / "case-015__x"
    trial.mkdir()
    _make_step(trial, "create")  # no reward.json -> verifier never ran

    parsed = R.parse_job(job)
    t = parsed["trials"][0]
    assert t["per_judge"]["create"]["value"] is None        # not False/0
    assert t["per_judge"]["create"]["error"] == "no_verifier_reward"
    assert t["infra_error_steps"] == ["create"]
    assert t["reward"] is None                               # no step scored
    # Excluded from judge aggregation entirely (not a 0).
    assert "create" not in parsed["aggregated"]
    assert parsed["n_infra_errors"] == 1
    assert parsed["infra_errors"] == [("case-015", "create")]


def test_multistep_genuine_zero_is_counted(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    trial = job / "case-001__a"
    trial.mkdir()
    _make_step(trial, "create", reward=0.0)   # ran, scored 0
    _make_step(trial, "submit", reward=1.0)

    parsed = R.parse_job(job)
    t = parsed["trials"][0]
    assert t["per_judge"]["create"]["value"] == 0.0
    assert "error" not in t["per_judge"]["create"]
    assert t["infra_error_steps"] == []
    assert parsed["aggregated"]["create"]["mean"] == 0.0     # genuine 0 counts
    assert t["reward"] == 0.5
    assert parsed["n_infra_errors"] == 0


def test_multistep_infra_excluded_from_step_mean(tmp_path):
    # The real scenario: one case's create verifier ran (1.0), another's didn't.
    # The create mean must be 1.0 (over the one that ran), not 0.5.
    job = tmp_path / "job"
    job.mkdir()
    a = job / "case-001__a"
    a.mkdir()
    _make_step(a, "create", reward=1.0)
    b = job / "case-015__b"
    b.mkdir()
    _make_step(b, "create")  # infra failure

    parsed = R.parse_job(job)
    assert parsed["aggregated"]["create"]["values"] == [1.0]
    assert parsed["aggregated"]["create"]["mean"] == 1.0
    assert parsed["n_infra_errors"] == 1


def test_multistep_infra_step_excludes_trial_from_reward_mean(tmp_path):
    # A case whose create ran (1.0) but auto-fix infra-failed must NOT contribute
    # a perfect 1.0 from its surviving step — the trial is excluded from the
    # reward mean entirely, so only fully-scored cases count.
    job = tmp_path / "job"
    job.mkdir()
    healthy = job / "case-001__a"
    healthy.mkdir()
    _make_step(healthy, "create", reward=1.0)
    _make_step(healthy, "submit", reward=1.0)
    partial = job / "case-006__b"
    partial.mkdir()
    _make_step(partial, "create", reward=1.0)
    _make_step(partial, "auto-fix")  # infra failure (no reward.json)

    parsed = R.parse_job(job)
    rb = next(t for t in parsed["trials"] if t["case_id"] == "case-006")
    assert rb["infra_error_steps"] == ["auto-fix"]
    assert rb["reward"] is None                  # not 1.0 from create-only
    assert parsed["mean_reward"] == 1.0          # only the healthy case counts
    assert parsed["n_completed"] == 2            # still counted as a case
    assert parsed["n_infra_errors"] == 1


# ---------------------------------------------------------------------------
# Trial that failed before producing any reward (e.g. pod never Ready) must be
# surfaced as an errored trial, not silently dropped from the case total.
# ---------------------------------------------------------------------------

def test_trial_failed_before_reward_is_surfaced(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    # A healthy single-step trial.
    _make_trial(job, "case-001__a", 1.0, {"files_exist": 1.0})
    # A trial that never produced steps/ or reward.json but has exception.txt.
    bad = job / "case-013__b"
    bad.mkdir()
    (bad / "exception.txt").write_text("pod aeh-case-013-... not Ready after 300s\n")

    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 2                      # not dropped
    assert parsed["n_trial_errors"] == 1
    assert parsed["trial_errors"][0][0] == "case-013"
    assert "not Ready" in parsed["trial_errors"][0][1]
    bad_trial = next(t for t in parsed["trials"] if t["case_id"] == "case-013")
    assert bad_trial["errored"] is True
    assert bad_trial["reward"] is None
    assert parsed["mean_reward"] == 1.0                    # errored trial excluded


def test_trial_with_no_reward_and_no_exception_is_dropped(tmp_path):
    # Without an exception.txt there's nothing to surface — keep returning None.
    job = tmp_path / "job"
    job.mkdir()
    _make_trial(job, "case-001__a", 1.0, {})
    (job / "case-002__b").mkdir()  # empty, no reward, no exception
    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 1
    assert parsed["n_trial_errors"] == 0


def test_single_step_unreadable_reward_with_exception_is_surfaced(tmp_path):
    # reward.json present but corrupt + exception.txt -> errored trial, not dropped.
    job = tmp_path / "job"
    job.mkdir()
    bad = job / "case-099__z"
    (bad / "verifier").mkdir(parents=True)
    (bad / "verifier" / "reward.json").write_text("{ truncated")  # invalid JSON
    (bad / "exception.txt").write_text("RuntimeError: boom\n")
    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 1
    assert parsed["n_trial_errors"] == 1
    assert parsed["trial_errors"][0] == ("case-099", "RuntimeError: boom")
    t = parsed["trials"][0]
    assert t["errored"] is True and t["reward"] is None


def test_single_step_unreadable_reward_without_exception_is_dropped(tmp_path):
    # Corrupt reward.json but no exception.txt -> still nothing to surface.
    job = tmp_path / "job"
    job.mkdir()
    bad = job / "case-099__z"
    (bad / "verifier").mkdir(parents=True)
    (bad / "verifier" / "reward.json").write_text("{ truncated")
    parsed = R.parse_job(job)
    assert parsed["n_completed"] == 0
    assert parsed["n_trial_errors"] == 0


def test_trial_error_reason_is_sanitized(tmp_path):
    # exception.txt is untrusted: control chars / ANSI / newlines must be escaped
    # and the reason bounded before it reaches run_result.json or CI logs.
    job = tmp_path / "job"
    job.mkdir()
    bad = job / "case-007__z"
    bad.mkdir()
    (bad / "exception.txt").write_text("RuntimeError: \x1b[31mboom\x1b[0m\twith\ttabs\n")
    parsed = R.parse_job(job)
    reason = parsed["trial_errors"][0][1]
    assert "\x1b" not in reason and "\t" not in reason   # raw control chars gone
    assert "\\x1b" in reason                              # escaped form retained
    assert len(reason) <= 200
