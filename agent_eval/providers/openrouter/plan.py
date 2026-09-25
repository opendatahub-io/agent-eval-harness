"""Build the agent-side :class:`ProviderPlan` for an OpenRouter run (spec 014).

There is one transport on every runner: the ``runner`` argument selects only
how the env block is delivered (``overlay`` / ``harbor_carrier`` /
``k8s_pod``). Nothing about routing, cost or budget differs per runner.
"""

from __future__ import annotations

import atexit
import os
from typing import Optional

from agent_eval.providers.base import (
    PLAN_RUNNERS,
    AgentModel,
    ConfigError,
    ProviderPlan,
    parse_agent_model,
    routing_key,
)

AGENT_ROLES = ("skill", "subagent", "hook")


def effective_roles(config, overrides: Optional[dict] = None) -> dict:
    """Role URIs after CLI overrides (``{"skill": ..., "subagent": ...}``),
    falling back to ``config.models``. Missing roles are ``None``."""
    overrides = overrides or {}
    models = config.models
    return {role: overrides.get(role) or getattr(models, role, None) for role in AGENT_ROLES}


def plan_is_active(roles: dict) -> bool:
    """Activation rule (Decision 21): the plan exists iff the effective skill
    model is an ``openrouter:/`` URI."""
    skill = roles.get("skill")
    if not skill:
        return False
    try:
        return parse_agent_model(skill).provider == "openrouter"
    except ValueError:
        return False


def hook_model_for(config, overrides: Optional[dict] = None) -> Optional[str]:
    """The model the harness's own hook (tool interception answers) uses.

    ``models.hook`` when set. Under an active plan the hook subprocess inherits
    the agent's OpenRouter env, so an unset hook must not fall back to the
    built-in Claude default (a ``claude-haiku-*`` slug 404s on OpenRouter): it
    follows the ``background_model`` if one is declared, else the skill model.
    ``None`` otherwise (the caller keeps its own default).
    """
    roles = effective_roles(config, overrides)
    if roles.get("hook"):
        # The hook client takes a bare model id; strip a provider prefix
        # (an unparseable gateway alias is passed through as written).
        try:
            return parse_agent_model(roles["hook"]).id
        except ValueError:
            return roles["hook"]
    if not plan_is_active(roles):
        return None
    orc = getattr(getattr(config.models, "providers", None), "openrouter", None)
    background = getattr(orc, "background_model", None) if orc is not None else None
    return background or parse_agent_model(roles["skill"]).id


def role_models(roles: dict) -> dict:
    """``AgentModel`` per agent role (``None`` where the role is unset); the
    subagent defaults to the skill model."""
    skill = parse_agent_model(roles["skill"])
    subagent = parse_agent_model(roles.get("subagent") or roles["skill"])
    hook = parse_agent_model(roles["hook"]) if roles.get("hook") else None
    return {"skill": skill, "subagent": subagent, "hook": hook}


def guardrail_providers(orc, keys) -> tuple:
    """The per-run key's provider allow-list: ``routing.guardrail.providers``
    when explicit, else the union of the pinned sets of the routing keys the
    agent roles use."""
    from agent_eval.providers.openrouter.audit import pinned_providers

    explicit = orc.routing.guardrail.providers
    if isinstance(explicit, (list, tuple)) and explicit:
        return tuple(sorted(set(explicit)))
    union: set = set()
    for key in keys:
        pins = pinned_providers(orc.routing.for_model(key))
        if pins:
            union |= pins
    return tuple(sorted(union))


def judge_routing_keys(config, judge_model: Optional[str] = None) -> dict:
    """``{routing key: pinned}`` for every ``openrouter:/`` judge of the run —
    ``models.judge`` (or a CLI override) and per-judge ``model:`` values. A
    judge is *pinned* when it inherits the table's pins or carries its own
    ``provider_options.routing`` (Decision 25), which is what the preflight's
    ``tool_choice: function`` check is about."""
    orc = getattr(getattr(config.models, "providers", None), "openrouter", None)
    inherit = bool(getattr(getattr(orc, "judge", None), "inherit_pins", False)) if orc is not None else False
    default = judge_model or getattr(config.models, "judge", None)
    out: dict = {}
    for jc in getattr(config, "judges", None) or []:
        model = getattr(jc, "model", None) or default
        if not (isinstance(model, str) and model.startswith("openrouter:")):
            continue
        try:
            key = parse_agent_model(model).key
        except ValueError:
            continue
        opts = getattr(jc, "provider_options", None) or {}
        out[key] = out.get(key, False) or inherit or bool(isinstance(opts, dict) and opts.get("routing"))
    return out


def build_plan(config, roles: Optional[dict] = None, *, runner: str = "claude-code",
               run_id: Optional[str] = None, require_key: bool = True) -> ProviderPlan:
    """The plan for ``config`` with ``roles`` (effective role URIs; defaults
    to ``config.models``).

    ``require_key=False`` builds a keyless plan for validation and dry runs
    (its env block can only be rendered with ``secrets="ref"``/``"omit"``). At
    ``routing.enforcement: key-guardrail`` the real build provisions the
    per-run key first (management key from ``management_key_env`` — value
    never echoed — ``limit`` = ``budget.run_usd``, allow-list = the pinned
    providers or ``guardrail.providers``), reads it back and refuses to start
    on any mismatch; the plan then carries the per-run key (``key_scope:
    per-run``) and an ``atexit`` fallback revokes it should no ``close()``
    run.
    """
    if runner not in PLAN_RUNNERS:
        raise ConfigError(f"unknown plan runner {runner!r}; expected one of {sorted(PLAN_RUNNERS)}")
    roles = roles or effective_roles(config)
    try:
        models = role_models(roles)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    skill: AgentModel = models["skill"]
    if skill.provider != "openrouter":
        raise ConfigError(
            f"no OpenRouter plan for skill model {skill.uri!r}: the plan is built "
            "only when the effective skill model is 'openrouter:/<author>/<slug>'")
    for role in ("subagent", "hook"):
        model = models[role]
        if model is not None and model.provider != "openrouter":
            raise ConfigError(
                f"models.{role} {model.uri!r} must share the plan's provider kind: "
                f"the agent's env routes every request to OpenRouter, so write "
                f"'openrouter:/<author>/<slug>' (or drop it to inherit the skill model)")

    from agent_eval.config import OpenRouterConfig  # config imports providers; call-time only

    orc = getattr(getattr(config.models, "providers", None), "openrouter", None) or OpenRouterConfig()
    enforcement = orc.routing.enforcement
    key = None
    key_scope = "operator"
    provisioned = None
    if require_key:
        if enforcement == "key-guardrail":
            provisioned = _provision_run_key(orc, models, run_id)
            key, key_scope = provisioned.key, "per-run"
        else:
            key = os.environ.get(orc.api_key_env)
            if not key:
                raise ConfigError(
                    f"set {orc.api_key_env}: the OpenRouter agent plan needs the inference "
                    "key in the harness environment (its value is never echoed)")
    plan = ProviderPlan(
        kind="openrouter",
        base_url=orc.base_url,
        key_scope=key_scope,
        key=key,
        key_env=orc.api_key_env,
        skill=skill,
        subagent=models["subagent"],
        hook=models["hook"],
        background_model=orc.background_model,
        routing=orc.routing,
        enforcement=enforcement,
        run_id=run_id,
        runner=runner,
        attribution=orc.attribution,
        cli_budget_inflation=orc.cli_budget_inflation,
        budget_run_usd=orc.budget.run_usd,
        management_key_env=orc.management_key_env,
        provisioned=provisioned,
    )
    if provisioned is not None:
        from agent_eval.providers.openrouter.keys import revoke_plan_key

        atexit.register(revoke_plan_key, plan)        # idempotent fallback; close() normally wins
    return plan


def _provision_run_key(orc, models: dict, run_id: Optional[str]):
    """Create, read back and validate the per-run key (``key-guardrail``)."""
    from agent_eval.providers.openrouter.keys import (
        guardrail_mismatches, provision, revoke, verify_guardrail)

    management_key = os.environ.get(orc.management_key_env)
    if not management_key:
        raise ConfigError(
            f"set {orc.management_key_env}: routing.enforcement: key-guardrail provisions a "
            "per-run key through the management API (the value is never echoed)")
    limit = orc.budget.run_usd
    if not isinstance(limit, (int, float)) or limit <= 0:
        raise ConfigError("routing.enforcement: key-guardrail needs budget.run_usd > 0 — it becomes "
                          "the per-run key's limit")
    keys = {m.key for m in models.values() if m is not None}
    if orc.background_model:
        keys.add(routing_key(orc.background_model))
    allowed = guardrail_providers(orc, sorted(keys))
    if not allowed:
        raise ConfigError("routing.enforcement: key-guardrail has no provider allow-list — pin a "
                          "routing key (order/only) or set routing.guardrail.providers")
    name = str(orc.routing.guardrail.key_name).replace("{run_id}", run_id or "run")
    provisioned = provision(management_key, name=name, limit_usd=float(limit),
                            allowed_providers=allowed, base_url=orc.base_url)
    problems = guardrail_mismatches(
        verify_guardrail(management_key, provisioned.hash, base_url=orc.base_url), provisioned)
    if problems:
        try:
            revoke(management_key, provisioned.hash, base_url=orc.base_url)
        except Exception:                                   # noqa: BLE001 — best effort, reported below
            problems.append(f"and the key {provisioned.hash} could not be revoked; revoke it by hand")
        raise ConfigError("key-guardrail read-back mismatch — the per-run key was revoked, nothing was "
                          "spent: " + "; ".join(problems))
    return provisioned
