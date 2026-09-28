"""The Kubernetes leg of the direct OpenRouter transport (spec 014, PR-7).

The plan reaches the pod through the child environment: the non-secret block
lands in the container ``env[]`` (winning over the credentials Secret's
``envFrom``) and ``ANTHROPIC_AUTH_TOKEN`` is mapped from a Secret through
``secretKeyRef`` — never a literal in the manifest, never an ``export`` in the
exec prefix. These are the pure helpers, tested everywhere; the manifest and
exec tests (which need the Harbor framework) live in
``test_harbor_kubernetes_env.py``."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.harbor import k8s_plan  # noqa: E402
from agent_eval.providers.env import MANAGED_ENV_KEYS  # noqa: E402
from openrouter_fakes import make_plan  # noqa: E402


def _plan_environ(plan, secret="model-keys", key="OPENROUTER_API_KEY"):
    return {k8s_plan.PLAN_ENV_VAR: json.dumps(plan.agent_env()),
            k8s_plan.TOKEN_SECRET_VAR: secret, k8s_plan.TOKEN_SECRET_KEY_VAR: key}


def test_plan_env_and_token_ref_from_the_child_environment():
    plan = make_plan(runner="harbor-k8s")
    environ = _plan_environ(plan)
    block = k8s_plan.plan_env_from_environ(environ)
    assert block == plan.agent_env() and "ANTHROPIC_AUTH_TOKEN" not in block
    assert set(block) == MANAGED_ENV_KEYS - {"ANTHROPIC_AUTH_TOKEN"}
    assert k8s_plan.token_secret_ref(environ) == {
        "name": "ANTHROPIC_AUTH_TOKEN",
        "valueFrom": {"secretKeyRef": {"name": "model-keys", "key": "OPENROUTER_API_KEY"}}}
    assert k8s_plan.token_secret_ref({k8s_plan.TOKEN_SECRET_VAR: "s"})["valueFrom"]["secretKeyRef"]["key"] == "OPENROUTER_API_KEY"
    # no plan: nothing
    assert k8s_plan.plan_env_from_environ({}) == {} and k8s_plan.token_secret_ref({}) is None
    assert k8s_plan.plan_env_from_environ({k8s_plan.PLAN_ENV_VAR: "not json"}) == {}
    assert k8s_plan.plan_env_from_environ({k8s_plan.PLAN_ENV_VAR: "[1]"}) == {}


def test_pod_env_lets_the_plan_win_and_drops_a_plain_token():
    plan = make_plan(runner="harbor-k8s")
    base = {"ANTHROPIC_BASE_URL": "https://stale.gateway", "ANTHROPIC_AUTH_TOKEN": "stale-token",
            "TASK_FLAG": "enabled"}
    merged = k8s_plan.pod_env(base, _plan_environ(plan))
    assert merged["ANTHROPIC_BASE_URL"] == plan.base_url and merged["TASK_FLAG"] == "enabled"
    assert "ANTHROPIC_AUTH_TOKEN" not in merged                   # the Secret supplies it
    assert k8s_plan.pod_env(base, {}) == base                     # no plan: untouched


def test_masked_names_come_from_the_child_environment():
    assert k8s_plan.masked_names({k8s_plan.MASK_VAR: "OPENROUTER_API_KEY, OPENROUTER_MANAGEMENT_KEY"}) == [
        "OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_KEY"]
    assert k8s_plan.masked_names({}) == []


def test_exec_env_skips_only_the_secret_delivered_token():
    assert k8s_plan.exec_env_skips("ANTHROPIC_AUTH_TOKEN", {k8s_plan.TOKEN_SECRET_VAR: "s"})
    assert not k8s_plan.exec_env_skips("ANTHROPIC_BASE_URL", {k8s_plan.TOKEN_SECRET_VAR: "s"})
    assert not k8s_plan.exec_env_skips("ANTHROPIC_AUTH_TOKEN", {})


def test_per_run_secret_name_is_a_dns_subdomain():
    assert k8s_plan.per_run_secret_name("2026-09-28-glm") == "agent-eval-2026-09-28-glm-openrouter"
    assert k8s_plan.per_run_secret_name("Run_X/y") == "agent-eval-run-x-y-openrouter"
    assert k8s_plan.per_run_secret_name("") == "agent-eval-run-openrouter"
    assert len(k8s_plan.per_run_secret_name("a" * 400)) <= 253
