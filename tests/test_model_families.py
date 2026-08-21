"""Conservative model-id → provider-family inference (agent_eval.model_families).

Table-driven: every family row, the anchored-negative lookalikes
(olmo/gemma never match the OpenAI/Google rules), Bedrock vendor-dot ids,
LiteLLM route prefixes, and the silence contract — unknown is None, never
a guess.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent_eval.model_families import (  # noqa: E402
    DEFAULT_HOOK_MODEL, family_composition, infer_model_family,
    same_family_advisory,
)

FAMILY_TABLE = [
    # anthropic — plain, Bedrock vendor-dot, region-prefixed, LiteLLM route
    ("claude-opus-4-8", "anthropic"),
    ("claude-3-5-haiku-20241022", "anthropic"),
    ("anthropic.claude-3-5-sonnet-20241022-v2:0", "anthropic"),
    ("us.anthropic.claude-sonnet-4-20250514-v1:0", "anthropic"),
    ("eu.anthropic.claude-3-haiku-20240307-v1:0", "anthropic"),
    ("anthropic/claude-sonnet-4", "anthropic"),
    # openai — gpt-*, chatgpt, the o1/o3/o4 series, Bedrock, routes
    ("gpt-4o", "openai"),
    ("gpt-4.1-mini", "openai"),
    ("chatgpt-4o-latest", "openai"),
    ("o1", "openai"),
    ("o1-mini", "openai"),
    ("o3", "openai"),
    ("o3-mini", "openai"),
    ("o4-mini", "openai"),
    ("openai/gpt-4.1", "openai"),
    ("openai.gpt-oss-120b-1:0", "openai"),
    # google
    ("gemini-2.0-flash", "google"),
    ("gemini-2.5-pro", "google"),
    ("vertex_ai/gemini-2.5-pro", "google"),
    # meta
    ("llama-3.3-70b-instruct", "meta"),
    ("meta.llama3-70b-instruct-v1:0", "meta"),
    # mistral
    ("mistral-large-latest", "mistral"),
    ("mixtral-8x7b-instruct", "mistral"),
    ("codestral-2501", "mistral"),
    ("mistral.mistral-7b-instruct-v0:2", "mistral"),
    ("mistral/mistral-small-latest", "mistral"),
    # qwen
    ("qwen2.5-72b-instruct", "qwen"),
    ("qwq-32b", "qwen"),
    # deepseek
    ("deepseek-r1", "deepseek"),
    ("deepseek/deepseek-chat", "deepseek"),
    # cohere
    ("command-r-plus", "cohere"),
    ("cohere.command-r-plus-v1:0", "cohere"),
    # amazon
    ("amazon.titan-text-express-v1", "amazon"),
    ("amazon.nova-pro-v1:0", "amazon"),
    ("us.amazon.nova-lite-v1:0", "amazon"),
    ("titan-embed-text-v1", "amazon"),
    # xai
    ("grok-3", "xai"),
    ("xai/grok-4", "xai"),
    # nested route prefixes: only the final segment is classified
    ("openrouter/anthropic/claude-3.5-sonnet", "anthropic"),
    # unknown = None = silent — anchored rules keep lookalikes out
    ("olmo-2-13b", None),          # never matches o[134]
    ("olmo2-13b", None),
    ("gemma-2-27b", None),         # never matches gemini
    ("my-gateway-alias", None),
    ("prod-judge", None),          # opaque gateway alias
    ("phi-4", None),
    ("orca-2-13b", None),
    ("o2", None),                  # not in the o1/o3/o4 series
    ("o11", None),                 # anchored: o1 must end or hit ./-
    ("commander-x", None),         # 'command' anchor requires a separator
    ("gpt2", None),                # conservative: gpt- requires the dash
    ("", None),
    ("   ", None),
    (None, None),
    (42, None),
]


@pytest.mark.parametrize("model_id,family", FAMILY_TABLE,
                         ids=[repr(m) for m, _ in FAMILY_TABLE])
def test_family_table(model_id, family):
    assert infer_model_family(model_id) == family


def test_route_prefix_alone_is_not_evidence():
    """A route prefix with an unclassifiable tail stays unknown — the
    route name is never used as family evidence."""
    assert infer_model_family("anthropic/custom-alias") is None
    assert infer_model_family("openai/my-deployment") is None


def test_case_and_whitespace_insensitive():
    assert infer_model_family("  Claude-Opus-4-8  ") == "anthropic"
    assert infer_model_family("GPT-4o") == "openai"


# ---------------------------------------------------------------------------
# family_composition
# ---------------------------------------------------------------------------

def test_family_composition_counts():
    comp = family_composition([
        "claude-opus-4-8", "claude-haiku-4-5", "gpt-4o",
        "my-gateway-alias", None,
    ])
    assert comp == {"anthropic": 2, "openai": 1, "unknown": 2}


def test_family_composition_empty_and_none():
    assert family_composition([]) == {}
    assert family_composition(None) == {}


def test_family_composition_never_names_an_unknown_family():
    comp = family_composition(["mystery-model"])
    assert set(comp) == {"unknown"}


# ---------------------------------------------------------------------------
# same_family_advisory (Appendix B.4 — the ONE home; config.py imports it)
# ---------------------------------------------------------------------------

def test_advisory_text_on_a_single_known_family():
    text = same_family_advisory(["claude-opus-4-8"], ["claude-haiku-4-5"])
    assert text is not None
    assert "anthropic" in text
    assert "Appendix B.4" in text
    assert "ANTHROPIC_BASE_URL" in text


@pytest.mark.parametrize("role_models,panel_models", [
    (["claude-opus-4-8", "gpt-4o"], []),           # cross-family
    (["claude-opus-4-8", "my-alias"], []),         # unknown anywhere -> silent
    (["claude-opus-4-8"], []),                     # < 2 collected
    ([], []),                                      # nothing configured
    (["claude-opus-4-8"], ["claude-x", "gpt-4o"]),  # cross-family panel member
])
def test_advisory_stays_silent(role_models, panel_models):
    assert same_family_advisory(role_models, panel_models) is None


# ---------------------------------------------------------------------------
# hook_shadow handling (PR10 — cross-family shadow simulators)
# ---------------------------------------------------------------------------

def test_hook_without_shadows_still_counts_in_the_pool():
    """Back-compat with the PR8 behavior, where models.hook rode inside
    role_models: an explicitly configured hook still joins the pool."""
    text = same_family_advisory(["claude-sonnet-4-5"],
                                hook_model="claude-haiku-4-5")
    assert text is not None and "anthropic" in text


def test_within_family_shadows_do_not_suppress():
    """A hook 'mitigated' by same-family shadows is not mitigated at all —
    hook AND shadows join the pool, so the advisory fires on them alone."""
    text = same_family_advisory([], hook_model="claude-haiku-4-5",
                                hook_shadow_models=["claude-opus-4-8"])
    assert text is not None
    assert "hook_shadow" in text  # the mitigation is named in the advisory


def test_shadows_classify_the_default_hook_when_hook_is_unset():
    """models.hook unset + shadows configured: the model that will actually
    answer is the interceptor's hardcoded default — it joins the
    comparison."""
    text = same_family_advisory([],
                                hook_shadow_models=["claude-opus-4-8"])
    assert text is not None and "anthropic" in text


def test_cross_family_shadow_suppresses_the_hook_role():
    """A shadow introducing a second KNOWN family IS the cross-family
    mitigation for the simulator layer — hook and shadows leave the pool."""
    assert same_family_advisory([], hook_model="claude-haiku-4-5",
                                hook_shadow_models=["gemini-2.5-flash"]) \
        is None


def test_cross_family_shadow_suppresses_only_the_hook_role():
    """Suppression is per-role: skill and judge still all-anthropic keeps
    the advisory alive even with a cross-family shadow on the hook."""
    text = same_family_advisory(
        ["claude-sonnet-4-5", "claude-opus-4-8"],
        hook_model="claude-haiku-4-5",
        hook_shadow_models=["gemini-2.5-flash"])
    assert text is not None and "anthropic" in text


def test_unknown_shadow_is_no_mitigation_and_silences_the_claim():
    """An unclassifiable shadow cannot prove a second family (no
    suppression) — and, joining the pool as an unknown, it silences the
    whole claim: unknown means silent, never a warning."""
    assert same_family_advisory([], hook_model="claude-haiku-4-5",
                                hook_shadow_models=["my-gateway-alias"]) \
        is None


def test_unknown_hook_with_known_shadows_stays_silent():
    """No known hook family means 'introduces a second family' cannot be
    established; the unknown hook then silences the pooled claim."""
    assert same_family_advisory(["claude-sonnet-4-5"],
                                hook_model="my-gateway-hook",
                                hook_shadow_models=["claude-opus-4-8"]) \
        is None


# ---------------------------------------------------------------------------
# DEFAULT_HOOK_MODEL drift guard
# ---------------------------------------------------------------------------

def test_default_hook_model_matches_the_tools_py_literal():
    """tools.py runs copied into workspaces where agent_eval is not
    importable, so it hardcodes its haiku default instead of importing
    DEFAULT_HOOK_MODEL — this source scan is the drift guard between the
    two literals."""
    import re
    source = (REPO_ROOT / "skills" / "eval-run" / "scripts"
              / "tools.py").read_text()
    m = re.search(r'\{"model":\s*model or "([^"]+)"\}', source)
    assert m, "tools.py no longer contains the `model or \"...\"` default"
    assert m.group(1) == DEFAULT_HOOK_MODEL
