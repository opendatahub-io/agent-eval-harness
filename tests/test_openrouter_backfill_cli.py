"""The offline backfill command (spec 014): re-query the run's unpriced ids
and re-reconcile the run dir, over the loopback fake."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.providers.openrouter import backfill as bf  # noqa: E402
from agent_eval.providers.openrouter.session import ProviderSession  # noqa: E402
from agent_eval.providers.reconcile import write_run_result  # noqa: E402
from openrouter_fakes import FAKE_KEY, GEN_COST, FakeOpenRouter, make_plan  # noqa: E402


def _interrupted_run(tmp_path, plan):
    """A run whose case was written but whose generations never landed."""
    case = write_run_result(tmp_path / "cases" / "c1" / "run_result.json",
                            {"exit_code": 0, "cost_usd": 0.4, "message_ids": ["gen-fake-a", "gen-fake-b"]},
                            plan=plan, ledger=[])
    write_run_result(tmp_path / "run_result.json",
                     {"exit_code": 0, "cost_usd": None, "per_case": {"c1": case},
                      "message_ids": ["gen-fake-a", "gen-fake-b"], "eval_params": {"max_budget_usd": 1}},
                     plan=plan, ledger=[])


def test_offline_backfill_prices_and_re_reconciles(tmp_path, monkeypatch, capsys):
    fake = FakeOpenRouter()
    base = fake.start()
    try:
        plan = make_plan(base)
        _interrupted_run(tmp_path, plan)
        assert json.loads((tmp_path / "run_result.json").read_text())["cost_source"] == "unavailable"
        monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
        assert bf.main([str(tmp_path), "--give-up-s", "1"]) == 0
    finally:
        fake.stop()
    assert "re-queried 2 id(s), priced 2, failed 0" in capsys.readouterr().out
    run = json.loads((tmp_path / "run_result.json").read_text())
    assert run["cost_source"] == "openrouter:generation" and run["cost_usd"] == pytest.approx(2 * GEN_COST, abs=1e-6)
    assert run["provider"]["base_url"] == base                      # the previous blocks are kept
    case = json.loads((tmp_path / "cases" / "c1" / "run_result.json").read_text())
    assert case["cost_source"] == "openrouter:generation" and case["cost_usd_estimate"] == 0.4
    assert bf.collect_sightings(tmp_path) == [{"gen_id": "gen-fake-a", "case_id": "c1"},
                                              {"gen_id": "gen-fake-b", "case_id": "c1"}]


def test_offline_backfill_needs_the_key_and_a_run(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert bf.main([str(tmp_path)]) == 2
    assert "set OPENROUTER_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
    assert bf.main([str(tmp_path)]) == 2
    assert "no run_result.json" in capsys.readouterr().err
    assert isinstance(ProviderSession, type)          # the session module is the online counterpart
