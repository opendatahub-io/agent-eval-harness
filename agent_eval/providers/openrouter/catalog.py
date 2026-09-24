"""OpenRouter's public catalog (spec 014): providers, models, endpoints.

``ModelCatalog`` caches the three public GETs and answers the joins the
reconcile/audit need — display provider name → slug, dated permaslug →
model id, ``(model, provider) → (quantization, endpoint_tag)`` — plus
``pricing_for`` for the report's price context. The fetcher is injectable so
tests and the frozen ``routing_snapshot.json`` never touch the network.
"""

from __future__ import annotations

from typing import Callable, Optional

from agent_eval.providers.base import routing_key
from agent_eval.providers.openrouter.http import BASE_URL, get_json
from agent_eval.providers.openrouter.routing import normalize_provider


def _data(payload):
    if isinstance(payload, dict):
        data = payload.get("data")
        if data is not None:
            return data
    return payload if isinstance(payload, list) else []


class ModelCatalog:
    """Lazily fetched, memoised view of the public catalog."""

    def __init__(self, fetch: Optional[Callable] = None, *, base_url: str = BASE_URL,
                 timeout: float = 15):
        self._fetch = fetch or (lambda url: get_json(url, timeout=timeout))
        self._base = base_url.rstrip("/")
        self._providers = None
        self._models = None
        self._endpoints: dict = {}

    # -- construction from a frozen snapshot ----------------------------------

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "ModelCatalog":
        """A catalog answering only from a recorded view (no network):
        ``{"providers": [...], "models": [...], "endpoints": {slug: [...]}}``."""
        cat = cls(fetch=lambda url: {"data": []})     # unknown slugs answer empty offline
        cat._providers = list(snapshot.get("providers") or [])
        cat._models = list(snapshot.get("models") or [])
        cat._endpoints = {routing_key(k): list(v or [])
                          for k, v in (snapshot.get("endpoints") or {}).items()}
        return cat

    def to_snapshot(self, slugs=()) -> dict:
        """The frozen view for ``routing_snapshot.json`` (fetches what is missing)."""
        return {"providers": self.providers(), "models": self.models(),
                "endpoints": {routing_key(s): self.endpoints(s) for s in slugs}}

    # -- public GETs ------------------------------------------------------------

    def providers(self) -> list:
        if self._providers is None:
            self._providers = _data(self._fetch(f"{self._base}/v1/providers"))
        return self._providers

    def models(self) -> list:
        if self._models is None:
            self._models = _data(self._fetch(f"{self._base}/v1/models"))
        return self._models

    def endpoints(self, slug: str) -> list:
        key = routing_key(slug)
        if key not in self._endpoints:
            payload = self._fetch(f"{self._base}/v1/models/{key}/endpoints")
            data = _data(payload)
            self._endpoints[key] = list(data.get("endpoints") or []) if isinstance(data, dict) else list(data or [])
        return self._endpoints[key]

    # -- joins ------------------------------------------------------------------

    def provider_slug(self, name: Optional[str]) -> Optional[str]:
        """Catalog slug for a provider display name or slug (``Novita`` →
        ``novita``); the normalised form when the catalog does not list it."""
        if not name:
            return None
        wanted = normalize_provider(name)
        try:
            for entry in self.providers():
                slug = entry.get("slug") or ""
                display = entry.get("name") or ""
                if wanted in (normalize_provider(slug), normalize_provider(display)):
                    return slug or wanted
        except Exception:
            pass
        return wanted

    def canonical_to_id(self, permaslug: Optional[str]) -> Optional[str]:
        """Model id for a dated permaslug (``z-ai/glm-5.2-20260616`` →
        ``z-ai/glm-5.2``) via ``/models`` ``canonical_slug``; None when unknown."""
        if not permaslug:
            return None
        key = routing_key(permaslug)
        try:
            for entry in self.models():
                if routing_key(entry.get("canonical_slug") or "") == key:
                    return entry.get("id")
        except Exception:
            return None
        return None

    def endpoint_for(self, slug: str, provider: Optional[str]) -> Optional[dict]:
        """The endpoint of ``slug`` served by ``provider`` (slug or display
        name); None when absent or ambiguous (two quantizations)."""
        wanted = self.provider_slug(provider)
        if not wanted:
            return None
        matches = [e for e in self.endpoints(slug)
                   if self.provider_slug(e.get("provider_name") or e.get("provider")) == wanted]
        if len(matches) != 1:
            return None
        return matches[0]

    def quantization_for(self, slug: str, provider: Optional[str]):
        """``(quantization, endpoint_tag)`` for ``(slug, provider)``; ``(None,
        None)`` when the provider does not serve the model or serves it at more
        than one quantization (ambiguous — the audit names it)."""
        endpoint = self.endpoint_for(slug, provider)
        if endpoint is None:
            return None, None
        return endpoint.get("quantization"), endpoint.get("tag")

    def pricing_for(self, slug: str, provider: Optional[str]) -> Optional[dict]:
        endpoint = self.endpoint_for(slug, provider)
        return (endpoint or {}).get("pricing")

    def min_prompt_price(self, slug: str) -> Optional[float]:
        """Lowest per-token prompt price across the model's endpoints (USD)."""
        prices = []
        for e in self.endpoints(slug):
            try:
                prices.append(float((e.get("pricing") or {}).get("prompt")))
            except (TypeError, ValueError):
                continue
        return min(prices) if prices else None
