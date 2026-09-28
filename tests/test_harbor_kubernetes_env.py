"""The Kubernetes environment under an OpenRouter plan (spec 014, PR-7): the
pod manifest (plan block in ``env[]``, ``secretKeyRef`` for the token, no
literal anywhere, unchanged without a plan) and the exec prefix that never
exports the Secret-delivered token. Needs the Harbor framework and the
kubernetes client, like the other KubernetesEnvironment tests."""

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Probe the exact submodule kubernetes.py needs (see test_harbor_exec_retry.py).
pytest.importorskip("harbor.environments.base")
pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.harbor import k8s_plan  # noqa: E402
from agent_eval.harbor.kubernetes import KubernetesEnvironment  # noqa: E402
from agent_eval.providers.env import settings_env_block  # noqa: E402
from openrouter_fakes import make_plan  # noqa: E402


def _plan_environ(plan, secret="model-keys", key="OPENROUTER_API_KEY",
                  mask="OPENROUTER_API_KEY,OPENROUTER_MANAGEMENT_KEY"):
    return {k8s_plan.PLAN_ENV_VAR: json.dumps(plan.agent_env()),
            k8s_plan.TOKEN_SECRET_VAR: secret, k8s_plan.TOKEN_SECRET_KEY_VAR: key,
            k8s_plan.MASK_VAR: mask}



def _k8s_env(monkeypatch):
    env = object.__new__(KubernetesEnvironment)          # __init__ needs a live cluster
    env.logger = logging.getLogger("test-k8s-plan")
    env._pod, env._namespace = "aeh-test", "team-a"
    # Harbor exposes the resource overrides as read-only properties; shadow
    # them on the class for the test (restored by monkeypatch).
    monkeypatch.setattr(KubernetesEnvironment, "_effective_cpus", 1, raising=False)
    monkeypatch.setattr(KubernetesEnvironment, "_effective_memory_mb", 512, raising=False)
    env.task_env_config = SimpleNamespace(workdir="/workspace", docker_image="example/eval:latest")
    env._skip_pkg_installs = False
    return env


def test_pod_manifest_under_a_plan(monkeypatch):
    plan = make_plan(runner="harbor-k8s")
    monkeypatch.setenv("AGENT_EVAL_K8S_CREDENTIALS_SECRET", "model-keys")
    monkeypatch.setenv("AGENT_EVAL_K8S_GCP_CREDENTIALS_SECRET", "vertex-creds")
    for k, v in _plan_environ(plan).items():
        monkeypatch.setenv(k, v)
    env = _k8s_env(monkeypatch)
    pod_env = k8s_plan.pod_env({"ANTHROPIC_BASE_URL": "https://stale.gateway", "ANTHROPIC_AUTH_TOKEN": "stale"})
    manifest = env._pod_manifest("example/eval:latest", pod_env)
    container = manifest["spec"]["containers"][0]
    entries = {e["name"]: e for e in container["env"]}
    block = settings_env_block(plan, target="k8s_pod")
    assert {k: entries[k]["value"] for k in block} == block         # the plan's block, blanks included
    assert entries["ANTHROPIC_AUTH_TOKEN"] == {
        "name": "ANTHROPIC_AUTH_TOKEN",
        "valueFrom": {"secretKeyRef": {"name": "model-keys", "key": "OPENROUTER_API_KEY"}}}
    # the credentials Secret stays attached for what else it holds, but the provider
    # key names are blanked by explicit entries (env[] wins over envFrom)
    assert container["envFrom"] == [{"secretRef": {"name": "model-keys"}}]
    assert entries["OPENROUTER_API_KEY"] == {"name": "OPENROUTER_API_KEY", "value": ""}
    assert entries["OPENROUTER_MANAGEMENT_KEY"] == {"name": "OPENROUTER_MANAGEMENT_KEY", "value": ""}
    assert len([e for e in container["env"] if e["name"] == "OPENROUTER_API_KEY"]) == 1
    # no Vertex credential mount inside an OpenRouter run
    assert "aeh-creds" not in json.dumps(manifest) and "GOOGLE_APPLICATION_CREDENTIALS" not in entries
    assert "stale" not in json.dumps(manifest) and plan.key not in json.dumps(manifest)
    # an in-container openrouter:/ judge keeps the operator key visible under its name
    monkeypatch.setenv(k8s_plan.MASK_VAR, "OPENROUTER_MANAGEMENT_KEY")
    entries = {e["name"]: e for e in env._pod_manifest("example/eval:latest", pod_env)["spec"]["containers"][0]["env"]}
    assert "OPENROUTER_API_KEY" not in entries and entries["OPENROUTER_MANAGEMENT_KEY"]["value"] == ""


def test_pod_manifest_without_a_plan_is_unchanged(monkeypatch):
    for k in (k8s_plan.PLAN_ENV_VAR, k8s_plan.TOKEN_SECRET_VAR, k8s_plan.TOKEN_SECRET_KEY_VAR, k8s_plan.MASK_VAR):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENT_EVAL_K8S_CREDENTIALS_SECRET", "model-keys")
    monkeypatch.setenv("AGENT_EVAL_K8S_GCP_CREDENTIALS_SECRET", "vertex-creds")
    env = _k8s_env(monkeypatch)
    base = {"ANTHROPIC_BASE_URL": "https://gateway", "ANTHROPIC_MODEL": "m"}
    manifest = env._pod_manifest("example/eval:latest", k8s_plan.pod_env(base))
    container = manifest["spec"]["containers"][0]
    assert container["env"] == [{"name": k, "value": v} for k, v in base.items()] + [
        {"name": "GOOGLE_APPLICATION_CREDENTIALS", "value": "/var/creds/key.json"}]
    assert container["envFrom"] == [{"secretRef": {"name": "model-keys"}}]
    assert manifest["spec"]["volumes"] == [{"name": "aeh-creds", "secret": {"secretName": "vertex-creds"}}]
    assert "valueFrom" not in json.dumps(manifest)


def test_exec_prefix_never_exports_the_secret_delivered_token(monkeypatch):
    env = _k8s_env(monkeypatch)
    seen = []
    env._ws_exec = lambda command, timeout: seen.append(command) or SimpleNamespace(stdout="", stderr="", return_code=0)
    monkeypatch.setenv(k8s_plan.TOKEN_SECRET_VAR, "model-keys")
    asyncio.run(env.exec("claude --print", env={"ANTHROPIC_AUTH_TOKEN": "sk-or-secret", "ANTHROPIC_BASE_URL": "https://x"}))
    assert "sk-or-secret" not in seen[-1] and "ANTHROPIC_AUTH_TOKEN" not in seen[-1]
    assert "export ANTHROPIC_BASE_URL=https://x; " in seen[-1]        # the other keys still travel
    monkeypatch.delenv(k8s_plan.TOKEN_SECRET_VAR)
    asyncio.run(env.exec("claude --print", env={"ANTHROPIC_AUTH_TOKEN": "tok"}))
    assert "export ANTHROPIC_AUTH_TOKEN=tok; " in seen[-1]         # no plan: today's behaviour
