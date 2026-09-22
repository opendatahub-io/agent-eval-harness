"""The Direct transport env template (spec 014) — one function, three targets.

``settings_env_block(plan)`` is the single source of the environment an
OpenRouter-routed Claude Code agent starts with. Every writer renders it:
the local settings overlay (``claude-code``), Harbor's value-free
``--agent-env`` carriers (``harbor-podman``) and the Kubernetes pod manifest
(``harbor-k8s`` / ``evalhub``). ``MANAGED_ENV_KEYS`` is the union of every key
the template can emit; config validation, the interception skip list and the
container forwarding exclusions all reuse it so the writers cannot drift.
"""

from __future__ import annotations

from typing import Optional

TARGETS = ("overlay", "harbor_carrier", "k8s_pod")
SECRET_MODES = ("literal", "ref", "omit")
# How each target carries the inference key: the 0600 overlay may hold the
# literal, a Harbor carrier holds a ``$VAR`` reference the writer resolves in
# the child env, a pod gets it from a Secret (``secretKeyRef``) — omitted here.
_DEFAULT_SECRETS = {"overlay": "literal", "harbor_carrier": "ref", "k8s_pod": "omit"}

# Blanked (empty string, not unset) so a user-level settings.json or a
# forwarded host value cannot re-route the run to Vertex/Bedrock.
BLANKED_KEYS = (
    "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    "GOOGLE_CLOUD_PROJECT", "CLAUDE_CODE_USE_BEDROCK", "AWS_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
)
ALIAS_KEYS = (
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
)
# Keys whose plan value is secret or run-specific: an authored value on any
# env surface is rejected on presence while a plan is active.
DYNAMIC_MANAGED_KEYS = frozenset({
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"})

MANAGED_ENV_KEYS = frozenset({
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
    "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "GOOGLE_CLOUD_PROJECT",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_BEDROCK", "AWS_REGION",
    "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
})


def custom_headers(attribution, run_id: Optional[str]) -> Optional[str]:
    """``ANTHROPIC_CUSTOM_HEADERS`` value: one ``Header: value`` per line
    (Claude Code sends each line as its own header on root and subagent
    requests). None when there is nothing to send."""
    lines = []
    referer = getattr(attribution, "referer", None)
    title = getattr(attribution, "title", None)
    if referer:
        lines.append(f"HTTP-Referer: {referer}")
    if title:
        lines.append(f"X-OpenRouter-Title: {title}")
    if getattr(attribution, "run_id_header", False) and run_id:
        lines.append(f"x-eval-run-id: {run_id}")
    return "\n".join(lines) or None


def settings_env_block(plan, *, secrets: Optional[str] = None,
                       target: str = "overlay") -> dict:
    """Render the Direct transport env template for ``plan``.

    ``target`` picks how the inference key travels (see ``_DEFAULT_SECRETS``);
    ``secrets`` overrides it: ``"literal"`` puts the key value in
    ``ANTHROPIC_AUTH_TOKEN``, ``"ref"`` puts ``$<key_env>`` (resolved by the
    writer's ``$VAR`` logic, never a literal in config or argv), ``"omit"``
    leaves the key out. Every other value is identical across targets.
    """
    if target not in TARGETS:
        raise ValueError(f"unknown env target {target!r}; expected one of {TARGETS}")
    secrets = secrets or _DEFAULT_SECRETS[target]
    if secrets not in SECRET_MODES:
        raise ValueError(f"unknown secrets mode {secrets!r}; expected one of {SECRET_MODES}")

    block = {"ANTHROPIC_BASE_URL": plan.base_url}
    if secrets == "literal":
        if not plan.key:
            raise ValueError(
                f"plan carries no inference key; set {plan.key_env} or render "
                "with secrets='ref'/'omit'")
        block["ANTHROPIC_AUTH_TOKEN"] = plan.key
    elif secrets == "ref":
        block["ANTHROPIC_AUTH_TOKEN"] = f"${plan.key_env}"
    # Hygiene: a non-empty ANTHROPIC_API_KEY would be sent as x-api-key (which
    # works); the blank keeps a stale host key or cached OAuth state off the wire.
    block["ANTHROPIC_API_KEY"] = ""
    for key in BLANKED_KEYS:
        block[key] = ""
    skill_id = plan.skill.id
    for key in ALIAS_KEYS:
        block[key] = skill_id
    # Alias rule (Decision 9): the haiku slot is the model under test unless a
    # background model is an explicit, recorded choice.
    block["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = plan.background_model or skill_id
    block["CLAUDE_CODE_SUBAGENT_MODEL"] = plan.subagent.id
    headers = custom_headers(plan.attribution, plan.run_id)
    if headers:
        block["ANTHROPIC_CUSTOM_HEADERS"] = headers
    block["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    unmanaged = set(block) - MANAGED_ENV_KEYS
    assert not unmanaged, f"env template emits unmanaged key(s): {sorted(unmanaged)}"
    return block
