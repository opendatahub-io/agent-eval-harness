"""Build the agent-side :class:`ProviderPlan` for an OpenRouter run (spec 014).

There is one transport on every runner: the ``runner`` argument selects only
how the env block is delivered (``overlay`` / ``harbor_carrier`` /
``k8s_pod``). Nothing about routing, cost or budget differs per runner.
"""

from __future__ import annotations

import os
from typing import Optional

from agent_eval.providers.base import (
    PLAN_RUNNERS,
    AgentModel,
    ConfigError,
    ProviderPlan,
    parse_agent_model,
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


def build_plan(config, roles: Optional[dict] = None, *, runner: str = "claude-code",
               run_id: Optional[str] = None, require_key: bool = True) -> ProviderPlan:
    """The plan for ``config`` with ``roles`` (effective role URIs; defaults
    to ``config.models``).

    ``require_key=False`` builds a keyless plan for validation and dry runs
    (its env block can only be rendered with ``secrets="ref"``/``"omit"``).
    ``routing.enforcement: key-guardrail`` is parsed by the config but the
    per-run key provisioning lands in a later release, so building such a plan
    is a :class:`ConfigError` for now.
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
    if require_key:
        # A keyless plan is a validation/dry-run view; the real build is where
        # the per-run key would be provisioned — not implemented yet.
        if enforcement == "key-guardrail":
            raise ConfigError(
                "models.providers.openrouter.routing.enforcement: key-guardrail (per-run "
                "keys) lands in a later release; use 'audit' for now")
        key = os.environ.get(orc.api_key_env)
        if not key:
            raise ConfigError(
                f"set {orc.api_key_env}: the OpenRouter agent plan needs the inference "
                "key in the harness environment (its value is never echoed)")
    return ProviderPlan(
        kind="openrouter",
        base_url=orc.base_url,
        key_scope="operator",
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
    )
