"""Preflight (spec 014) — the checks that run before any spend, PR-5 minimum.

The agent path carries no routing body (Decision 1), so this is the only place
the declared pins are checked before the first request: every agent-path slug
exists in the public catalog, each pinned provider serves it, the inference key
is valid (``GET /key``) and the account can use the model (``GET /models/user``,
the paid-training filter). A catalog *fetch* failure degrades the run to
``warn`` with the reason; key validity, slug existence and eligibility stay
strict under ``strict``. The frozen catalog view is written to
``<run_dir>/provider/routing_snapshot.json`` for the post-hoc audit.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent_eval.providers.base import ConfigError, parse_agent_model, routing_key
from agent_eval.providers.ledger import utc_now
from agent_eval.providers.openrouter.audit import pinned_providers
from agent_eval.providers.openrouter.catalog import ModelCatalog
from agent_eval.providers.openrouter.http import OpenRouterHTTPError, get_json
from agent_eval.providers.openrouter.routing import routing_sha

SNAPSHOT_RELPATH = Path("provider") / "routing_snapshot.json"
LEVELS = ("strict", "warn", "off")


@dataclass
class PreflightResult:
    level: str
    failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    degraded_reason: Optional[str] = None
    snapshot: Optional[dict] = None
    snapshot_path: Optional[Path] = None


def table_sha(table) -> Optional[str]:
    """SHA of the declared routing table (defaults + models); None when empty."""
    if table is None or getattr(table, "is_empty", True):
        return None
    body = {"defaults": table.defaults.to_dict(),
            "models": {k: v.to_dict() for k, v in sorted(table.models.items())}}
    return routing_sha(body)


def plan_models(plan) -> dict:
    """``{routing key: variant}`` for every id the agent path can send."""
    ids = [plan.skill, plan.subagent] + ([plan.hook] if plan.hook is not None else [])
    if plan.background_model:
        ids.append(parse_agent_model(plan.background_model))
    out: dict = {}
    for m in ids:
        out.setdefault(m.key, ":".join(m.variants) if m.variants else None)
    return out


def run_preflight(plan, *, level: str = "strict", run_dir=None, catalog=None,
                  fetch=None) -> PreflightResult:
    """Run the checks for ``plan`` at ``level``. ``strict`` raises
    :class:`ConfigError` listing every failure; ``warn`` prints them and marks
    the result degraded; ``off`` does nothing. ``fetch`` (tests) serves the
    public catalog GETs; the key-authenticated GETs go through it too."""
    if level not in LEVELS:
        raise ConfigError(f"preflight level must be one of {LEVELS}; got {level!r}")
    res = PreflightResult(level=level)
    if level == "off":
        return res
    base = plan.base_url.rstrip("/")
    keyed = fetch or (lambda url: get_json(url, key=plan.key, timeout=15))
    catalog = catalog or (ModelCatalog(fetch=fetch, base_url=plan.base_url) if fetch
                          else ModelCatalog(base_url=plan.base_url))
    table = getattr(plan, "routing", None)
    keys = plan_models(plan)
    snapshot_catalog: dict = {"providers": [], "models": [], "endpoints": {}}
    known: Optional[set] = None
    try:
        models = catalog.models()
        known = set()
        for m in models:
            if isinstance(m, dict):
                for f in ("id", "canonical_slug"):
                    if m.get(f):
                        known.add(routing_key(m[f]))
        snapshot_catalog["providers"] = catalog.providers()
        snapshot_catalog["models"] = [m for m in models if isinstance(m, dict) and
                                      {routing_key(m.get("id")), routing_key(m.get("canonical_slug"))} & set(keys)]
    except OpenRouterHTTPError as exc:
        res.warnings.append(f"catalog unavailable ({exc}); slug and pin checks skipped")
        res.degraded_reason = "catalog"
    snap_keys: dict = {}
    for key, variant in keys.items():
        spec = table.for_model(key) if table is not None else None
        pins = pinned_providers(spec)
        entry = {"variant": variant, "pinned_set": sorted(pins) if pins else [],
                 "catalog": {"providers": [], "endpoints": []}}
        snap_keys[key] = entry
        if known is None:
            continue
        if key not in known:
            res.failures.append(f"model {key}: not in the OpenRouter catalog")
            continue
        try:
            eps = catalog.endpoints(key)
        except OpenRouterHTTPError as exc:
            res.warnings.append(f"model {key}: endpoints unavailable ({exc}); pin check skipped")
            res.degraded_reason = res.degraded_reason or "catalog"
            continue
        snapshot_catalog["endpoints"][key] = eps
        served: dict = {}
        for e in eps:
            if isinstance(e, dict):
                served.setdefault(catalog.provider_slug(e.get("provider_name")), []).append(e)
        entry["catalog"] = {"providers": sorted(s for s in served if s),
                            "endpoints": [e.get("tag") or e.get("provider_name") for e in eps if isinstance(e, dict)]}
        if not pins:
            continue
        missing = sorted(p for p in pins if p not in served)
        if missing:
            res.failures.append(f"model {key}: pinned provider(s) {', '.join(missing)} do not serve it "
                                f"(served by: {', '.join(entry['catalog']['providers']) or 'nobody'})")
        quants = getattr(spec, "quantizations", None)
        if quants and not missing and not any(e.get("quantization") in quants
                                              for p in pins for e in served.get(p, [])):
            res.failures.append(f"model {key}: no pinned provider serves it at quantization {list(quants)}")
    try:
        keyed(f"{base}/v1/key")
    except OpenRouterHTTPError as exc:
        if exc.status in (401, 403):
            res.failures.append(f"inference key ({plan.key_env}) rejected: HTTP {exc.status}")
        else:
            res.warnings.append(f"key check unavailable ({exc})")
            res.degraded_reason = res.degraded_reason or "key-check"
    try:
        eligible = keyed(f"{base}/v1/models/user")
        data = eligible.get("data") if isinstance(eligible, dict) else eligible
        elig = {routing_key(m.get("id")) for m in (data or []) if isinstance(m, dict) and m.get("id")}
        for key in keys:
            if elig and key not in elig:
                reasons = [m.get("ineligibility_reasons") for m in data if isinstance(m, dict)
                           and routing_key(m.get("id")) == key and m.get("ineligibility_reasons")]
                why = f": {reasons[0]}" if reasons else " (account privacy/paid-training settings)"
                res.failures.append(f"model {key}: not available to this key{why}")
    except OpenRouterHTTPError as exc:
        res.warnings.append(f"eligibility check unavailable ({exc})")
        res.degraded_reason = res.degraded_reason or "eligibility"
    res.snapshot = {"ts": utc_now(), "routing_sha": table_sha(table), "enforcement": plan.enforcement,
                    "preflight": level, "keys": snap_keys, "catalog": snapshot_catalog}
    if run_dir is not None:
        res.snapshot_path = Path(run_dir) / SNAPSHOT_RELPATH
        res.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        res.snapshot_path.write_text(json.dumps(res.snapshot, indent=2) + "\n")
    if res.failures:
        if level == "strict":
            raise ConfigError("preflight failed:\n  - " + "\n  - ".join(res.failures))
        res.degraded_reason = "preflight"
        for f in res.failures:
            print(f"WARNING: preflight: {f}", file=sys.stderr)
    return res
