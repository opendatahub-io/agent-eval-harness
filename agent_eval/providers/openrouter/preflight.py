"""Preflight (spec 014): the checks that run before any spend.

The agent path carries no routing body (Decision 1), so this is the only place
the declared pins are checked before the first request. Per routing key used
by an agent role (skill, subagent, hook, background model) or by an
``openrouter:/`` judge: the slug exists in the public catalog; each pinned
provider serves it, at a declared quantization when ``quantizations`` is set,
with tools/``tool_choice: auto`` for agents (Claude Code sends ``auto``) and
``tool_choice: function`` for judges that carry pins (the forced tool call
404s otherwise — probe #5, Decision 25); endpoints with ``status < 0`` are
degraded and excluded; ``max_completion_tokens < 32000`` warns. The key is
valid (``GET /key``) and the account can use the model (``GET /models/user``,
the paid-training filter). A catalog *fetch* failure degrades the run to
``warn`` with the reason; key validity, slug existence and eligibility stay
strict under ``strict``. The frozen catalog view is written to
``<run_dir>/provider/routing_snapshot.json`` for the post-hoc audit.

``python3 -m agent_eval.providers.openrouter.preflight --config eval.yaml``
runs the same checks from the command line (no spend, no per-run key).
"""

from __future__ import annotations

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv before 3p imports
import argparse
import json
import os
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
CLAUDE_CODE_MAX_TOKENS = 32000


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


def _tag(slug, e) -> str:
    return e.get("tag") or f"{slug}/{e.get('quantization')}" if e.get("quantization") else (e.get("tag") or slug)


def _check_endpoint(key, slug, e, roles, quants, res) -> list:
    """Why a pinned endpoint is excluded from the eligible set (empty = eligible)."""
    reasons = []
    q = e.get("quantization")
    if quants and q not in quants:
        reasons.append(f"quantization {q} not in {list(quants)}")
    status = e.get("status")
    if isinstance(status, (int, float)) and not isinstance(status, bool) and status < 0:
        reasons.append(f"status {status} (degraded)")
    params = e.get("supported_parameters")
    if "agent" in roles and isinstance(params, list) and not {"tools", "tool_choice"} <= set(params):
        reasons.append("no tools/tool_choice support")
    stc = e.get("supports_tool_choice")
    if "agent" in roles and isinstance(stc, dict) and stc.get("auto") is False:
        reasons.append("tool_choice: auto unsupported (Claude Code sends auto)")
    mct = e.get("max_completion_tokens")
    if isinstance(mct, (int, float)) and not isinstance(mct, bool) and mct < CLAUDE_CODE_MAX_TOKENS:
        res.warnings.append(f"model {key}: endpoint {_tag(slug, e)} max_completion_tokens {int(mct)} "
                            f"< {CLAUDE_CODE_MAX_TOKENS} (Claude Code requests 32k)")
    return reasons


def run_preflight(plan, *, level: str = "strict", run_dir=None, catalog=None, fetch=None,
                  judges: Optional[dict] = None) -> PreflightResult:
    """Run the checks for ``plan`` at ``level``; ``judges`` is
    ``{routing key: pinned}`` for the run's ``openrouter:/`` judges (see
    ``plan.judge_routing_keys``). ``strict`` raises :class:`ConfigError` listing
    every failure; ``warn`` prints them and marks the result degraded; ``off``
    does nothing. ``fetch`` (tests) serves every GET."""
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
    keys: dict = {k: {"variant": v, "roles": {"agent"}, "judge_pinned": False}
                  for k, v in plan_models(plan).items()}
    for key, pinned in (judges or {}).items():
        entry = keys.setdefault(routing_key(key), {"variant": None, "roles": set(), "judge_pinned": False})
        entry["roles"].add("judge")
        entry["judge_pinned"] = entry["judge_pinned"] or bool(pinned)
    snapshot_catalog: dict = {"providers": [], "models": [], "endpoints": {}}
    known: Optional[dict] = None
    try:
        models = catalog.models()
        known = {}
        for m in models:
            if isinstance(m, dict):
                for f in ("id", "canonical_slug"):
                    if m.get(f):
                        known[routing_key(m[f])] = m
        snapshot_catalog["providers"] = catalog.providers()
        snapshot_catalog["models"] = [m for m in models if isinstance(m, dict) and
                                      {routing_key(m.get("id")), routing_key(m.get("canonical_slug"))} & set(keys)]
    except OpenRouterHTTPError as exc:
        res.warnings.append(f"catalog unavailable ({exc}); slug and pin checks skipped")
        res.degraded_reason = "catalog"
    snap_keys: dict = {}
    pricing: dict = {}
    for key, info in keys.items():
        roles = info["roles"]
        spec = table.for_model(key) if table is not None else None
        pins = pinned_providers(spec)
        quants = getattr(spec, "quantizations", None) if spec is not None else None
        entry = {"variant": info["variant"], "roles": sorted(roles), "pinned_set": [],
                 "eligible": [], "excluded": [], "catalog": {"providers": [], "endpoints": []}}
        snap_keys[key] = entry
        if known is None:
            continue
        if key not in known:
            res.failures.append(f"model {key}: not in the OpenRouter catalog")
            continue
        permaslug = known[key].get("canonical_slug") or known[key].get("id")
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
                            "endpoints": [_tag(s, e) for s, lst in served.items() for e in lst]}
        pricing[key] = {s: [e.get("pricing") for e in lst if e.get("pricing")] for s, lst in served.items()
                        if any(e.get("pricing") for e in lst)}
        if not pins:
            continue
        missing = sorted(p for p in pins if p not in served)
        if missing:
            res.failures.append(f"model {key}: pinned provider(s) {', '.join(missing)} do not serve it "
                                f"(served by: {', '.join(entry['catalog']['providers']) or 'nobody'})")
            continue
        function_capable, function_reported = [], False
        for slug in sorted(pins):
            for e in served.get(slug, []):
                reasons = _check_endpoint(key, slug, e, roles, quants, res)
                tag = _tag(slug, e)
                if reasons:
                    entry["excluded"].append({"tag": tag, "reasons": reasons})
                    continue
                entry["eligible"].append(tag)
                entry["pinned_set"].append([slug, permaslug, e.get("quantization")])
                stc = e.get("supports_tool_choice")
                if isinstance(stc, dict) and "function" in stc:
                    function_reported = True
                    if stc.get("function"):
                        function_capable.append(tag)
        if not entry["eligible"]:
            detail = "; ".join(f"{x['tag']}: {', '.join(x['reasons'])}" for x in entry["excluded"])
            res.failures.append(f"model {key}: no eligible pinned endpoint ({detail})")
            continue
        if entry["excluded"]:
            res.warnings.append(f"model {key}: excluded pinned endpoint(s) " + "; ".join(
                f"{x['tag']} ({', '.join(x['reasons'])})" for x in entry["excluded"]))
        if "judge" in roles and info["judge_pinned"] and function_reported:
            lacking = [t for t in entry["eligible"] if t not in function_capable]
            if not function_capable:
                res.failures.append(f"model {key}: no pinned endpoint supports tool_choice: function — "
                                    "a pinned judge's forced tool call answers 404 (Decision 25)")
            elif lacking:
                res.warnings.append(f"model {key}: pinned endpoint(s) {', '.join(lacking)} lack "
                                    "tool_choice: function; the judge falls back per the tool_choice ladder")
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
    providers_map = sorted(p.get("slug") for p in snapshot_catalog["providers"] if isinstance(p, dict) and p.get("slug"))
    res.snapshot = {"ts": utc_now(), "routing_sha": table_sha(table), "enforcement": plan.enforcement,
                    "key_scope": plan.key_scope, "preflight": level,
                    "providers_map_sha": routing_sha(providers_map) if providers_map else None,
                    "keys": snap_keys, "pricing": pricing, "catalog": snapshot_catalog}
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


def main(argv=None) -> int:
    """``--config eval.yaml [--model URI] [--level strict|warn] [--run-dir DIR]``:
    the run's preflight without a run — no spend, and no per-run key even at
    ``key-guardrail`` (the operator key is used for the key/eligibility checks)."""
    from agent_eval.config import EvalConfig, OpenRouterConfig  # call-time: providers never import config at load
    from agent_eval.providers.base import ProviderPlan
    from agent_eval.providers.openrouter.plan import (
        build_plan, effective_roles, judge_routing_keys, plan_is_active)

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--model", default=None, help="skill model override (openrouter:/…)")
    parser.add_argument("--level", default=None, choices=("strict", "warn"))
    parser.add_argument("--run-dir", type=Path, default=None, help="where to write routing_snapshot.json")
    args = parser.parse_args(argv)
    try:
        config = EvalConfig.from_yaml(args.config)
        roles = effective_roles(config, {"skill": args.model})
        if not plan_is_active(roles):
            print("nothing to preflight: the effective skill model is not an openrouter:/ URI", file=sys.stderr)
            return 2
        orc = getattr(getattr(config.models, "providers", None), "openrouter", None) or OpenRouterConfig()
        plan = build_plan(config, roles, require_key=False)
        key = os.environ.get(orc.api_key_env)
        if not key:
            raise ConfigError(f"set {orc.api_key_env}: the key and eligibility checks need the inference key")
        plan = ProviderPlan(**{**plan.__dict__, "key": key, "session": None})
        res = run_preflight(plan, level=args.level or orc.preflight, run_dir=args.run_dir,
                            judges=judge_routing_keys(config))
    except (ConfigError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for w in res.warnings:
        print(f"WARNING: {w}")
    checked = ", ".join(sorted(res.snapshot["keys"])) if res.snapshot else "-"
    print(f"preflight {res.level}: {'degraded (' + res.degraded_reason + ')' if res.degraded_reason else 'ok'} "
          f"| keys: {checked}" + (f" | snapshot: {res.snapshot_path}" if res.snapshot_path else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
