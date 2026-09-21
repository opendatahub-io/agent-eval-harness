"""Provider-neutral building blocks: error classes and the routing key."""

from __future__ import annotations

from typing import Optional

# How a failure is attributed in run/judge records. ``config`` = the eval
# author has to change something (unroutable pins, unknown slug); ``infra`` =
# the harness/network side; ``provider`` = the model host failed a request that
# was well-formed; ``agent`` = the model under test misbehaved.
ERROR_CLASSES = ("config", "infra", "provider", "agent")


def routing_key(model: Optional[str]) -> str:
    """Routing-table key for a provider model id.

    The bare slug without its ``:variant`` suffix, lowercased —
    ``z-ai/glm-5.2:exacto`` and ``Z-AI/glm-5.2`` both key ``z-ai/glm-5.2`` —
    so one ``routing.models`` entry covers every variant of a model.
    """
    value = (model or "").strip()
    slug, _, _variant = value.partition(":")
    return slug.strip().lower()


class JudgeProviderError(RuntimeError):
    """A provider-side failure of an LLM judge request.

    Raised instead of feeding a degraded response to the text parser: a
    committed-200 upstream error (OpenRouter reports provider failures inside a
    200 body), a degraded ``tool_choice`` mode that did not yield the requested
    tool call (Decision 25 strict parse), or a routing 404 that survived the
    ``tool_choice`` ladder. ``error_class`` follows ``ERROR_CLASSES``;
    ``retryable`` tells ``_with_judge_retries`` whether another attempt can heal
    it (``provider_unavailable``/``provider_overloaded`` can, a config error
    cannot).
    """

    def __init__(self, message: str, *, error_type: str = "provider_error",
                 error_class: str = "provider", retryable: bool = False,
                 provider: Optional[str] = None,
                 tool_choice_mode: Optional[str] = None):
        if error_class not in ERROR_CLASSES:
            raise ValueError(f"unknown error_class {error_class!r}")
        super().__init__(message)
        self.error_type = error_type
        self.error_class = error_class
        self.retryable = retryable
        self.provider = provider
        self.tool_choice_mode = tool_choice_mode
