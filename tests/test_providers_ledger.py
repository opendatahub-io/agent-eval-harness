"""The per-run provider ledger (spec 014): schema, append/read, filters."""

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.providers.ledger import (  # noqa: E402
    LEDGER_RELPATH, SCHEMA_KEYS, Ledger, ledger_path, make_record, read_ledger, rows_for_case)


def test_make_record_fills_the_schema_and_derives_the_routing_key():
    rec = make_record(role="agent", source="generation", run_id="r", case_id="c",
                      gen_id="gen-1", model_requested="Z-AI/glm-5.2:exacto", cost_usd=0.001,
                      model_echo="z-ai/glm-5.2", message_index=3)
    assert list(rec) == list(SCHEMA_KEYS)
    assert rec["model"] == "z-ai/glm-5.2"
    assert rec["status"] == "ok" and rec["provider_kind"] == "openrouter"
    assert rec["ts"].endswith("Z")
    assert rec["audit"] is None and rec["tool_choice_mode"] is None


def test_make_record_truncates_messages_drops_unknown_keys_and_validates_enums():
    rec = make_record(role="judge", source="judge", judge="q", error_message="x" * 500,
                      metadata={"raw": "never stored"}, cost_usd="not-a-number")
    assert len(rec["error_message"]) == 200
    assert "metadata" not in rec
    assert rec["cost_usd"] is None
    with pytest.raises(ValueError, match="role"):
        make_record(role="spectator", source="generation")
    with pytest.raises(ValueError, match="source"):
        make_record(role="agent", source="guess")
    with pytest.raises(ValueError, match="status"):
        make_record(role="agent", source="generation", status="maybe")
    with pytest.raises(ValueError, match="audit"):
        make_record(role="agent", source="generation", audit="fine")


def test_append_and_read_round_trip(tmp_path):
    ledger = Ledger.for_run(tmp_path)
    assert ledger.path == tmp_path / LEDGER_RELPATH == ledger_path(tmp_path)
    assert not ledger.exists() and ledger.read() == []
    a = ledger.append(make_record(role="agent", source="generation", run_id="r", case_id="c1",
                                  gen_id="gen-1", cost_usd=0.01))
    ledger.append(make_record(role="hook", source="generation", run_id="r", case_id="c1",
                              gen_id="gen-2", status="backfill_failed"))
    ledger.append(make_record(role="key-usage", source="key-usage", run_id="r", cost_usd=0.5))
    ledger.append({"role": "judge", "source": "judge", "run_id": "r", "case_id": "c2",
                   "judge": "q", "cost_usd": 0.002})           # partial dict is normalised
    lines = ledger.path.read_text().splitlines()
    assert len(lines) == 4 and json.loads(lines[0]) == a
    assert oct(ledger.path.stat().st_mode & 0o777) == "0o600"
    assert [r["gen_id"] for r in ledger.read(case_id="c1")] == ["gen-1", "gen-2"]
    assert [r["role"] for r in ledger.read(role="key-usage")] == ["key-usage"]
    assert [r["gen_id"] for r in ledger.read(status="backfill_failed")] == ["gen-2"]
    assert len(read_ledger(tmp_path, run_id="r")) == 4 and read_ledger(tmp_path, run_id="x") == []
    assert [r["case_id"] for r in rows_for_case(ledger.read(), "c2")] == ["c2"]
    assert rows_for_case(ledger.read(), None) == []       # run-level rows belong to no case


def test_read_skips_corrupt_lines(tmp_path):
    ledger = Ledger.for_run(tmp_path)
    ledger.append(make_record(role="agent", source="generation", gen_id="gen-1"))
    with open(ledger.path, "a") as f:
        f.write("{not json\n[1,2]\n")
    assert [r["gen_id"] for r in ledger.read()] == ["gen-1"]


def test_concurrent_appends_interleave_whole_lines(tmp_path):
    ledger = Ledger.for_run(tmp_path)

    def worker(n):
        for i in range(50):
            ledger.append(make_record(role="agent", source="generation", gen_id=f"gen-{n}-{i}",
                                      error_message="m" * 150))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = ledger.read()
    assert len(rows) == 200 and len({r["gen_id"] for r in rows}) == 200
