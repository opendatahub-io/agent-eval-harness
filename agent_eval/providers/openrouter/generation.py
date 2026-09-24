"""The cost and attribution source on the agent path: ``GET /api/v1/generation``
backfill and the ``/api/v1/key`` usage delta (spec 014).

The harness is not on the request path. It learns of every billed generation
from the ``gen-…`` ids in Claude Code's stream-json and asks OpenRouter for the
generation record — ``total_cost``, ``provider_name``, the dated permaslug,
native token counts, finish reasons, latency. ``/generation`` materialises
7.6–12.7 s after the stream ends (probe #6), hence the backoff: first poll 5 s
after sighting, then every 2 s on 404, giving up at 60 s → ``backfill_failed``
(the id is kept so an offline pass can complete it). Everything written is a
ledger row; ``run_result.json`` is reconcile's business.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from agent_eval.providers.base import routing_key
from agent_eval.providers.ledger import Ledger, make_record
from agent_eval.providers.openrouter.errors import classify, describe
from agent_eval.providers.openrouter.http import BASE_URL, OpenRouterHTTPError, get_json

FIRST_POLL_S = 5.0
POLL_S = 2.0
GIVE_UP_S = 60.0
KEY_USAGE_SETTLE_S = 20.0
KEY_USAGE_MAX_S = 60.0
KEY_USAGE_POLL_S = 5.0


def is_generation_id(value) -> bool:
    return isinstance(value, str) and value.startswith("gen-")


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def parse_generation(payload: dict, *, catalog=None) -> dict:
    """Ledger fields from a ``/generation`` answer (``{"data": {...}}`` or the
    inner object)."""
    data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    data = data or {}
    provider_name = data.get("provider_name")
    provider = catalog.provider_slug(provider_name) if (catalog and provider_name) else None
    if provider is None and provider_name:
        from agent_eval.providers.openrouter.routing import normalize_provider

        provider = normalize_provider(provider_name)
    tokens = {
        "input": _num(data.get("native_tokens_prompt")) if data.get("native_tokens_prompt") is not None
        else _num(data.get("tokens_prompt")),
        "output": _num(data.get("native_tokens_completion")) if data.get("native_tokens_completion") is not None
        else _num(data.get("tokens_completion")),
        "cache_read": _num(data.get("native_tokens_cached")),
        "cache_create": None,
        "reasoning": _num(data.get("native_tokens_reasoning")),
    }
    latency = _num(data.get("latency"))
    gen_time = _num(data.get("generation_time"))
    return {
        "gen_id": data.get("id"),
        "model_served": data.get("model"),
        "provider": provider,
        "provider_name": provider_name,
        "cost_usd": _num(data.get("total_cost")),
        "cost_details": {"upstream_inference_cost": _num(data.get("upstream_inference_cost")),
                         "cache_discount": _num(data.get("cache_discount"))},
        "is_byok": bool(data.get("is_byok")) if data.get("is_byok") is not None else None,
        "tokens": tokens,
        "streamed": data.get("streamed"),
        "latency_ms": latency,
        "generation_time_ms": gen_time,
        "stop_reason": data.get("finish_reason"),
        "native_finish_reason": data.get("native_finish_reason"),
    }


@dataclass
class Sighting:
    """One generation id as the transcript exposed it."""

    gen_id: str
    case_id: Optional[str] = None
    step_id: Optional[str] = None
    role: str = "agent"
    message_index: Optional[int] = None
    model_requested: Optional[str] = None
    model_echo: Optional[str] = None
    routing_sha: Optional[str] = None
    sighted_at: float = 0.0
    next_poll_at: float = 0.0
    attempts: int = 0


@dataclass
class BackfillStats:
    sighted: int = 0
    ok: int = 0
    failed: int = 0
    pending: int = 0
    aborted: Optional[str] = None
    errors: list = field(default_factory=list)


class Backfill:
    """The process-wide ``/generation`` worker.

    ``sight(...)`` enqueues an id; a daemon thread polls due ids with the
    documented cadence and writes one ledger row per id. ``poll_once(now)`` is
    the synchronous step the thread runs (tests drive it with a fake clock).
    ``retry_failed()`` re-queues every ``backfill_failed`` id (run end / offline)
    and ``close()`` drains, retries once and stops the thread.
    """

    def __init__(self, ledger: Ledger, *, key: Optional[str], run_id: Optional[str] = None,
                 catalog=None, fetch: Optional[Callable] = None, base_url: str = BASE_URL,
                 first_poll_s: float = FIRST_POLL_S, poll_s: float = POLL_S,
                 give_up_s: float = GIVE_UP_S, max_workers: int = 4,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, provider_kind: str = "openrouter"):
        self.ledger = ledger
        self.key = key
        self.run_id = run_id
        self.catalog = catalog
        self._fetch = fetch or (lambda url: get_json(url, key=self.key, timeout=15))
        self._base = base_url.rstrip("/")
        self.first_poll_s = first_poll_s
        self.poll_s = poll_s
        self.give_up_s = give_up_s
        self.max_workers = max(1, int(max_workers))
        self._clock = clock
        self._sleep = sleep
        self.provider_kind = provider_kind
        self._pending: dict = {}
        self._failed: dict = {}
        self._seen: set = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.stats = BackfillStats()

    # -- feeding ------------------------------------------------------------------

    def sight(self, gen_id: str, *, case_id=None, step_id=None, role: str = "agent",
              message_index=None, model_requested=None, model_echo=None,
              routing_sha=None) -> bool:
        """Enqueue an id the first time it is seen. Non-``gen-`` ids are ignored
        (an Anthropic-direct or Vertex id has no OpenRouter generation)."""
        if not is_generation_id(gen_id) or self.stats.aborted:
            return False
        with self._lock:
            if gen_id in self._seen:
                return False
            self._seen.add(gen_id)
            now = self._clock()
            self._pending[gen_id] = Sighting(
                gen_id=gen_id, case_id=case_id, step_id=step_id, role=role,
                message_index=message_index, model_requested=model_requested,
                model_echo=model_echo, routing_sha=routing_sha,
                sighted_at=now, next_poll_at=now + self.first_poll_s)
            self.stats.sighted += 1
            self.stats.pending = len(self._pending)
        return True

    def sight_many(self, sightings: Iterable[dict]) -> int:
        return sum(1 for s in sightings if self.sight(**s))

    # -- polling ------------------------------------------------------------------

    def _due(self, now: float) -> list:
        with self._lock:
            due = [s for s in self._pending.values() if s.next_poll_at <= now]
        due.sort(key=lambda s: s.next_poll_at)
        return due[: self.max_workers]

    def poll_once(self, now: Optional[float] = None) -> int:
        """Poll every due id once. Returns how many rows were written."""
        if self.stats.aborted:
            return 0
        now = self._clock() if now is None else now
        written = 0
        for sighting in self._due(now):
            if self.stats.aborted:
                break
            with self._lock:
                still_pending = sighting.gen_id in self._pending
            if not still_pending:
                continue
            written += self._poll_one(sighting, now)
        return written

    def _poll_one(self, s: Sighting, now: float) -> int:
        s.attempts += 1
        try:
            payload = self._fetch(f"{self._base}/v1/generation?id={s.gen_id}")
        except OpenRouterHTTPError as exc:
            return self._handle_error(s, exc, now)
        except Exception as exc:  # transport surprises never kill the worker
            return self._handle_error(s, OpenRouterHTTPError(None, str(exc)[:200]), now)
        fields = parse_generation(payload, catalog=self.catalog)
        fields.pop("gen_id", None)          # the sighting's id is authoritative
        self._write(s, status="ok", lag=now - s.sighted_at, **fields)
        self._forget(s)
        self.stats.ok += 1
        return 1

    def _handle_error(self, s: Sighting, exc: OpenRouterHTTPError, now: float) -> int:
        if exc.status in (401, 403):
            self.stats.aborted = f"{exc.status}: {exc.message}"
            self._write(s, status="backfill_failed", lag=now - s.sighted_at, error=exc)
            self._forget(s)
            self.stats.failed += 1
            # Abort: every remaining id is failed without another GET.
            with self._lock:
                remaining = list(self._pending.values())
            for other in remaining:
                self._write(other, status="backfill_failed", lag=now - other.sighted_at, error=exc)
                self._forget(other)
                self.stats.failed += 1
            return 1
        if now - s.sighted_at >= self.give_up_s:
            self._write(s, status="backfill_failed", lag=now - s.sighted_at, error=exc)
            self._forget(s)
            self.stats.failed += 1
            return 1
        delay = self.poll_s
        if exc.status == 429 and exc.retry_after:
            delay = max(delay, float(exc.retry_after))
        s.next_poll_at = now + delay
        return 0

    def _forget(self, s: Sighting):
        with self._lock:
            self._pending.pop(s.gen_id, None)
            self.stats.pending = len(self._pending)

    def _write(self, s: Sighting, *, status: str, lag: float, error=None, **fields):
        record = make_record(
            role=s.role, source="generation", status=status, provider_kind=self.provider_kind,
            run_id=self.run_id, case_id=s.case_id, step_id=s.step_id, gen_id=s.gen_id,
            message_index=s.message_index, model_requested=s.model_requested,
            model=routing_key(s.model_requested or s.model_echo or ""),
            model_echo=s.model_echo, routing_sha=s.routing_sha,
            backfill_lag_s=round(lag, 1), **fields)
        if error is not None:
            record["error_type"] = getattr(error, "error_type", None) or (
                "not_found" if getattr(error, "status", None) == 404 else None)
            record["error_class"] = classify(record["error_type"], getattr(error, "status", None),
                                             getattr(error, "message", "")).value
            record["error_message"] = describe(getattr(error, "status", None),
                                               getattr(error, "message", str(error)))
            if status == "backfill_failed":
                self._failed[s.gen_id] = s
                self.stats.errors.append(record["error_message"])
        self.ledger.append(record)

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> "Backfill":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="openrouter-backfill",
                                            daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            self._sleep(0.5)

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def failed_ids(self) -> list:
        return sorted(self._failed)

    def retry_failed(self) -> int:
        """Re-queue every ``backfill_failed`` id for one more pass (run end,
        or the offline backfill command). Returns how many were re-queued."""
        if self.stats.aborted:
            return 0
        with self._lock:
            failed = list(self._failed.values())
            self._failed.clear()
            now = self._clock()
            for s in failed:
                s.sighted_at = now - self.give_up_s + self.poll_s * 3   # a short second window
                s.next_poll_at = now
                self._pending[s.gen_id] = s
            self.stats.pending = len(self._pending)
        return len(failed)

    def drain(self, timeout_s: float = GIVE_UP_S + 5) -> int:
        """Poll synchronously until nothing is pending or ``timeout_s`` passes."""
        deadline = self._clock() + timeout_s
        while self.pending() and self._clock() < deadline and not self.stats.aborted:
            if not self.poll_once():
                self._sleep(min(self.poll_s, 0.5))
        return self.pending()

    def close(self, *, retry: bool = True) -> BackfillStats:
        """Stop the thread, drain what is due, retry failed ids once, drain again."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self.drain()
        if retry and self.retry_failed():
            self.drain()
        return self.stats


# --- key usage ----------------------------------------------------------------------

@dataclass(frozen=True)
class KeyUsageDelta:
    before_usd: Optional[float]
    after_usd: Optional[float]
    settle_s: float

    @property
    def delta_usd(self) -> Optional[float]:
        if self.before_usd is None or self.after_usd is None:
            return None
        return round(self.after_usd - self.before_usd, 6)


def read_key_usage(fetch: Optional[Callable] = None, *, key: Optional[str] = None,
                   base_url: str = BASE_URL) -> Optional[float]:
    """``GET /api/v1/key`` → the key's cumulative ``usage`` in USD."""
    fetch = fetch or (lambda url: get_json(url, key=key, timeout=15))
    payload = fetch(f"{base_url.rstrip('/')}/v1/key")
    data = payload.get("data") if isinstance(payload, dict) else None
    usage = (data or {}).get("usage") if isinstance(data, dict) else None
    return _num(usage)


def read_key_usage_settled(fetch: Optional[Callable] = None, *, key: Optional[str] = None,
                           before_usd: Optional[float], settle_s: float = KEY_USAGE_SETTLE_S,
                           max_s: float = KEY_USAGE_MAX_S, poll_s: float = KEY_USAGE_POLL_S,
                           clock: Callable[[], float] = time.monotonic,
                           sleep: Callable[[float], None] = time.sleep,
                           base_url: str = BASE_URL) -> KeyUsageDelta:
    """The run-end key-usage read: wait ``settle_s`` (the counter updates about
    20 s after the last request), then poll until two consecutive reads agree
    or ``max_s`` passes."""
    start = clock()
    sleep(settle_s)
    last = None
    stable = None
    while True:
        try:
            current = read_key_usage(fetch, key=key, base_url=base_url)
        except OpenRouterHTTPError:
            current = None
        if current is not None and current == last:
            stable = current
            break
        last = current
        if clock() - start >= max_s:
            stable = current if current is not None else last
            break
        sleep(poll_s)
    return KeyUsageDelta(before_usd=before_usd, after_usd=stable, settle_s=round(clock() - start, 1))


def coverage(message_ids: Iterable[str], rows: Iterable[dict]) -> dict:
    """Coverage math over the transcript ids: the denominator is what the
    transcript says happened, not what the ledger holds."""
    ids = {i for i in (message_ids or []) if is_generation_id(i)}
    by_id = {}
    for r in rows:
        if r.get("source") != "generation" or not r.get("gen_id"):
            continue
        by_id[r["gen_id"]] = r
    priced = sum(1 for i in ids if by_id.get(i, {}).get("status") == "ok")
    missing = sum(1 for i in ids if by_id.get(i, {}).get("status") == "backfill_failed")
    unattributed = sum(1 for i in ids if by_id.get(i, {}).get("status") == "ok"
                       and not by_id[i].get("provider_name"))
    pending = len(ids) - priced - missing
    return {
        "requests": len(ids),
        "requests_priced": priced,
        "requests_missing_cost": missing,
        "requests_unattributed": unattributed,
        "requests_pending": pending,
        "coverage": round(priced / len(ids), 4) if ids else None,
    }
