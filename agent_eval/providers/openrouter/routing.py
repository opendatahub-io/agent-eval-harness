"""OpenRouter routing declarations (spec 014, Contracts → RoutingSpec).

A ``RoutingSpec`` declares *where* an OpenRouter model may be served: provider
``order``/``only``/``ignore``, fallback policy, quantizations, sort, data
policy, price ceiling and model ``fallbacks``. One declaration, two readers:

- the **agent path** (later PRs): a declaration of intent that never reaches
  the request body — Claude Code cannot send ``provider``/``models`` — checked
  by preflight and audited post hoc;
- the **judge path** (this module): ``to_chat_extra_body()`` sends the spec
  inline as the ``extra_body`` of the Chat Completions request.

Merge order (later wins, per field, **lists replace** — ``order`` and
``quantizations`` are complete statements): ``routing.defaults`` ←
``routing.models.<routing key>`` ← role override (a judge's
``provider_options.routing``).

Judge pins are **opt-in** (Decision 25). A forced ``tool_choice`` combined with
provider pins makes OpenRouter answer 404 "No endpoints found" whenever a
pinned endpoint cannot force a named function, so a judge sends
``order``/``only``/``ignore``/``quantizations``/``allow_fallbacks`` only when
``models.providers.openrouter.judge.inherit_pins`` is true or the judge itself
sets ``provider_options.routing``. An unpinned judge keeps the non-binding
keys (``sort``, ``data_collection``, ``zdr``, ``max_price``, ``fallbacks``) and
never sends ``require_parameters``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, replace
from typing import Any, Optional

from agent_eval.providers.base import routing_key

QUANTIZATIONS = ("int4", "int8", "fp4", "fp6", "fp8", "fp16", "bf16", "fp32",
                 "unknown")
SORT_VALUES = ("price", "throughput", "latency")
DATA_COLLECTION_VALUES = ("allow", "deny")
MAX_PRICE_KEYS = ("prompt", "completion", "request", "image", "audio")
MAX_FALLBACKS = 3

_LIST_KEYS = ("order", "only", "ignore", "quantizations", "fallbacks")
_PROVIDER_LIST_KEYS = ("order", "only", "ignore")
_BOOL_KEYS = ("allow_fallbacks", "require_parameters", "zdr")
# Provider keys that bind a request to specific endpoints. A judge sends them
# only when it inherits pins; ``require_parameters`` rides along with them.
_PIN_KEYS = ("order", "only", "ignore", "quantizations", "allow_fallbacks",
             "require_parameters")


def normalize_provider(name: str) -> str:
    """Lowercase-slug form of a provider identifier (``Z.AI`` → ``z-ai``,
    ``Amazon Bedrock`` → ``amazon-bedrock``).

    Cosmetic: OpenRouter accepts slugs and display names alike, case-
    insensitively (probe #4). Normalising keeps ``routing_sha`` and the ledger
    ``provider`` field stable across spellings. Catalog-backed validation of
    unknown names is a preflight concern (later PR).
    """
    value = (name or "").strip().lower()
    return "-".join(part for part in value.replace(".", "-").split() if part)


def _check_str_list(value, context) -> list:
    if isinstance(value, str):
        value = [value]
    if (not isinstance(value, list) or not value
            or not all(isinstance(v, str) and v.strip() for v in value)):
        raise ValueError(f"{context} must be a non-empty list of strings")
    return [v.strip() for v in value]


@dataclass(frozen=True)
class RoutingSpec:
    """One routing declaration. Every field is optional; ``None`` = unset (the
    key is not sent and does not override a lower-precedence value)."""

    order: Optional[tuple] = None
    allow_fallbacks: Optional[bool] = None
    require_parameters: Optional[bool] = None
    quantizations: Optional[tuple] = None
    sort: Optional[Any] = None
    only: Optional[tuple] = None
    ignore: Optional[tuple] = None
    data_collection: Optional[str] = None
    zdr: Optional[bool] = None
    max_price: Optional[dict] = None
    fallbacks: Optional[tuple] = None

    @classmethod
    def field_names(cls) -> tuple:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_dict(cls, raw, context: str = "routing") -> "RoutingSpec":
        """Parse and validate a routing mapping (config or ``provider_options``)."""
        if raw is None:
            return cls()
        if isinstance(raw, RoutingSpec):
            return raw
        if not isinstance(raw, dict):
            raise ValueError(f"{context} must be a mapping")
        names = cls.field_names()
        unknown = set(raw) - set(names)
        if unknown:
            raise ValueError(
                f"{context} has unknown key(s): {', '.join(sorted(unknown))} "
                f"(allowed: {', '.join(names)})")
        kw = {}
        for key, value in raw.items():
            if value is None:
                continue
            ctx = f"{context}.{key}"
            if key in _LIST_KEYS:
                items = _check_str_list(value, ctx)
                if key in _PROVIDER_LIST_KEYS:
                    items = [normalize_provider(v) for v in items]
                elif key == "quantizations":
                    items = [v.lower() for v in items]
                    bad = [v for v in items if v not in QUANTIZATIONS]
                    if bad:
                        raise ValueError(
                            f"{ctx}: unknown quantization(s) {bad}; "
                            f"expected one of {list(QUANTIZATIONS)}")
                elif key == "fallbacks" and len(items) > MAX_FALLBACKS:
                    raise ValueError(
                        f"{ctx} lists {len(items)} models; at most "
                        f"{MAX_FALLBACKS} fallbacks are allowed")
                kw[key] = tuple(items)
            elif key in _BOOL_KEYS:
                if not isinstance(value, bool):
                    raise ValueError(f"{ctx} must be a boolean")
                kw[key] = value
            elif key == "sort":
                kw[key] = _check_sort(value, ctx)
            elif key == "data_collection":
                if value not in DATA_COLLECTION_VALUES:
                    raise ValueError(
                        f"{ctx} must be one of {list(DATA_COLLECTION_VALUES)}")
                kw[key] = value
            elif key == "max_price":
                kw[key] = _check_max_price(value, ctx)
        return cls(**kw)

    # -- composition -------------------------------------------------------

    def merged(self, other: Optional["RoutingSpec"]) -> "RoutingSpec":
        """``other`` wins on every field it sets; lists replace, never extend."""
        if other is None:
            return self
        updates = {f.name: getattr(other, f.name) for f in fields(self)
                   if getattr(other, f.name) is not None}
        return replace(self, **updates)

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))

    @property
    def is_pinned(self) -> bool:
        """Whether the declaration binds requests to specific providers."""
        return bool(self.order or self.only)

    def to_dict(self) -> dict:
        """Set fields only, JSON-shaped (tuples → lists)."""
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            out[f.name] = (list(value) if isinstance(value, tuple)
                           else dict(value) if isinstance(value, dict) else value)
        return out

    # -- request bodies -----------------------------------------------------

    def provider_body(self, *, pinned: bool) -> dict:
        """The request's ``provider`` object.

        ``pinned=False`` drops every binding key and ``require_parameters``;
        ``pinned=True`` sends the full declaration and defaults
        ``require_parameters`` to ``True`` (the judge's tool schema must be
        honoured by whichever pinned endpoint serves it) unless the declaration
        set it to ``False`` explicitly.
        """
        body = self.to_dict()
        body.pop("fallbacks", None)
        if not pinned:
            for key in _PIN_KEYS:
                body.pop(key, None)
        elif "require_parameters" not in body:
            body["require_parameters"] = True
        return body

    def to_chat_extra_body(self, slug: str, role: str = "judge", *,
                           inherit_pins: bool = False,
                           overrides: Optional[Any] = None) -> dict:
        """``extra_body`` for a Chat Completions request on ``slug``.

        For ``role="judge"`` the binding keys are sent only when the judge
        inherits pins (``inherit_pins``) or carries a role override
        (``overrides`` — a per-judge ``provider_options.routing`` mapping or
        ``RoutingSpec``). Other roles send the declaration as-is (nothing
        consumes that today: the agent path never sends a body). ``fallbacks``
        become the ``models`` array with ``slug`` first.
        """
        override_spec = (RoutingSpec.from_dict(overrides, context="overrides")
                         if overrides is not None else None)
        spec = self.merged(override_spec)
        if role == "judge":
            pinned = spec.is_pinned and (inherit_pins or override_spec is not None)
        else:
            pinned = spec.is_pinned
        body = {}
        provider = spec.provider_body(pinned=pinned)
        if provider:
            body["provider"] = provider
        if spec.fallbacks:
            body["models"] = [slug, *spec.fallbacks]
        return body


def _check_sort(value, ctx):
    if isinstance(value, str):
        if value not in SORT_VALUES:
            raise ValueError(f"{ctx} must be one of {list(SORT_VALUES)}")
        return value
    if isinstance(value, dict):
        unknown = set(value) - {"by", "partition"}
        if unknown or value.get("by") not in SORT_VALUES:
            raise ValueError(
                f"{ctx} mapping form is {{by: {'|'.join(SORT_VALUES)}, "
                f"partition: model|none}}")
        if "partition" in value and value["partition"] not in ("model", "none"):
            raise ValueError(f"{ctx}.partition must be 'model' or 'none'")
        return dict(value)
    raise ValueError(f"{ctx} must be a string or a mapping")


def _check_max_price(value, ctx):
    if not isinstance(value, dict) or not value:
        raise ValueError(
            f"{ctx} must be a mapping of {list(MAX_PRICE_KEYS)} to USD numbers")
    unknown = set(value) - set(MAX_PRICE_KEYS)
    if unknown:
        raise ValueError(f"{ctx} has unknown key(s): {sorted(unknown)}")
    for k, v in value.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{ctx}.{k} must be a non-negative number")
    return dict(value)


@dataclass(frozen=True)
class RoutingTable:
    """``routing.defaults`` plus per-model entries keyed by ``routing_key``."""

    defaults: RoutingSpec = field(default_factory=RoutingSpec)
    models: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw, context: str = "routing") -> "RoutingTable":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError(f"{context} must be a mapping")
        unknown = set(raw) - {"defaults", "models"}
        if unknown:
            raise ValueError(
                f"{context} has unknown key(s): {', '.join(sorted(unknown))} "
                "(allowed: defaults, models)")
        defaults = RoutingSpec.from_dict(raw.get("defaults"),
                                         context=f"{context}.defaults")
        models_raw = raw.get("models") or {}
        if not isinstance(models_raw, dict):
            raise ValueError(f"{context}.models must be a mapping of model slug → routing")
        models = {}
        for slug, spec_raw in models_raw.items():
            if not isinstance(slug, str) or "/" not in slug:
                raise ValueError(
                    f"{context}.models keys must be '<author>/<slug>' model ids; "
                    f"got {slug!r}")
            key = routing_key(slug)
            if key in models:
                raise ValueError(
                    f"{context}.models declares {key!r} twice (variants share "
                    "one entry)")
            models[key] = RoutingSpec.from_dict(
                spec_raw, context=f"{context}.models.{slug}")
        return cls(defaults=defaults, models=models)

    @property
    def is_empty(self) -> bool:
        return self.defaults.is_empty and not self.models

    def has_entry(self, model: str) -> bool:
        return routing_key(model) in self.models

    def for_model(self, model: str) -> RoutingSpec:
        """Effective declaration for ``model``: defaults ← models entry."""
        return self.defaults.merged(self.models.get(routing_key(model)))


def judge_extra_body(table: Optional[RoutingTable], model: str, *,
                     inherit_pins: bool = False,
                     provider_options: Optional[dict] = None) -> dict:
    """Routing part of a judge request's ``extra_body`` (Decision 25 rule).

    ``provider_options`` is the judge's validated mapping: ``routing`` acts as
    the role override (and opts the judge into pins), ``fallbacks`` only adds a
    ``models`` array (non-binding, never implies pins).
    """
    opts = provider_options or {}
    spec = (table or RoutingTable()).for_model(model)
    override = opts.get("routing")
    fallbacks = opts.get("fallbacks")
    if fallbacks:
        spec = replace(spec, fallbacks=tuple(
            _check_str_list(fallbacks, "provider_options.fallbacks")))
    return spec.to_chat_extra_body(model, role="judge", inherit_pins=inherit_pins,
                                   overrides=override)


def routing_sha(body: Any) -> str:
    """SHA-256 of the canonical JSON of a routing declaration or ``extra_body``."""
    if isinstance(body, RoutingSpec):
        body = body.to_dict()
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
