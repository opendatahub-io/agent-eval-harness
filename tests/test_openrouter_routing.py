"""OpenRouter routing declarations (spec 014): RoutingSpec / RoutingTable and
the Decision 25 per-role rule behind a judge's ``extra_body``."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.providers import JudgeProviderError, routing_key  # noqa: E402
from agent_eval.providers.openrouter import (  # noqa: E402
    RoutingSpec,
    RoutingTable,
    judge_extra_body,
    normalize_provider,
    routing_sha,
)

SLUG = "z-ai/glm-5.2"
TABLE = RoutingTable.from_dict({
    "defaults": {"allow_fallbacks": True, "sort": "throughput"},
    "models": {SLUG: {"order": ["Z.AI", "novita"], "allow_fallbacks": False,
                      "quantizations": ["fp8"]}},
})


@pytest.mark.parametrize("model, expected", [
    ("z-ai/glm-5.2", "z-ai/glm-5.2"),
    ("z-ai/glm-5.2:exacto", "z-ai/glm-5.2"),
    ("Z-AI/GLM-5.2:nitro", "z-ai/glm-5.2"),
    ("  openai/gpt-5.2 ", "openai/gpt-5.2"),
    ("", ""),
    (None, ""),
])
def test_routing_key_strips_variant_and_case(model, expected):
    assert routing_key(model) == expected


@pytest.mark.parametrize("name, expected", [
    ("Z.AI", "z-ai"), ("novita", "novita"), ("Amazon Bedrock", "amazon-bedrock"),
    ("StreamLake", "streamlake"), (" Together ", "together"),
])
def test_normalize_provider(name, expected):
    assert normalize_provider(name) == expected


class TestRoutingSpecParsing:

    def test_empty_and_none(self):
        assert RoutingSpec.from_dict(None).is_empty
        assert RoutingSpec.from_dict({}).is_empty
        assert RoutingSpec.from_dict({"order": None}).is_empty

    def test_provider_lists_are_normalised(self):
        spec = RoutingSpec.from_dict({"order": ["Z.AI", "Novita"], "only": "z-ai",
                                      "ignore": ["Amazon Bedrock"]})
        assert spec.order == ("z-ai", "novita")
        assert spec.only == ("z-ai",)
        assert spec.ignore == ("amazon-bedrock",)

    @pytest.mark.parametrize("raw, match", [
        ({"bogus": 1}, "unknown key"),
        ({"quantizations": ["q4"]}, "quantization"),
        ({"fallbacks": ["a/b", "c/d", "e/f", "g/h"]}, "at most 3"),
        ({"allow_fallbacks": "no"}, "boolean"),
        ({"require_parameters": 1}, "boolean"),
        ({"sort": "cheapest"}, "sort"),
        ({"sort": {"by": "price", "partition": "x"}}, "partition"),
        ({"data_collection": "maybe"}, "data_collection"),
        ({"max_price": {"prompt": -1}}, "non-negative"),
        ({"max_price": {"tokens": 1}}, "unknown key"),
        ({"order": []}, "non-empty list"),
        ({"order": [1]}, "list of strings"),
        ("z-ai", "must be a mapping"),
    ])
    def test_rejects_bad_values(self, raw, match):
        with pytest.raises(ValueError, match=match):
            RoutingSpec.from_dict(raw, context="routing")

    def test_context_names_the_offending_key(self):
        with pytest.raises(ValueError, match=r"models\.providers\.openrouter\.routing\.defaults\.sort"):
            RoutingSpec.from_dict({"sort": "x"},
                                  context="models.providers.openrouter.routing.defaults")

    def test_sort_mapping_form_and_max_price(self):
        spec = RoutingSpec.from_dict({"sort": {"by": "latency", "partition": "none"},
                                      "max_price": {"prompt": 1, "completion": 2.5}})
        assert spec.sort == {"by": "latency", "partition": "none"}
        assert spec.max_price == {"prompt": 1, "completion": 2.5}


class TestRoutingComposition:

    def test_merged_later_wins_and_lists_replace(self):
        base = RoutingSpec.from_dict({"order": ["a", "b"], "sort": "price",
                                      "allow_fallbacks": True})
        over = RoutingSpec.from_dict({"order": ["c"], "allow_fallbacks": False})
        merged = base.merged(over)
        assert merged.order == ("c",)
        assert merged.allow_fallbacks is False
        assert merged.sort == "price"
        assert base.merged(None) == base

    def test_table_for_model_merges_defaults_and_entry_across_variants(self):
        spec = TABLE.for_model("z-ai/glm-5.2:exacto")
        assert spec.order == ("z-ai", "novita")
        assert spec.allow_fallbacks is False
        assert spec.quantizations == ("fp8",)
        assert spec.sort == "throughput"
        assert TABLE.has_entry("Z-AI/glm-5.2:nitro") and not TABLE.has_entry("openai/gpt-5.2")
        assert TABLE.for_model("openai/gpt-5.2") == TABLE.defaults

    def test_table_rejects_bad_keys_and_duplicates(self):
        with pytest.raises(ValueError, match="<author>/<slug>"):
            RoutingTable.from_dict({"models": {"glm": {}}})
        with pytest.raises(ValueError, match="twice"):
            RoutingTable.from_dict({"models": {SLUG: {}, f"{SLUG}:exacto": {}}})
        with pytest.raises(ValueError, match="unknown key"):
            RoutingTable.from_dict({"policy": "strict"})

    def test_routing_sha_is_stable_across_spellings(self):
        a = RoutingSpec.from_dict({"order": ["Z.AI", "novita"]})
        b = RoutingSpec.from_dict({"order": ["z-ai", "Novita"]})
        assert routing_sha(a) == routing_sha(b)
        assert routing_sha(a) != routing_sha(RoutingSpec.from_dict({"order": ["novita"]}))
        assert routing_sha({"provider": {"sort": "price"}}) == routing_sha({"provider": {"sort": "price"}})


class TestJudgeExtraBody:
    """Decision 25: judge pins are opt-in; unpinned judges send only the
    non-binding keys and never `require_parameters`."""

    def test_unpinned_judge_sends_only_non_binding_keys(self):
        assert judge_extra_body(TABLE, SLUG) == {"provider": {"sort": "throughput"}}
        assert judge_extra_body(TABLE, f"{SLUG}:exacto") == {"provider": {"sort": "throughput"}}

    def test_inherit_pins_sends_pins_and_require_parameters(self):
        body = judge_extra_body(TABLE, SLUG, inherit_pins=True)
        assert body == {"provider": {
            "order": ["z-ai", "novita"], "allow_fallbacks": False,
            "quantizations": ["fp8"], "sort": "throughput",
            "require_parameters": True}}

    def test_inherit_pins_without_an_entry_stays_unpinned(self):
        body = judge_extra_body(TABLE, "openai/gpt-5.2", inherit_pins=True)
        assert body == {"provider": {"sort": "throughput"}}

    def test_per_judge_routing_override_opts_into_pins(self):
        body = judge_extra_body(TABLE, SLUG, provider_options={"routing": {"order": ["novita"]}})
        provider = body["provider"]
        assert provider["order"] == ["novita"]            # override replaces the list
        assert provider["allow_fallbacks"] is False       # table entry still applies
        assert provider["require_parameters"] is True

    def test_fallbacks_only_never_implies_pins(self):
        body = judge_extra_body(TABLE, SLUG, provider_options={"fallbacks": ["deepseek/deepseek-v4"]})
        assert body == {"provider": {"sort": "throughput"},
                        "models": [SLUG, "deepseek/deepseek-v4"]}

    def test_unpinned_judge_never_sends_require_parameters(self):
        table = RoutingTable.from_dict({"defaults": {"require_parameters": True,
                                                     "allow_fallbacks": True}})
        assert judge_extra_body(table, SLUG) == {}
        assert judge_extra_body(table, SLUG, inherit_pins=True) == {}

    def test_explicit_require_parameters_false_is_respected_when_pinned(self):
        table = RoutingTable.from_dict({"models": {SLUG: {"order": ["z-ai"],
                                                          "require_parameters": False}}})
        body = judge_extra_body(table, SLUG, inherit_pins=True)
        assert body["provider"] == {"order": ["z-ai"], "require_parameters": False}

    def test_empty_table_yields_empty_body(self):
        assert judge_extra_body(None, "openai/gpt-5.2") == {}
        assert judge_extra_body(RoutingTable(), "openai/gpt-5.2", inherit_pins=True) == {}

    def test_non_judge_role_sends_the_declaration_as_is(self):
        body = TABLE.for_model(SLUG).to_chat_extra_body(SLUG, role="agent")
        assert body["provider"]["order"] == ["z-ai", "novita"]
        assert body["provider"]["require_parameters"] is True

    def test_invalid_override_is_rejected(self):
        with pytest.raises(ValueError, match="overrides"):
            judge_extra_body(TABLE, SLUG, provider_options={"routing": {"nope": 1}})


def test_judge_provider_error_carries_classification():
    err = JudgeProviderError("boom", error_type="provider_overloaded",
                             retryable=True, provider="Novita", tool_choice_mode="auto")
    assert (err.error_class, err.retryable, err.provider, err.tool_choice_mode) == (
        "provider", True, "Novita", "auto")
    cfg = JudgeProviderError("no endpoints", error_type="no_endpoints", error_class="config")
    assert cfg.error_class == "config" and cfg.retryable is False
    with pytest.raises(ValueError):
        JudgeProviderError("x", error_class="mystery")
