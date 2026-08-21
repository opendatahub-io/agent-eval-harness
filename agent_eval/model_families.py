"""Conservative model-id → provider-family inference (pure stdlib).

The ONE family-inference implementation for the measurement-validity
program. Consumers: the validity block's same-family caveat (paper
Appendix B.4 — same-family agents, judges, and simulators share training
lineage and can fail in correlated ways) and, later, judge-panel family
composition labels.

Conservative by design: ``None`` means unknown, and unknown means callers
stay SILENT — never warn, never claim a family for an id they cannot
classify. Opaque gateway aliases stay unclassified on purpose; matching is
anchored (``olmo`` never matches the OpenAI ``o1/o3/o4`` rule, ``gemma``
never matches ``gemini``).

Stdlib only — this module sits on the scoring path (score.py), which the
purity guard keeps free of heavyweight stats/data stacks.
"""

import re

# LiteLLM-style route prefixes (``anthropic/claude-…``, ``vertex_ai/gemini-…``,
# ``openrouter/anthropic/claude-…``): the segments before the last ``/`` are
# routing, not identity — strip them and classify the final model id only.
# The route name itself is never used as family evidence.

#: Bedrock-style vendor-dot ids: an optional region prefix, then
#: ``<vendor>.<model>`` (``us.anthropic.claude-…``, ``meta.llama3-…``).
_REGION_PREFIXES = frozenset({"us", "eu", "apac", "global"})

_VENDOR_DOT_FAMILIES = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "meta": "meta",
    "mistral": "mistral",
    "qwen": "qwen",
    "deepseek": "deepseek",
    "cohere": "cohere",
    "amazon": "amazon",
    "xai": "xai",
}

#: Anchored model-name rules, checked in order. Every pattern requires the
#: family token at the START of the id followed by a separator/digit/end, so
#: lookalike names ("olmo", "gemma", "commander"?) cannot match.
_FAMILY_PATTERNS = (
    ("anthropic", re.compile(r"^claude(?:[-.\d]|$)")),
    # gpt-*, chatgpt*, and the o1/o3/o4 reasoning series only — 'olmo',
    # 'gemma' and other o-/g-prefixed ids never match this anchor.
    ("openai", re.compile(r"^(?:gpt-|chatgpt|o[134](?:[.-]|$))")),
    ("google", re.compile(r"^gemini(?:[-.\d]|$)")),
    ("meta", re.compile(r"^llama(?:[-.\d]|$)")),
    ("mistral", re.compile(r"^(?:mistral|mixtral|codestral|ministral|pixtral)"
                           r"(?:[-.\d]|$)")),
    ("qwen", re.compile(r"^(?:qwen|qwq)(?:[-.\d]|$)")),
    ("deepseek", re.compile(r"^deepseek(?:[-.\d]|$)")),
    ("cohere", re.compile(r"^command(?:[-.\d]|$)")),
    ("amazon", re.compile(r"^(?:titan|nova)(?:[-.\d]|$)")),
    ("xai", re.compile(r"^grok(?:[-.\d]|$)")),
)


def infer_model_family(model_id):
    """Provider family for a model id, or ``None`` when unknown.

    ``None`` = unknown = the caller stays silent (no warning, no claim).
    Handles LiteLLM route prefixes (``anthropic/…``, ``vertex_ai/…`` — the
    last path segment is the model id) and Bedrock vendor-dot ids
    (``anthropic.claude-…``, ``us.anthropic.claude-…``).
    """
    if not isinstance(model_id, str):
        return None
    mid = model_id.strip().lower()
    if not mid:
        return None
    # Route prefixes are routing, not identity — classify the last segment.
    mid = mid.split("/")[-1].strip()
    if not mid:
        return None
    # Bedrock vendor-dot: optional region prefix, then <vendor>.<model>.
    parts = mid.split(".")
    if len(parts) > 1 and parts[0] in _REGION_PREFIXES:
        parts = parts[1:]
    if len(parts) > 1 and parts[0] in _VENDOR_DOT_FAMILIES and parts[1]:
        return _VENDOR_DOT_FAMILIES[parts[0]]
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(mid):
            return family
    return None


def family_composition(model_ids):
    """Family → count over an iterable of model ids.

    Unclassifiable ids are counted under the ``"unknown"`` key — counted,
    never named: no family is ever claimed for an unknown id.
    """
    composition: dict = {}
    for model_id in model_ids or []:
        family = infer_model_family(model_id) or "unknown"
        composition[family] = composition.get(family, 0) + 1
    return composition


def same_family_advisory(role_models, panel_models=()):
    """Appendix-B.4 advisory text, or ``None`` when no claim can be made.

    ``role_models`` are the explicitly configured role/judge model ids
    (``models.skill/subagent/judge/hook`` when set, plus per-judge single
    models); ``panel_models`` is the flat list of ``judges[].model`` panel
    entries. Returns the warning text when >= 2 collected ids ALL classify
    (no unknowns anywhere) into exactly ONE provider family; otherwise
    ``None``. An unclassifiable id (opaque gateway alias) silences the claim
    by design — unknown means silent, never a warning.

    The CALLER decides when the check is engaged. Per user decision Q2 it
    fires only when reliability features are in play — a ``judges[].model``
    panel or a consequence-tagged judge (``models.hook_shadow`` does not
    exist yet; it joins the engagement test when it ships) — and at most
    once per config load. The run-report same-family caveat is separate and
    always renders.
    """
    models = [m for m in list(role_models or []) + list(panel_models or [])
              if isinstance(m, str) and m.strip()]
    if len(models) < 2:
        return None
    families = [infer_model_family(m) for m in models]
    if any(f is None for f in families):
        return None
    if len(set(families)) != 1:
        return None
    return (
        f"All configured models resolve to one provider family "
        f"({families[0]}): correlated failures across generation/judgment/"
        "simulation layers (arXiv 2608.00794 Appendix B.4). Consider a "
        "cross-family judge panel (judges[].model as a list) — "
        "non-Anthropic members require an Anthropic-Messages-compatible "
        "gateway via ANTHROPIC_BASE_URL."
    )
