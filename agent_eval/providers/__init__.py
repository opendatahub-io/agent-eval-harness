"""Provider registry (spec 014).

A *provider* is a model host reachable through a ``<provider>:/<model>`` URI on
the ``models.*`` roles. The only declared kind in this release is
``openrouter``; the transport to the agent (Claude Code → OpenRouter) lands in
later PRs, this package currently carries the judge-side pieces: routing
declarations (``openrouter.routing``) and the error taxonomy shared by the
judge transport (``base``).

The package is deliberately stdlib-only: no HTTP client dependency is pulled in
for a provider declaration.
"""

from agent_eval.providers.base import (
    ERROR_CLASSES,
    AgentModel,
    ConfigError,
    ErrorClass,
    JudgeProviderError,
    ProviderKind,
    ProviderPlan,
    parse_agent_model,
    routing_key,
)

__all__ = ["ERROR_CLASSES", "AgentModel", "ConfigError", "ErrorClass", "JudgeProviderError",
           "ProviderKind", "ProviderPlan", "parse_agent_model", "routing_key"]
