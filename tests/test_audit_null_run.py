"""Tests for the null-agent solvability probe (audit_dataset.py --null-run).

The probe reads a scored null run's summary.yaml per_case records (never a
stored reward — the composite is RECOMPUTED via
agent_eval.harbor.reward.compose_reward) and merges a ``null_probe`` section
into dataset_audit.yaml via the load-and-merge write path.
"""

import sys
from pathlib import Path

import pytest
import yaml

from agent_eval.config import EvalConfig
from agent_eval.dataset_audit import (
    AUDIT_FILENAME,
    DEFAULT_NULL_REWARD_THRESHOLD,
    NULL_PROBE_LABEL,
    NullRunError,
    audit_null_run,
    load_audit,
    run_audit,
    write_audit,
    write_null_probe,
)
from agent_eval.harbor.reward import compose_reward, judge_ranges

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_LABEL = ("null-pass rate (joint task/judge non-discriminativeness, "
                  "upper-bounds 1−V1)")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def make_config(tmp_path, *, judges=None, reward=None):
    """Write a minimal eval.yaml under tmp_path and load it."""
    config_data = {
        "name": "null-probe-test",
        "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
        "dataset": {"path": "dataset", "schema": "input.yaml with prompt"},
        "outputs": [{"path": "output", "schema": "stdout"}],
    }
    if judges is not None:
        config_data["judges"] = judges
    if reward is not None:
        config_data["reward"] = reward
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(yaml.safe_dump(config_data))
    (tmp_path / "dataset").mkdir(exist_ok=True)
    return EvalConfig.from_yaml(config_path)


def make_case(dataset_dir, name):
    case = dataset_dir / name
    case.mkdir(parents=True, exist_ok=True)
    (case / "input.yaml").write_text("prompt: q\n")
    return case


def write_summary(run_dir, per_case):
    """summary.yaml in score.py's shape — per_case only, NEVER a reward key."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.yaml").write_text(yaml.safe_dump(
        {"run_id": run_dir.name, "per_case": per_case}))
    return run_dir


BOOL_JUDGE = {"name": "passes", "llm_rubric": "did it pass",
              "feedback_type": "bool"}
NUMERIC_JUDGE = {"name": "quality", "llm_rubric": "rate it",
                 "score_range": [1, 5]}
CHECK_JUDGE = {"name": "has_output", "feedback_type": "bool",
               "check": "return True, 'ok'"}


# ---------------------------------------------------------------------------
# Flagging: bool passes
# ---------------------------------------------------------------------------

class TestBoolPassFlagging:
    def test_bool_pass_flagged_with_judge_and_rationale(self, tmp_path):
        config = make_config(tmp_path, judges=[BOOL_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "passes": {"value": True, "rationale": "vacuously true",
                       "judge_type": "llm"}}})
        probe = audit_null_run(run, config)
        rec = probe["cases"]["case-001"]
        assert rec["null_pass"] is True
        assert rec["passing_bool_judges"][0]["judge"] == "passes"
        assert rec["passing_bool_judges"][0]["rationale"] == "vacuously true"
        assert probe["null_pass_rate"] == 1.0

    def test_failing_bool_not_flagged(self, tmp_path):
        config = make_config(tmp_path, judges=[BOOL_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "passes": {"value": False, "rationale": "no output",
                       "judge_type": "llm"}}})
        probe = audit_null_run(run, config)
        assert probe["cases"]["case-001"]["null_pass"] is False
        assert probe["null_pass_rate"] == 0.0

    def test_skipped_and_errored_never_pass(self, tmp_path):
        """if:-skipped (value None + 'Skipped:' rationale) and errored
        (error key) records never count — not even via the recomputed
        reward (an all-skipped case composes to the vacuous gates-only
        1.0 and must not flag on it)."""
        config = make_config(tmp_path, judges=[BOOL_JUDGE, CHECK_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "passes": {"value": None,
                       "rationale": "Skipped: condition 'x' is false",
                       "judge_type": "llm"},
            "has_output": {"value": None, "error": "boom",
                           "judge_type": "check"},
        }})
        probe = audit_null_run(run, config)
        rec = probe["cases"]["case-001"]
        assert rec["null_pass"] is False
        assert rec["passing_bool_judges"] == []
        assert probe["null_pass_rate"] == 0.0

    def test_errored_true_value_never_passes(self, tmp_path):
        """A record with an error key never counts, whatever its value."""
        config = make_config(tmp_path, judges=[BOOL_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "passes": {"value": True, "error": "all samples failed",
                       "judge_type": "llm"}}})
        probe = audit_null_run(run, config)
        assert probe["cases"]["case-001"]["passing_bool_judges"] == []

    def test_numeric_one_is_not_a_bool_pass(self, tmp_path):
        config = make_config(tmp_path, judges=[NUMERIC_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "quality": {"value": 1, "rationale": "bottom of scale",
                        "judge_type": "llm"}}})
        probe = audit_null_run(run, config)
        rec = probe["cases"]["case-001"]
        assert rec["passing_bool_judges"] == []
        assert rec["null_pass"] is False  # (1-1)/4 = 0.0 < 0.5


# ---------------------------------------------------------------------------
# Flagging: recomputed reward
# ---------------------------------------------------------------------------

class TestRewardRecomputation:
    def test_reward_recomputed_via_compose_reward(self, tmp_path):
        """Score 4 on [1,5] -> 0.75 >= 0.5 default -> flagged via reward.
        The value must match compose_reward's own output (summary.yaml
        stores no reward anywhere)."""
        config = make_config(tmp_path, judges=[NUMERIC_JUDGE])
        per_judge = {"quality": {"value": 4, "rationale": "good",
                                 "judge_type": "llm"}}
        run = write_summary(tmp_path / "run", {"case-001": per_judge})
        probe = audit_null_run(run, config)
        rec = probe["cases"]["case-001"]
        expected, _ = compose_reward(
            per_judge, reward_cfg=config.reward,
            judge_ranges=judge_ranges(config))
        assert rec["reward"] == round(expected, 4) == 0.75
        assert rec["null_pass"] is True
        assert rec["passing_bool_judges"] == []

    def test_reward_threshold_flag(self, tmp_path):
        """A higher threshold un-flags the same case; recorded verbatim."""
        config = make_config(tmp_path, judges=[NUMERIC_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "quality": {"value": 4, "judge_type": "llm"}}})
        probe = audit_null_run(run, config, reward_threshold=0.8)
        assert probe["reward_threshold"] == 0.8
        assert probe["cases"]["case-001"]["null_pass"] is False
        assert probe["cases"]["case-001"]["reward"] == 0.75

    def test_default_threshold_is_fixed_half(self):
        """Fixed 0.5 — never derived from thresholds/normalization."""
        assert DEFAULT_NULL_REWARD_THRESHOLD == 0.5

    def test_bool_gate_zeroes_reward_but_bool_pass_still_flags(self, tmp_path):
        """A failing bool gates the default composite to 0.0, yet a passing
        bool judge flags the case independently of the reward clause."""
        config = make_config(tmp_path, judges=[
            BOOL_JUDGE, CHECK_JUDGE, NUMERIC_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "passes": {"value": True, "rationale": "vacuous",
                       "judge_type": "llm"},
            "has_output": {"value": False, "rationale": "empty",
                           "judge_type": "check"},
            "quality": {"value": 5, "judge_type": "llm"},
        }})
        probe = audit_null_run(run, config)
        rec = probe["cases"]["case-001"]
        assert rec["reward"] == 0.0
        assert rec["null_pass"] is True
        assert [e["judge"] for e in rec["passing_bool_judges"]] == ["passes"]

    def test_reward_section_respected(self, tmp_path):
        """A reward: section replaces the default composition."""
        config = make_config(
            tmp_path, judges=[NUMERIC_JUDGE],
            reward={"judge": "quality", "normalize": True})
        run = write_summary(tmp_path / "run", {"case-001": {
            "quality": {"value": 3, "judge_type": "llm"}}})
        probe = audit_null_run(run, config)
        assert probe["cases"]["case-001"]["reward"] == 0.5  # (3-1)/4
        assert probe["cases"]["case-001"]["null_pass"] is True


# ---------------------------------------------------------------------------
# Low-confidence marking (single-sample stochastic verdicts)
# ---------------------------------------------------------------------------

class TestLowConfidence:
    def test_unsampled_llm_pass_is_low_confidence(self, tmp_path):
        config = make_config(tmp_path, judges=[BOOL_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "passes": {"value": True, "rationale": "ok",
                       "judge_type": "llm"}}})
        probe = audit_null_run(run, config)
        rec = probe["cases"]["case-001"]
        assert rec["low_confidence"] is True
        assert rec["passing_bool_judges"][0]["low_confidence"] is True

    def test_sampled_llm_pass_is_not_low_confidence(self, tmp_path):
        config = make_config(tmp_path, judges=[BOOL_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "passes": {"value": True, "rationale": "ok", "judge_type": "llm",
                       "stability": {"samples": 3, "pass_count": 3,
                                     "error_count": 0,
                                     "values": [True, True, True],
                                     "stable": True}}}})
        probe = audit_null_run(run, config)
        rec = probe["cases"]["case-001"]
        assert rec["low_confidence"] is False
        assert rec["passing_bool_judges"][0]["low_confidence"] is False
        assert rec["passing_bool_judges"][0]["samples"] == 3

    def test_unsampled_agent_pass_is_low_confidence(self, tmp_path):
        config = make_config(tmp_path, judges=[BOOL_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "passes": {"value": True, "judge_type": "agent"}}})
        probe = audit_null_run(run, config)
        assert probe["cases"]["case-001"]["low_confidence"] is True

    def test_deterministic_check_pass_is_never_low_confidence(self, tmp_path):
        config = make_config(tmp_path, judges=[CHECK_JUDGE])
        run = write_summary(tmp_path / "run", {"case-001": {
            "has_output": {"value": True, "rationale": "ok",
                           "judge_type": "check"}}})
        probe = audit_null_run(run, config)
        rec = probe["cases"]["case-001"]
        assert rec["null_pass"] is True
        assert rec["low_confidence"] is False

    def test_builtin_llm_pass_is_low_confidence(self, tmp_path):
        """Builtin LLM judges are pinned to n=1 at scoring time — always a
        single-sample stochastic verdict."""
        config = make_config(tmp_path, judges=[
            {"name": "completeness", "builtin": "quality/output_completeness"}])
        run = write_summary(tmp_path / "run", {"case-001": {
            "completeness": {"value": True, "rationale": "covers all",
                             "judge_type": "builtin"}}})
        probe = audit_null_run(run, config)
        assert probe["cases"]["case-001"]["low_confidence"] is True

    def test_builtin_python_pass_is_not_low_confidence(self, tmp_path):
        config = make_config(tmp_path, judges=[
            {"name": "consulted", "builtin": "process/consulted_docs"}])
        run = write_summary(tmp_path / "run", {"case-001": {
            "consulted": {"value": True,
                          "rationale": "No expected_files specified — "
                                       "nothing to verify",
                          "judge_type": "builtin"}}})
        probe = audit_null_run(run, config)
        rec = probe["cases"]["case-001"]
        assert rec["null_pass"] is True  # the vacuous-PASS triage example
        assert rec["low_confidence"] is False


# ---------------------------------------------------------------------------
# Label + block shape
# ---------------------------------------------------------------------------

class TestLabelAndShape:
    def test_statistic_label_exact(self):
        assert NULL_PROBE_LABEL == EXPECTED_LABEL
        assert "−" in NULL_PROBE_LABEL  # U+2212 minus, not a hyphen

    def test_probe_block_shape(self, tmp_path):
        config = make_config(tmp_path, judges=[BOOL_JUDGE])
        run = write_summary(tmp_path / "run", {
            "case-001": {"passes": {"value": True, "judge_type": "llm"}},
            "case-002": {"passes": {"value": False, "judge_type": "llm"}},
        })
        probe = audit_null_run(run, config,
                               now="2026-08-21T00:00:00+00:00")
        assert probe["run_dir"] == str(run)
        assert probe["generated_at"] == "2026-08-21T00:00:00+00:00"
        assert probe["reward_threshold"] == 0.5
        assert probe["label"] == EXPECTED_LABEL
        assert probe["null_pass_rate"] == 0.5
        assert set(probe["cases"]) == {"case-001", "case-002"}
        for rec in probe["cases"].values():
            assert set(rec) == {"null_pass", "passing_bool_judges",
                                "reward", "low_confidence"}


# ---------------------------------------------------------------------------
# Merge semantics: null_probe <-> full audit round-trip, both directions
# ---------------------------------------------------------------------------

class TestMergeRoundTrip:
    def _probe(self, tmp_path, config):
        run = write_summary(tmp_path / "run", {"case-001": {
            "passes": {"value": True, "judge_type": "llm"}}})
        return audit_null_run(run, config, now="2026-08-21T00:00:00+00:00")

    def test_null_probe_merge_preserves_existing_audit_keys(self, tmp_path):
        config = make_config(tmp_path, judges=[BOOL_JUDGE])
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001")
        write_audit(run_audit(config, now="2026-08-20T00:00:00+00:00"),
                    dataset)
        before = load_audit(dataset)

        probe = self._probe(tmp_path, config)
        write_null_probe(probe, dataset)

        merged = load_audit(dataset)
        assert merged["null_probe"] == probe
        for key in ("audit_version", "generated_at", "dataset_path",
                    "parameters", "checks", "summary", "cases",
                    "case_hashes"):
            assert merged[key] == before[key]

    def test_full_reaudit_preserves_null_probe(self, tmp_path):
        """Round-trip the other direction: write_audit after the probe."""
        config = make_config(tmp_path, judges=[BOOL_JUDGE])
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001")
        probe = self._probe(tmp_path, config)
        write_null_probe(probe, dataset)  # audit file did not exist yet

        write_audit(run_audit(config, now="2026-08-22T00:00:00+00:00"),
                    dataset)
        merged = load_audit(dataset)
        assert merged["null_probe"] == probe
        assert merged["generated_at"] == "2026-08-22T00:00:00+00:00"

    def test_probe_rerun_replaces_null_probe_wholesale(self, tmp_path):
        config = make_config(tmp_path, judges=[BOOL_JUDGE])
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001")
        write_null_probe(self._probe(tmp_path, config), dataset)

        run2 = write_summary(tmp_path / "run2", {"case-001": {
            "passes": {"value": False, "judge_type": "llm"}}})
        probe2 = audit_null_run(run2, config,
                                now="2026-08-23T00:00:00+00:00")
        write_null_probe(probe2, dataset)
        merged = load_audit(dataset)
        assert merged["null_probe"] == probe2
        assert merged["null_probe"]["null_pass_rate"] == 0.0


# ---------------------------------------------------------------------------
# Error paths (engine)
# ---------------------------------------------------------------------------

class TestErrorPaths:
    def test_missing_summary_raises(self, tmp_path):
        config = make_config(tmp_path)
        run = tmp_path / "run"
        run.mkdir()
        with pytest.raises(NullRunError, match="no summary.yaml"):
            audit_null_run(run, config)

    def test_missing_per_case_raises_with_scoring_guidance(self, tmp_path):
        config = make_config(tmp_path)
        run = tmp_path / "run"
        run.mkdir()
        (run / "summary.yaml").write_text("run_id: run\n")
        with pytest.raises(NullRunError, match="--samples 3"):
            audit_null_run(run, config)

    def test_batch_run_detected_in_guidance(self, tmp_path):
        config = make_config(tmp_path)
        run = tmp_path / "run"
        run.mkdir()
        (run / "summary.yaml").write_text("run_id: run\n")
        (run / "run_result.json").write_text('{"execution_mode": "batch"}')
        with pytest.raises(NullRunError, match="batch-mode run"):
            audit_null_run(run, config)


# ---------------------------------------------------------------------------
# CLI (--null-run)
# ---------------------------------------------------------------------------

class TestCLI:
    def _main(self):
        sys.path.insert(0, str(
            REPO_ROOT / "skills" / "eval-dataset" / "scripts"))
        from audit_dataset import main
        return main

    def _setup(self, tmp_path, value=True):
        make_config(tmp_path, judges=[BOOL_JUDGE])
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001")
        run = write_summary(tmp_path / "run", {"case-001": {
            "passes": {"value": value, "rationale": "so it goes",
                       "judge_type": "llm"}}})
        return tmp_path / "eval.yaml", dataset, run

    def test_exit_zero_on_null_pass_by_default(self, tmp_path, monkeypatch,
                                               capsys):
        config_path, dataset, run = self._setup(tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(config_path), "--null-run", str(run)])
        self._main()()  # no SystemExit -> exit 0 (findings, not verdicts)
        out = capsys.readouterr().out
        assert f"{EXPECTED_LABEL}: 100.0% (1/1)" in out
        assert "fix the case" in out and "fix the judge" in out
        assert (dataset / AUDIT_FILENAME).is_file()
        assert load_audit(dataset)["null_probe"]["null_pass_rate"] == 1.0

    def test_fail_on_null_pass_exits_one(self, tmp_path, monkeypatch):
        config_path, _, run = self._setup(tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(config_path), "--null-run", str(run),
                             "--fail-on-null-pass"])
        with pytest.raises(SystemExit) as exc:
            self._main()()
        assert exc.value.code == 1

    def test_fail_on_null_pass_passes_clean_run(self, tmp_path, monkeypatch):
        config_path, _, run = self._setup(tmp_path, value=False)
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(config_path), "--null-run", str(run),
                             "--fail-on-null-pass"])
        self._main()()  # zero null-passes -> no SystemExit

    def test_reward_threshold_flag_recorded(self, tmp_path, monkeypatch):
        make_config(tmp_path, judges=[NUMERIC_JUDGE])
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001")
        run = write_summary(tmp_path / "run", {"case-001": {
            "quality": {"value": 4, "judge_type": "llm"}}})
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(tmp_path / "eval.yaml"),
                             "--null-run", str(run),
                             "--reward-threshold", "0.8"])
        self._main()()
        probe = load_audit(dataset)["null_probe"]
        assert probe["reward_threshold"] == 0.8
        assert probe["cases"]["case-001"]["null_pass"] is False

    def test_unscored_run_exits_two(self, tmp_path, monkeypatch, capsys):
        config_path, _, _ = self._setup(tmp_path)
        empty_run = tmp_path / "empty-run"
        empty_run.mkdir()
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(config_path), "--null-run", str(empty_run)])
        with pytest.raises(SystemExit) as exc:
            self._main()()
        assert exc.value.code == 2
        assert "--samples 3" in capsys.readouterr().err

    def test_batch_run_exits_two_with_guidance(self, tmp_path, monkeypatch,
                                               capsys):
        config_path, _, _ = self._setup(tmp_path)
        batch_run = tmp_path / "batch-run"
        batch_run.mkdir()
        (batch_run / "summary.yaml").write_text("run_id: batch-run\n")
        (batch_run / "run_result.json").write_text(
            '{"execution_mode": "batch"}')
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(config_path), "--null-run", str(batch_run)])
        with pytest.raises(SystemExit) as exc:
            self._main()()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "batch-mode run" in err
        assert "batch-mode limitation" in err

    def test_out_of_range_threshold_exits_two(self, tmp_path, monkeypatch):
        config_path, _, run = self._setup(tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(config_path), "--null-run", str(run),
                             "--reward-threshold", "1.5"])
        with pytest.raises(SystemExit) as exc:
            self._main()()
        assert exc.value.code == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
