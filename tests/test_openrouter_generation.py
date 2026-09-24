"""The /generation backfill and key-usage readers (spec 014), against a stub
fetcher and a fake clock — no network, no sleeping."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.providers.ledger import Ledger  # noqa: E402
from agent_eval.providers.openrouter.catalog import ModelCatalog  # noqa: E402
from agent_eval.providers.openrouter.generation import (  # noqa: E402
    Backfill, KeyUsageDelta, coverage, is_generation_id, parse_generation,
    read_key_usage, read_key_usage_settled)
from agent_eval.providers.openrouter.http import OpenRouterHTTPError  # noqa: E402

PROBE_12_COST = 0.00165633   # the recorded /generation total_cost of probe #12's gen id


def _gen_payload(gen_id, cost=PROBE_12_COST, provider="Novita", model="z-ai/glm-5.2-20260616"):
    return {"data": {"id": gen_id, "total_cost": cost, "provider_name": provider, "model": model,
                     "tokens_prompt": 1200, "tokens_completion": 340,
                     "native_tokens_prompt": 1250, "native_tokens_completion": 340,
                     "native_tokens_reasoning": 120, "finish_reason": "tool_use",
                     "native_finish_reason": "tool_calls", "latency": 20431,
                     "generation_time": 19600, "streamed": True, "is_byok": False,
                     "upstream_inference_cost": None}}


class _Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


class _Stub:
    """Scripted /generation server: `available_at[gen_id] = clock time` decides
    when a 404 turns into a 200; `errors[gen_id]` overrides with an exception."""

    def __init__(self, clock, available_at=None, errors=None, payloads=None):
        self.clock = clock
        self.available_at = available_at or {}
        self.errors = errors or {}
        self.payloads = payloads or {}
        self.calls = []

    def __call__(self, url):
        gen_id = url.split("id=", 1)[1]
        self.calls.append((round(self.clock.now, 1), gen_id))
        if gen_id in self.errors:
            err = self.errors[gen_id]
            if callable(err):
                err = err()
            if err is not None:
                raise err
        if self.clock.now < self.available_at.get(gen_id, 0):
            raise OpenRouterHTTPError(404, "generation not found", error_type="not_found")
        return self.payloads.get(gen_id) or _gen_payload(gen_id)


def _backfill(tmp_path, clock, stub, **kw):
    return Backfill(Ledger.for_run(tmp_path), key="sk-test", run_id="run-1", clock=clock,
                    sleep=lambda s: clock.advance(s), fetch=stub, **kw)


def test_parse_generation_maps_the_documented_fields():
    fields = parse_generation(_gen_payload("gen-1"))
    assert fields["gen_id"] == "gen-1" and fields["cost_usd"] == PROBE_12_COST
    assert fields["provider_name"] == "Novita" and fields["provider"] == "novita"
    assert fields["model_served"] == "z-ai/glm-5.2-20260616"
    assert fields["tokens"] == {"input": 1250, "output": 340, "cache_read": None,
                                "cache_create": None, "reasoning": 120}
    assert fields["stop_reason"] == "tool_use" and fields["native_finish_reason"] == "tool_calls"
    assert fields["latency_ms"] == 20431 and fields["generation_time_ms"] == 19600
    assert fields["streamed"] is True and fields["is_byok"] is False


def test_parse_generation_uses_the_catalog_for_the_provider_slug():
    catalog = ModelCatalog.from_snapshot({"providers": [{"slug": "z-ai", "name": "Z.AI"}]})
    assert parse_generation(_gen_payload("gen-1", provider="Z.AI"), catalog=catalog)["provider"] == "z-ai"


def test_is_generation_id():
    assert is_generation_id("gen-01J") and not is_generation_id("msg_01") and not is_generation_id(None)


def test_backoff_first_poll_then_two_second_steps_until_available(tmp_path):
    clock = _Clock()
    stub = _Stub(clock, available_at={"gen-1": 1009.0})      # the verified ~9 s lag
    bf = _backfill(tmp_path, clock, stub)
    assert bf.sight("gen-1", case_id="c1", model_requested="z-ai/glm-5.2:exacto",
                    model_echo="z-ai/glm-5.2", message_index=3)
    assert bf.sight("gen-1") is False                           # deduplicated
    assert bf.sight("msg_anthropic") is False                   # never queried
    assert bf.poll_once() == 0 and stub.calls == []             # nothing due before 5 s
    clock.advance(5.0)
    assert bf.poll_once() == 0                                  # 404 → reschedule
    clock.advance(2.0)
    assert bf.poll_once() == 0
    clock.advance(2.0)                                          # t = 9 s: available
    assert bf.poll_once() == 1
    assert [t - 1000.0 for t, _ in stub.calls] == [5.0, 7.0, 9.0]
    rows = Ledger.for_run(tmp_path).read()
    assert len(rows) == 1 and rows[0]["status"] == "ok"
    assert rows[0]["cost_usd"] == PROBE_12_COST                # probe #12 reproduced
    assert rows[0]["provider"] == "novita" and rows[0]["model_served"] == "z-ai/glm-5.2-20260616"
    assert rows[0]["model"] == "z-ai/glm-5.2" and rows[0]["model_echo"] == "z-ai/glm-5.2"
    assert rows[0]["message_index"] == 3 and rows[0]["case_id"] == "c1"
    assert rows[0]["backfill_lag_s"] == 9.0
    assert bf.pending() == 0 and bf.stats.ok == 1


def test_give_up_after_sixty_seconds_keeps_the_id_and_retries_at_close(tmp_path):
    clock = _Clock()
    stub = _Stub(clock, available_at={"gen-1": 1080.0})      # lands only after 80 s
    bf = _backfill(tmp_path, clock, stub)
    bf.sight("gen-1", case_id="c1")
    while bf.pending():
        clock.advance(2.0)
        bf.poll_once()
    rows = Ledger.for_run(tmp_path).read()
    assert rows[-1]["status"] == "backfill_failed" and rows[-1]["gen_id"] == "gen-1"
    assert rows[-1]["cost_usd"] is None and rows[-1]["error_class"] == "config"  # 404 not_found
    assert bf.failed_ids() == ["gen-1"] and bf.stats.failed == 1
    assert clock.now - 1000.0 >= 60.0
    clock.advance(25.0)                                          # run end: now materialised
    stats = bf.close()
    assert stats.ok == 1
    assert Ledger.for_run(tmp_path).read(status="ok")[0]["gen_id"] == "gen-1"


def test_429_honours_retry_after_and_5xx_keeps_the_cadence(tmp_path):
    clock = _Clock()
    attempts = {"gen-1": 0, "gen-2": 0}

    def rate_limited():
        attempts["gen-1"] += 1
        if attempts["gen-1"] == 1:
            return OpenRouterHTTPError(429, "slow down", error_type="rate_limit_exceeded",
                                       retry_after=7)
        return None

    def flaky():
        attempts["gen-2"] += 1
        return OpenRouterHTTPError(503, "upstream") if attempts["gen-2"] == 1 else None

    stub = _Stub(clock, errors={"gen-1": rate_limited, "gen-2": flaky})
    bf = _backfill(tmp_path, clock, stub)
    bf.sight("gen-1"); bf.sight("gen-2")
    clock.advance(5.0); bf.poll_once()                            # both error once
    clock.advance(2.0); assert bf.poll_once() == 1               # gen-2 retried at +2 s, ok
    clock.advance(5.0); assert bf.poll_once() == 1               # gen-1 only after Retry-After 7 s
    assert [c for c in stub.calls if c[1] == "gen-1"] == [(1005.0, "gen-1"), (1012.0, "gen-1")]


def test_401_aborts_the_worker_and_fails_every_pending_id(tmp_path):
    clock = _Clock()
    stub = _Stub(clock, errors={"gen-1": OpenRouterHTTPError(401, "bad key", error_type="authentication")})
    bf = _backfill(tmp_path, clock, stub)
    bf.sight("gen-1"); bf.sight("gen-2"); bf.sight("gen-3")
    clock.advance(5.0)
    bf.poll_once()
    assert bf.stats.aborted and bf.pending() == 0
    rows = Ledger.for_run(tmp_path).read()
    assert len(rows) == 3 and {r["status"] for r in rows} == {"backfill_failed"}
    assert {r["error_class"] for r in rows} == {"config"}
    assert len([c for c in stub.calls]) == 1                      # no further GETs
    assert bf.sight("gen-4") is False and bf.retry_failed() == 0


def test_concurrency_is_bounded_per_poll(tmp_path):
    clock = _Clock()
    stub = _Stub(clock)
    bf = _backfill(tmp_path, clock, stub, max_workers=2)
    for i in range(5):
        bf.sight(f"gen-{i}")
    clock.advance(5.0)
    assert bf.poll_once() == 2 and bf.poll_once() == 2 and bf.poll_once() == 1


def test_drain_polls_until_empty(tmp_path):
    clock = _Clock()
    stub = _Stub(clock, available_at={"gen-1": 1011.0})
    bf = _backfill(tmp_path, clock, stub)
    bf.sight("gen-1")
    assert bf.drain(timeout_s=30) == 0
    assert Ledger.for_run(tmp_path).read()[0]["status"] == "ok"


def test_error_messages_never_carry_the_key(tmp_path):
    clock = _Clock()
    stub = _Stub(clock, errors={"gen-1": OpenRouterHTTPError(500, "boom sk-test leaked?")})
    bf = _backfill(tmp_path, clock, stub, give_up_s=1)
    bf.sight("gen-1"); clock.advance(5.0); bf.poll_once()
    row = Ledger.for_run(tmp_path).read()[0]
    assert row["status"] == "backfill_failed" and "sk-test" in row["error_message"]  # message text only
    assert "Authorization" not in str(row)


# --- key usage -------------------------------------------------------------------------

def test_read_key_usage_and_settled_delta():
    clock = _Clock()
    readings = iter([1.00, 1.05, 1.07, 1.07])

    def fetch(url):
        assert url.endswith("/v1/key")
        return {"data": {"usage": next(readings)}}

    assert read_key_usage(fetch) == 1.00
    delta = read_key_usage_settled(fetch, before_usd=1.00, settle_s=20, poll_s=5, max_s=60,
                                   clock=clock, sleep=lambda s: clock.advance(s))
    assert delta == KeyUsageDelta(before_usd=1.00, after_usd=1.07, settle_s=30.0)
    assert delta.delta_usd == pytest.approx(0.07)
    assert KeyUsageDelta(None, 1.0, 20).delta_usd is None


def test_settled_read_gives_up_at_max_and_keeps_the_last_value():
    clock = _Clock()
    counter = {"n": 0}

    def fetch(url):
        counter["n"] += 1
        return {"data": {"usage": 1.0 + counter["n"] * 0.01}}       # never stable

    delta = read_key_usage_settled(fetch, before_usd=1.0, settle_s=20, poll_s=10, max_s=60,
                                   clock=clock, sleep=lambda s: clock.advance(s))
    assert delta.after_usd is not None and delta.settle_s >= 60


# --- coverage math ---------------------------------------------------------------------

def test_coverage_uses_transcript_ids_as_the_denominator():
    rows = [{"source": "generation", "gen_id": "gen-1", "status": "ok", "provider_name": "Novita"},
            {"source": "generation", "gen_id": "gen-2", "status": "backfill_failed"},
            {"source": "generation", "gen_id": "gen-x", "status": "ok", "provider_name": "N"},  # not in transcript
            {"source": "judge", "gen_id": "gen-3", "status": "ok"}]
    cov = coverage(["gen-1", "gen-2", "gen-3", "gen-4", "msg_5"], rows)
    assert cov == {"requests": 4, "requests_priced": 1, "requests_missing_cost": 1,
                   "requests_unattributed": 0, "requests_pending": 2, "coverage": 0.25}
    assert coverage([], rows)["coverage"] is None
