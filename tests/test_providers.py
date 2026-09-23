"""Provider core (spec 014 PR-3b): model ids, the Direct transport env
template, and the agent plan. Nothing here talks to a network."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.providers import (  # noqa: E402
    AgentModel, ConfigError, ErrorClass, ProviderKind, ProviderPlan, parse_agent_model, routing_key)
from agent_eval.providers.env import (  # noqa: E402
    BLANKED_KEYS, DYNAMIC_MANAGED_KEYS, MANAGED_ENV_KEYS, TARGETS, custom_headers, settings_env_block)
from agent_eval.providers.openrouter.plan import (  # noqa: E402
    build_plan, effective_roles, hook_model_for, plan_is_active)
from agent_eval.providers.openrouter.routing import RoutingTable  # noqa: E402


# --- model ids ---------------------------------------------------------------

@pytest.mark.parametrize("uri, provider, id_, slug, variants, suffix, key", [
    ("openrouter:/z-ai/glm-5.2:exacto", "openrouter", "z-ai/glm-5.2:exacto", "z-ai/glm-5.2", ("exacto",), "", "z-ai/glm-5.2"),
    ("openrouter:/z-ai/glm-5.2:exacto:nitro", "openrouter", "z-ai/glm-5.2:exacto:nitro", "z-ai/glm-5.2", ("exacto", "nitro"), "", "z-ai/glm-5.2"),
    ("openrouter:/anthropic/claude-opus-4-8[1m]", "openrouter", "anthropic/claude-opus-4-8[1m]", "anthropic/claude-opus-4-8", (), "[1m]", "anthropic/claude-opus-4-8"),
    ("openrouter:/openrouter/auto", "openrouter", "openrouter/auto", "openrouter/auto", (), "", "openrouter/auto"),
    ("OpenRouter://Z-AI/GLM-5.2", "openrouter", "Z-AI/GLM-5.2", "Z-AI/GLM-5.2", (), "", "z-ai/glm-5.2"),
    ("anthropic:/claude-opus-4-8", "anthropic", "claude-opus-4-8", "claude-opus-4-8", (), "", "claude-opus-4-8"),
    ("sonnet", None, "sonnet", "sonnet", (), "", "sonnet"),
    ("opus[1m]", None, "opus[1m]", "opus", (), "[1m]", "opus"),
])
def test_parse_agent_model(uri, provider, id_, slug, variants, suffix, key):
    m = parse_agent_model(uri)
    assert m == AgentModel(provider=provider, id=id_, slug=slug, variants=variants, suffix=suffix)
    assert m.key == key
    assert m.uri == (f"{provider}:/{id_}" if provider else id_)


@pytest.mark.parametrize("uri, match", [
    ("openrouter:/", "model id missing"),
    ("openrouter://", "model id missing"),
    ("", "model id missing"),
    (None, "model id missing"),
    ("openrouter:/glm-5.2", "<author>/<slug>"),
    ("openrouter:/z-ai/", "<author>/<slug>"),
    ("openrouter://glm", "<author>/<slug>"),
    ("openrouter:/z-ai//glm", "<author>/<slug>"),
    ("openrouter:/z-ai/glm 5.2", "unparseable"),
])
def test_parse_agent_model_rejections(uri, match):
    with pytest.raises(ValueError, match=match):
        parse_agent_model(uri)


def test_routing_key_strips_suffix_and_variants():
    assert routing_key("Z-AI/glm-5.2[1m]:exacto") == "z-ai/glm-5.2"
    assert routing_key("anthropic/claude-opus-4-8[1m]") == "anthropic/claude-opus-4-8"
    assert routing_key(None) == ""


def test_enums_and_error_class():
    assert ProviderKind.OPENROUTER == "openrouter"
    assert {e.value for e in ErrorClass} == {"config", "infra", "provider", "agent"}
    assert issubclass(ConfigError, ValueError)


# --- env template --------------------------------------------------------------

def _plan(**over):
    base = dict(
        kind="openrouter", base_url="https://openrouter.ai/api", key_scope="operator",
        key="sk-test-secret", key_env="OPENROUTER_API_KEY",
        skill=parse_agent_model("openrouter:/z-ai/glm-5.2:exacto"),
        subagent=parse_agent_model("openrouter:/z-ai/glm-5.2"),
        hook=None, background_model=None, routing=RoutingTable(), enforcement="audit",
        run_id="2026-09-22-glm", runner="claude-code",
        attribution=SimpleNamespace(referer=None, title="agent-eval-harness", run_id_header=False),
        cli_budget_inflation=50, budget_run_usd=None)
    base.update(over)
    return ProviderPlan(**base)


def test_env_block_overlay_target_is_the_direct_transport_template():
    block = settings_env_block(_plan())
    assert block["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"      # no /v1
    assert block["ANTHROPIC_AUTH_TOKEN"] == "sk-test-secret"
    assert block["ANTHROPIC_API_KEY"] == ""
    for key in BLANKED_KEYS:
        assert block[key] == ""                                              # blank, present
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        assert block[key] == "z-ai/glm-5.2:exacto"                          # :variant preserved
    assert block["CLAUDE_CODE_SUBAGENT_MODEL"] == "z-ai/glm-5.2"
    assert block["ANTHROPIC_CUSTOM_HEADERS"] == "X-OpenRouter-Title: agent-eval-harness"
    assert block["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in block
    assert set(block) <= MANAGED_ENV_KEYS


def test_env_block_targets_differ_only_in_how_the_key_travels():
    plan = _plan()
    overlay = settings_env_block(plan, target="overlay")
    carrier = settings_env_block(plan, target="harbor_carrier")
    pod = settings_env_block(plan, target="k8s_pod")
    assert overlay["ANTHROPIC_AUTH_TOKEN"] == "sk-test-secret"
    assert carrier["ANTHROPIC_AUTH_TOKEN"] == "$OPENROUTER_API_KEY"          # a reference, never the value
    assert "ANTHROPIC_AUTH_TOKEN" not in pod                                 # secretKeyRef supplies it
    assert set(overlay) == set(carrier) == set(pod) | {"ANTHROPIC_AUTH_TOKEN"}
    for key in pod:
        assert overlay[key] == carrier[key] == pod[key]
    assert "sk-test-secret" not in repr(carrier) and "sk-test-secret" not in repr(pod)


def test_env_block_secret_modes_override_the_target_default():
    plan = _plan()
    assert settings_env_block(plan, secrets="ref")["ANTHROPIC_AUTH_TOKEN"] == "$OPENROUTER_API_KEY"
    assert "ANTHROPIC_AUTH_TOKEN" not in settings_env_block(plan, secrets="omit")
    with pytest.raises(ValueError, match="no inference key"):
        settings_env_block(_plan(key=None), secrets="literal")
    with pytest.raises(ValueError, match="unknown env target"):
        settings_env_block(plan, target="sidecar")
    with pytest.raises(ValueError, match="unknown secrets mode"):
        settings_env_block(plan, secrets="plain")


def test_alias_rule_haiku_slot_defaults_to_the_skill_model():
    default = settings_env_block(_plan(background_model=None))
    assert default["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "z-ai/glm-5.2:exacto"
    cheap = settings_env_block(_plan(background_model="qwen/qwen3-8b"))
    assert cheap["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "qwen/qwen3-8b"
    assert cheap["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "z-ai/glm-5.2:exacto"


def test_custom_headers_are_one_per_line_and_optional():
    attribution = SimpleNamespace(referer="https://example.test/eval", title="my-eval",
                                  run_id_header=True)
    assert custom_headers(attribution, "run-1") == (
        "HTTP-Referer: https://example.test/eval\nX-OpenRouter-Title: my-eval\nx-eval-run-id: run-1")
    assert custom_headers(SimpleNamespace(referer=None, title=None, run_id_header=False), "r") is None
    # The run tag stands on its own when attribution is otherwise empty.
    assert custom_headers(SimpleNamespace(referer=None, title=None, run_id_header=True), "r") == "x-eval-run-id: r"
    assert custom_headers(SimpleNamespace(referer=None, title=None, run_id_header=True), None) is None
    block = settings_env_block(_plan(attribution=SimpleNamespace(referer=None, title=None,
                                                                 run_id_header=False)))
    assert "ANTHROPIC_CUSTOM_HEADERS" not in block


def test_managed_env_keys_is_the_union_of_everything_the_template_emits():
    emitted = set()
    for target in TARGETS:
        emitted |= set(settings_env_block(
            _plan(attribution=SimpleNamespace(referer="r", title="t", run_id_header=True)),
            target=target))
    assert emitted == MANAGED_ENV_KEYS
    assert DYNAMIC_MANAGED_KEYS <= MANAGED_ENV_KEYS


def test_plan_repr_hides_the_key():
    plan = _plan()
    text = repr(plan)
    assert "sk-test-secret" not in text
    assert plan.key_hash in text and len(plan.key_hash) == 8
    assert _plan(key=None).key_hash is None
    assert plan.close() is None
    assert plan.agent_env()["ANTHROPIC_AUTH_TOKEN"] == "sk-test-secret"
    assert plan.target == "overlay"


# --- build_plan ----------------------------------------------------------------

def _config(tmp_path, models):
    path = tmp_path / "eval.yaml"
    path.write_text("name: t\nexecution:\n  skill: s\nmodels:\n" + models
                    + "judges:\n  - {name: j, check: 'return True'}\n")
    return EvalConfig.from_yaml(path)


_ROUTED = """  skill: openrouter:/z-ai/glm-5.2:exacto
  providers:
    openrouter:
      attribution: {referer: https://example.test, title: t, run_id_header: true}
      background_model: qwen/qwen3-8b
      budget: {run_usd: 7}
      routing:
        models:
          z-ai/glm-5.2: {order: [z-ai], allow_fallbacks: false}
"""


def test_build_plan_reads_the_operator_key_and_the_block(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    config = _config(tmp_path, _ROUTED)
    plan = build_plan(config, runner="harbor-podman", run_id="run-7")
    assert plan.kind == "openrouter" and plan.transport == "direct"
    assert plan.key == "sk-or-test" and plan.key_scope == "operator"
    assert plan.key_env == "OPENROUTER_API_KEY"
    assert plan.skill.id == "z-ai/glm-5.2:exacto"
    assert plan.subagent.id == "z-ai/glm-5.2:exacto"      # defaults to the skill model
    assert plan.hook is None
    assert plan.background_model == "qwen/qwen3-8b"
    assert plan.budget_run_usd == 7 and plan.cli_budget_inflation == 50
    assert plan.routing.for_model("z-ai/glm-5.2").order == ("z-ai",)
    assert plan.target == "harbor_carrier"
    env = plan.agent_env()
    assert env["ANTHROPIC_AUTH_TOKEN"] == "$OPENROUTER_API_KEY"
    assert env["ANTHROPIC_CUSTOM_HEADERS"].endswith("x-eval-run-id: run-7")
    assert "sk-or-test" not in repr(plan)


def test_build_plan_without_a_block_uses_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    plan = build_plan(_config(tmp_path, "  skill: openrouter:/z-ai/glm-5.2\n  subagent: openrouter:/z-ai/glm-5.2:exacto\n"))
    assert plan.base_url == "https://openrouter.ai/api"
    assert plan.enforcement == "audit" and plan.budget_run_usd is None
    assert plan.subagent.id == "z-ai/glm-5.2:exacto"
    assert plan.agent_env()["ANTHROPIC_CUSTOM_HEADERS"] == "X-OpenRouter-Title: agent-eval-harness"


def test_build_plan_requires_the_key_variable_without_echoing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "leaked-if-used")
    config = _config(tmp_path, _ROUTED)
    with pytest.raises(ConfigError, match="set OPENROUTER_API_KEY") as exc:
        build_plan(config)
    assert "leaked-if-used" not in str(exc.value)
    keyless = build_plan(config, require_key=False)
    assert keyless.key is None
    assert "ANTHROPIC_AUTH_TOKEN" not in keyless.agent_env(secrets="omit")


def test_build_plan_rejects_non_openrouter_roles_and_unknown_runners(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    config = _config(tmp_path, "  skill: sonnet\n")
    with pytest.raises(ConfigError, match="no OpenRouter plan for skill model"):
        build_plan(config)
    routed = _config(tmp_path, _ROUTED)
    with pytest.raises(ConfigError, match="unknown plan runner"):
        build_plan(routed, runner="lambda")
    with pytest.raises(ConfigError, match="must share the plan's provider kind"):
        build_plan(routed, roles={"skill": "openrouter:/z-ai/glm-5.2", "subagent": "sonnet", "hook": None})


def test_build_plan_key_guardrail_is_not_available_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    config = _config(tmp_path, _ROUTED.replace("        models:\n", "        enforcement: key-guardrail\n        models:\n"))
    assert config.models.providers.openrouter.routing.enforcement == "key-guardrail"
    with pytest.raises(ConfigError, match="key-guardrail .* later release"):
        build_plan(config)
    assert build_plan(config, require_key=False).enforcement == "key-guardrail"


def test_effective_roles_and_activation(tmp_path):
    config = _config(tmp_path, "  skill: sonnet\n  subagent: haiku\n")
    assert effective_roles(config) == {"skill": "sonnet", "subagent": "haiku", "hook": None}
    assert plan_is_active(effective_roles(config)) is False
    overridden = effective_roles(config, {"skill": "openrouter:/z-ai/glm-5.2"})
    assert overridden["skill"] == "openrouter:/z-ai/glm-5.2"
    assert plan_is_active(overridden) is True
    assert plan_is_active({"skill": "openrouter:/"}) is False
    assert plan_is_active({"skill": None}) is False


# --- workspace env injection -------------------------------------------------------

def test_inject_env_skips_yaml_nulls(monkeypatch):
    import workspace

    monkeypatch.setenv("SET_VAR", "value")
    config = SimpleNamespace(execution=SimpleNamespace(env={
        "LITERAL": "x", "NULL": None, "REF": "$SET_VAR", "MISSING": "$UNSET_VAR_XYZ", "NUM": 3}))
    settings = {}
    workspace._inject_env(settings, config)
    assert settings["env"] == {"LITERAL": "x", "REF": "value", "NUM": "3"}
    assert "NULL" not in settings["env"]


def test_custom_headers_reject_crlf_values():
    with pytest.raises(ValueError, match="single lines"):
        custom_headers(SimpleNamespace(referer=None, title="x\nAuthorization: Bearer y",
                                       run_id_header=False), None)
    with pytest.raises(ValueError, match="single lines"):
        custom_headers(SimpleNamespace(referer=None, title="t", run_id_header=True), "r\r\nX: y")


def test_hook_model_defaults_to_the_plan_model_under_an_active_plan(tmp_path):
    routed = _config(tmp_path, "  skill: openrouter:/z-ai/glm-5.2:exacto\n")
    assert hook_model_for(routed) == "z-ai/glm-5.2:exacto"
    cheap = _config(tmp_path, _ROUTED)                       # declares background_model
    assert hook_model_for(cheap) == "qwen/qwen3-8b"
    explicit = _config(tmp_path, "  skill: openrouter:/z-ai/glm-5.2\n  hook: openrouter:/z-ai/glm-5.2:nitro\n")
    assert hook_model_for(explicit) == "openrouter:/z-ai/glm-5.2:nitro"
    anthropic = _config(tmp_path, "  skill: sonnet\n")
    assert hook_model_for(anthropic) is None                # caller keeps its built-in default
    assert hook_model_for(anthropic, {"skill": "openrouter:/z-ai/glm-5.2"}) == "z-ai/glm-5.2"


def test_interception_handlers_carry_the_plan_hook_model(tmp_path):
    from agent_eval.tools.interception import build_handlers

    handler_data, _ = build_handlers(_config(tmp_path, _ROUTED))
    assert handler_data["hook_model"] == "qwen/qwen3-8b"
    handler_data, _ = build_handlers(_config(tmp_path, "  skill: sonnet\n"))
    assert "hook_model" not in handler_data
    handler_data, _ = build_handlers(_config(tmp_path, "  skill: sonnet\n  hook: claude-haiku-4-5\n"))
    assert handler_data["hook_model"] == "claude-haiku-4-5"

