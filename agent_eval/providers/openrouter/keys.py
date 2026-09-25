"""Management-API client for ``enforcement: key-guardrail`` (spec 014).

A per-run inference key is provisioned before any spend with a real-dollar
``limit`` and a provider allow-list, used by the agent (and by the backfill /
key-usage reads, so the delta is exact by construction), and revoked in the
host's ``finally`` on every exit path. **The guardrail field semantics are
DOCUMENTED / UNVERIFIED (probe #26):** :func:`key_request` is the one place
the field names live, and :func:`guardrail_mismatches` fails closed — a
server that does not echo the limit and the allow-list back cannot be relied
on to enforce them, so the key is revoked and the run never starts.

The management key is read from the environment at plan build and at revoke
time only; it is never placed in any env target, the ledger, the snapshot or
``key.json`` (which records the hash, never the key).

``python3 -m agent_eval.providers.openrouter.keys revoke <run_dir>`` retries a
revocation that failed at run end.
"""

from __future__ import annotations

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv before 3p imports
import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from agent_eval.providers.base import ConfigError
from agent_eval.providers.ledger import utc_now
from agent_eval.providers.openrouter.http import BASE_URL, OpenRouterHTTPError, delete, get_json, post_json
from agent_eval.providers.openrouter.routing import normalize_provider

KEY_RECORD_RELPATH = Path("provider") / "key.json"


@dataclass
class ProvisionedKey:
    """A per-run key. ``key`` is the secret; everything else is recordable."""

    key: str
    hash: str
    name: str
    limit_usd: float
    allowed_providers: tuple
    base_url: str = BASE_URL
    created_at: Optional[str] = None
    revoked_at: Optional[str] = None
    revoke_error: Optional[str] = None

    def __repr__(self) -> str:
        return (f"ProvisionedKey(hash={self.hash!r}, name={self.name!r}, "
                f"limit_usd={self.limit_usd!r}, revoked={bool(self.revoked_at)})")

    def record(self) -> dict:
        """What ``key.json`` holds — never the key."""
        return {"hash": self.hash, "name": self.name, "limit_usd": self.limit_usd,
                "allowed_providers": list(self.allowed_providers), "base_url": self.base_url,
                "created_at": self.created_at, "revoked_at": self.revoked_at,
                "revoke_error": self.revoke_error}


def key_request(*, name: str, limit_usd: float, allowed_providers: Iterable[str]) -> dict:
    """The ``POST /api/v1/keys`` body. The **only** place the guardrail field
    names live (probe #26): ``limit`` is the key's spend cap in USD,
    ``allowed_providers`` the provider slugs it may be routed to."""
    return {"name": name, "limit": float(limit_usd),
            "allowed_providers": sorted({normalize_provider(p) for p in allowed_providers if p})}


def _data(payload) -> dict:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def provision(management_key: str, *, name: str, limit_usd: float, allowed_providers: Iterable[str],
              base_url: str = BASE_URL) -> ProvisionedKey:
    """Create the per-run key. The returned object holds the secret once; the
    caller writes only its ``record()``."""
    body = key_request(name=name, limit_usd=limit_usd, allowed_providers=allowed_providers)
    base = base_url.rstrip("/")
    try:
        payload = post_json(f"{base}/v1/keys", body, key=management_key)
    except OpenRouterHTTPError as exc:
        raise ConfigError(f"management API refused to create the per-run key: {exc}") from None
    data = _data(payload)
    key = payload.get("key") if isinstance(payload, dict) else None
    key = key or data.get("key")
    hash_ = data.get("hash") or (payload.get("hash") if isinstance(payload, dict) else None)
    if not isinstance(key, str) or not key or not isinstance(hash_, str) or not hash_:
        raise ConfigError("management API returned no key/hash for the per-run key")
    return ProvisionedKey(key=key, hash=hash_, name=str(data.get("name") or name),
                          limit_usd=float(limit_usd), allowed_providers=tuple(body["allowed_providers"]),
                          base_url=base_url, created_at=str(data.get("created_at") or utc_now()))


def verify_guardrail(management_key: str, key_hash: str, *, base_url: str = BASE_URL) -> dict:
    """``GET /api/v1/keys/{hash}``: what the server actually holds for the key."""
    try:
        return _data(get_json(f"{base_url.rstrip('/')}/v1/keys/{key_hash}", key=management_key))
    except OpenRouterHTTPError as exc:
        raise ConfigError(f"management API: the per-run key could not be read back: {exc}") from None


def guardrail_mismatches(read_back: dict, requested: ProvisionedKey) -> list:
    """Differences between what was requested and what the server echoes —
    any difference means the guardrail is not what the run assumes."""
    problems = []
    limit = read_back.get("limit")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or abs(limit - requested.limit_usd) > 1e-9:
        problems.append(f"limit: requested {requested.limit_usd}, server holds {limit!r}")
    allowed = read_back.get("allowed_providers")
    if not isinstance(allowed, list):
        problems.append("the server did not echo an allowed-provider list (the allow-list would not be enforced)")
    elif sorted(normalize_provider(str(p)) for p in allowed) != list(requested.allowed_providers):
        problems.append(f"allowed_providers: requested {list(requested.allowed_providers)}, "
                        f"server holds {sorted(str(p) for p in allowed)}")
    return problems


def revoke(management_key: str, key_hash: str, *, base_url: str = BASE_URL) -> None:
    """``DELETE /api/v1/keys/{hash}``."""
    delete(f"{base_url.rstrip('/')}/v1/keys/{key_hash}", key=management_key)


def write_key_record(run_dir, provisioned: ProvisionedKey) -> Path:
    path = Path(run_dir) / KEY_RECORD_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(provisioned.record(), f, indent=2)
        f.write("\n")
    return path


def read_key_record(run_dir) -> Optional[dict]:
    try:
        payload = json.loads((Path(run_dir) / KEY_RECORD_RELPATH).read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def revoke_plan_key(plan, *, run_dir=None) -> bool:
    """Revoke the plan's per-run key once (idempotent) and record the outcome
    in ``key.json`` when the run dir is known. A failure is a stderr ERROR
    naming the hash (the key's ``limit`` bounds the blast radius meanwhile).
    Returns True when the key is revoked (now or earlier)."""
    pk = getattr(plan, "provisioned", None)
    if pk is None:
        return False
    if pk.revoked_at:
        return True
    env_name = getattr(plan, "management_key_env", None) or "OPENROUTER_MANAGEMENT_KEY"
    management_key = os.environ.get(env_name)
    try:
        if not management_key:
            raise ConfigError(f"{env_name} is not set in this process")
        revoke(management_key, pk.hash, base_url=pk.base_url)
        pk.revoked_at, pk.revoke_error = utc_now(), None
        ok = True
    except (OpenRouterHTTPError, ConfigError) as exc:
        pk.revoke_error = str(exc)[:200]
        ok = False
        print(f"ERROR: per-run key {pk.hash} could not be revoked ({exc}); retry with "
              f"`python3 -m agent_eval.providers.openrouter.keys revoke {run_dir or '<run_dir>'}`",
              file=sys.stderr)
    if run_dir is not None:
        try:
            write_key_record(run_dir, pk)
        except OSError as exc:
            print(f"WARNING: key.json not updated: {exc}", file=sys.stderr)
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rv = sub.add_parser("revoke", help="revoke the per-run key recorded in <run_dir>/provider/key.json")
    rv.add_argument("run_dir", type=Path)
    rv.add_argument("--management-key-env", default="OPENROUTER_MANAGEMENT_KEY")
    args = parser.parse_args(argv)
    record = read_key_record(args.run_dir)
    if record is None:
        print(f"ERROR: no {KEY_RECORD_RELPATH} under {args.run_dir}", file=sys.stderr)
        return 2
    if record.get("revoked_at"):
        print(f"per-run key {record.get('hash')} already revoked at {record['revoked_at']}")
        return 0
    management_key = os.environ.get(args.management_key_env)
    if not management_key:
        print(f"ERROR: set {args.management_key_env}", file=sys.stderr)
        return 2
    try:
        revoke(management_key, str(record.get("hash")), base_url=record.get("base_url") or BASE_URL)
    except OpenRouterHTTPError as exc:
        record["revoke_error"] = str(exc)[:200]
        (args.run_dir / KEY_RECORD_RELPATH).write_text(json.dumps(record, indent=2) + "\n")
        print(f"ERROR: per-run key {record.get('hash')} still not revoked: {exc}", file=sys.stderr)
        return 1
    record["revoked_at"], record["revoke_error"] = utc_now(), None
    (args.run_dir / KEY_RECORD_RELPATH).write_text(json.dumps(record, indent=2) + "\n")
    print(f"per-run key {record.get('hash')} revoked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
