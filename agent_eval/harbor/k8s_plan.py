"""How an OpenRouter plan reaches a Kubernetes trial pod (spec 014, PR-7).

The Harbor environment class runs inside the ``harbor`` child process and has
no plan object; ``harbor/run.py`` hands it what the pod spec needs through the
child environment, the same channel podman uses:

- ``AGENT_EVAL_K8S_PLAN_ENV`` — JSON of the plan's **non-secret** env block
  (``settings_env_block(plan, target="k8s_pod")``: base URL, blank
  Vertex/Bedrock lines, the model aliases, ``ANTHROPIC_API_KEY=""``). It lands
  in the container ``env[]`` and wins over the credentials Secret's
  ``envFrom``, so a stale ``ANTHROPIC_BASE_URL`` there cannot re-route.
- ``AGENT_EVAL_K8S_TOKEN_SECRET`` / ``AGENT_EVAL_K8S_TOKEN_SECRET_KEY`` — the
  Secret (and the key inside it) that ``ANTHROPIC_AUTH_TOKEN`` is mapped from
  via ``valueFrom.secretKeyRef``: the operator's credentials Secret at
  ``audit``, the per-run Secret ``agent-eval-<run_id>-openrouter`` at
  ``key-guardrail``. The key value itself never enters the manifest, the exec
  prefix or a log.
- ``AGENT_EVAL_K8S_PLAN_MASK`` — key names the credentials Secret may hold
  that the agent must not see (the operator's inference key at
  ``key-guardrail``, the management key always): each becomes an explicit
  empty ``env[]`` entry, which wins over ``envFrom``. The inference key stays
  visible under its configured name only when an in-container ``openrouter:/``
  judge needs it — the same rule podman applies to its forwarding.

Pure functions, no ``harbor``/``kubernetes`` import, so they are unit-tested
in every environment.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

PLAN_ENV_VAR = "AGENT_EVAL_K8S_PLAN_ENV"
TOKEN_SECRET_VAR = "AGENT_EVAL_K8S_TOKEN_SECRET"
TOKEN_SECRET_KEY_VAR = "AGENT_EVAL_K8S_TOKEN_SECRET_KEY"
# Names the credentials Secret's envFrom must not expose to the agent while a
# plan is active (the operator's OpenRouter key, the management key): masked
# with an explicit empty env[] entry, which wins over envFrom.
MASK_VAR = "AGENT_EVAL_K8S_PLAN_MASK"
TOKEN_ENV_NAME = "ANTHROPIC_AUTH_TOKEN"
DEFAULT_SECRET_KEY = "OPENROUTER_API_KEY"


def plan_env_from_environ(environ=None) -> dict:
    """The plan's non-secret block handed over by ``harbor/run.py`` (``{}``
    without a plan). Only string values are accepted."""
    raw = (environ if environ is not None else os.environ).get(PLAN_ENV_VAR)
    if not raw:
        return {}
    try:
        block = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(block, dict):
        return {}
    return {str(k): str(v) for k, v in block.items() if v is not None}


def token_secret_ref(environ=None) -> Optional[dict]:
    """The ``env[]`` entry mapping ``ANTHROPIC_AUTH_TOKEN`` from the Secret
    named in the environment, or ``None`` when no plan is active."""
    env = environ if environ is not None else os.environ
    name = env.get(TOKEN_SECRET_VAR)
    if not name:
        return None
    key = env.get(TOKEN_SECRET_KEY_VAR) or DEFAULT_SECRET_KEY
    return {"name": TOKEN_ENV_NAME, "valueFrom": {"secretKeyRef": {"name": name, "key": key}}}


def pod_env(base: dict, environ=None) -> dict:
    """``base`` (Harbor's persistent env + the forwarded host hints) with the
    plan's block merged **last** — the plan wins for the managed keys."""
    merged = dict(base or {})
    merged.update(plan_env_from_environ(environ))
    if token_secret_ref(environ) is not None:
        merged.pop(TOKEN_ENV_NAME, None)          # the Secret supplies it; never a plain value
    return merged


def masked_names(environ=None) -> list:
    """Names to blank in the pod ``env[]`` (``AGENT_EVAL_K8S_PLAN_MASK``)."""
    raw = (environ if environ is not None else os.environ).get(MASK_VAR, "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def exec_env_skips(key: str, environ=None) -> bool:
    """Whether ``exec()`` must not ``export`` ``key``: the token is delivered
    by the Secret, never inlined into a command line or a log."""
    return key == TOKEN_ENV_NAME and bool((environ if environ is not None else os.environ).get(TOKEN_SECRET_VAR))


def per_run_secret_name(run_id: str) -> str:
    """``agent-eval-<run_id>-openrouter`` as an RFC 1123 subdomain name."""
    core = re.sub(r"[^a-z0-9-]", "-", (run_id or "run").lower())
    core = re.sub(r"-+", "-", core).strip("-") or "run"
    name = f"agent-eval-{core}-openrouter"
    if len(name) > 253:
        name = f"agent-eval-{core[:253 - len('agent-eval--openrouter')]}-openrouter"
    return name.strip("-")
