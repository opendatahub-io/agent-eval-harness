"""The key guardrail (spec 014, PR-6) against the loopback fake management API:
provisioning with limit + allow-list, the read-back that fails closed, the
per-run key on every env target, revocation on every exit path, key.json and
the offline revoke command. No OpenRouter key."""

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.providers.base import ConfigError  # noqa: E402
from agent_eval.providers.env import settings_env_block  # noqa: E402
from agent_eval.providers.openrouter import keys as K  # noqa: E402
from agent_eval.providers.openrouter import plan as plan_mod  # noqa: E402
from agent_eval.providers.openrouter.session import ProviderSession  # noqa: E402
from openrouter_fakes import FAKE_KEY, FAKE_MGMT_KEY, FakeOpenRouter, quiet_atexit  # noqa: E402


def _config(tmp_path, base_url, **over):
    orc = {"base_url": base_url, "budget": {"run_usd": 0.25},
           "routing": {"enforcement": "key-guardrail",
                       "models": {"z-ai/glm-5.2": {"order": ["novita"], "allow_fallbacks": False}}}}
    orc.update(over)
    p = tmp_path / "eval.yaml"
    p.write_text(yaml.safe_dump({
        "name": "t", "execution": {"skill": "s"}, "judges": [{"name": "j", "check": "return True"}],
        "models": {"skill": "openrouter:/z-ai/glm-5.2:exacto", "providers": {"openrouter": orc}}}))
    return EvalConfig.from_yaml(p)


class FastSession(ProviderSession):
    def __init__(self, *a, **kw):
        kw.update(first_poll_s=0.0, poll_s=0.01, give_up_s=0.3, settle_s=0.0, key_poll_s=0.0, key_max_s=2.0)
        super().__init__(*a, **kw)


@pytest.fixture
def fake(monkeypatch):
    srv = FakeOpenRouter()
    srv.base_url = srv.start()
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", FAKE_MGMT_KEY)
    srv.atexit = quiet_atexit(monkeypatch)
    yield srv
    srv.stop()


def test_key_request_is_the_one_place_the_fields_live():
    body = K.key_request(name="agent-eval r1", limit_usd=0.25, allowed_providers=["Z.AI", "novita", "novita"])
    assert body == {"name": "agent-eval r1", "limit": 0.25, "allowed_providers": ["novita", "z-ai"]}


def test_build_plan_provisions_a_per_run_key(fake, tmp_path):
    plan = plan_mod.build_plan(_config(tmp_path, fake.base_url), run_id="r1")
    pk = plan.provisioned
    assert plan.key_scope == "per-run" and plan.key == pk.key and plan.key != FAKE_KEY
    assert pk.hash in fake.issued and fake.issued[pk.hash]["key"] == plan.key
    assert fake.key_requests == [{"name": "agent-eval r1", "limit": 0.25, "allowed_providers": ["novita"]}]
    assert "sk-or-run" not in repr(pk) and "sk-or-run" not in repr(plan)
    assert plan.key_hash == hashlib.sha256(plan.key.encode()).hexdigest()[:8]
    # every env target carries the per-run key, never a host variable reference
    assert settings_env_block(plan, target="overlay")["ANTHROPIC_AUTH_TOKEN"] == plan.key
    assert settings_env_block(plan, target="harbor_carrier")["ANTHROPIC_AUTH_TOKEN"] == plan.key
    assert "ANTHROPIC_AUTH_TOKEN" not in settings_env_block(plan, target="k8s_pod")
    # the read-back happened before anything else
    assert ("GET /api/v1/keys/" + pk.hash, True) in fake.requests
    # the atexit fallback is registered and idempotent once close() revoked
    assert fake.atexit and fake.atexit[0][0] is K.revoke_plan_key
    plan.close()                                            # no session: the plan revokes itself
    assert fake.deletes == [pk.hash] and pk.revoked_at
    fake.atexit[0][0](*fake.atexit[0][1])
    assert fake.deletes == [pk.hash]                        # still one DELETE


def test_explicit_guardrail_providers_win_over_the_pins(fake, tmp_path):
    config = _config(tmp_path, fake.base_url,
                     routing={"enforcement": "key-guardrail", "guardrail": {"providers": ["z-ai", "Novita"]},
                              "models": {"z-ai/glm-5.2": {"order": ["novita"], "allow_fallbacks": False}}})
    plan = plan_mod.build_plan(config, run_id="r2")
    assert fake.key_requests[-1]["allowed_providers"] == ["novita", "z-ai"]
    plan.close()


def test_read_back_mismatch_revokes_and_refuses(tmp_path, monkeypatch):
    srv = FakeOpenRouter(echo_allowlist=False)
    base = srv.start()
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", FAKE_MGMT_KEY)
    quiet_atexit(monkeypatch)
    try:
        with pytest.raises(ConfigError, match="read-back mismatch.*did not echo an allowed-provider list"):
            plan_mod.build_plan(_config(tmp_path, base), run_id="r3")
        assert len(srv.deletes) == 1 and srv.deletes[0] in srv.issued     # revoked, nothing spent
        assert not any(p == "/api/v1/key" for p, _ in srv.requests)
    finally:
        srv.stop()
    assert K.guardrail_mismatches({"limit": 0.25, "allowed_providers": ["novita"]},
                                  K.ProvisionedKey("k", "h", "n", 0.25, ("novita",))) == []
    assert K.guardrail_mismatches({"limit": 1.0, "allowed_providers": ["z-ai"]},
                                  K.ProvisionedKey("k", "h", "n", 0.25, ("novita",))) == [
        "limit: requested 0.25, server holds 1.0",
        "allowed_providers: requested ['novita'], server holds ['z-ai']"]


def test_session_close_revokes_once_and_records_key_json(fake, tmp_path):
    plan = plan_mod.build_plan(_config(tmp_path, fake.base_url), run_id="r4")
    session = FastSession(plan, tmp_path).start()
    plan.attach(session)
    record = json.loads((tmp_path / "provider" / "key.json").read_text())
    assert record["hash"] == plan.provisioned.hash and record["revoked_at"] is None
    assert plan.key not in (tmp_path / "provider" / "key.json").read_text()
    assert session.key_usage_before == 0.5                  # /key answered to the per-run key
    plan.close()
    plan.close()
    assert fake.deletes == [plan.provisioned.hash]
    record = json.loads((tmp_path / "provider" / "key.json").read_text())
    assert record["revoked_at"] and record["revoke_error"] is None
    assert (tmp_path / "provider" / "key.json").stat().st_mode & 0o777 == 0o600


def test_failed_revoke_is_reported_and_retried_offline(tmp_path, monkeypatch, capsys):
    srv = FakeOpenRouter(revoke_status=503)
    base = srv.start()
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", FAKE_MGMT_KEY)
    quiet_atexit(monkeypatch)
    try:
        plan = plan_mod.build_plan(_config(tmp_path, base), run_id="r5")
        session = FastSession(plan, tmp_path).start()
        plan.attach(session)
        (tmp_path / "run_result.json").write_text(json.dumps({"exit_code": 0, "message_ids": []}))
        plan.close()
        err = capsys.readouterr().err
        assert "could not be revoked" in err and plan.provisioned.hash in err
        record = json.loads((tmp_path / "provider" / "key.json").read_text())
        assert record["revoked_at"] is None and "503" in record["revoke_error"]
        run = json.loads((tmp_path / "run_result.json").read_text())
        assert any("could not be revoked" in w for w in run["cost_warnings"])
        # the offline retry once the server recovers
        srv.revoke_status = 200
        assert K.main(["revoke", str(tmp_path)]) == 0
        assert srv.deletes == [plan.provisioned.hash] * 2
        assert json.loads((tmp_path / "provider" / "key.json").read_text())["revoked_at"]
        assert K.main(["revoke", str(tmp_path)]) == 0          # already revoked: nothing to do
    finally:
        srv.stop()
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY")
    assert K.main(["revoke", str(tmp_path / "nowhere")]) == 2


def test_keyboard_interrupt_still_revokes(fake, tmp_path):
    plan = plan_mod.build_plan(_config(tmp_path, fake.base_url), run_id="r6")
    session = FastSession(plan, tmp_path).start()
    plan.attach(session)

    def interrupted():
        raise KeyboardInterrupt

    session.finish = interrupted
    plan.close()
    assert fake.deletes == [plan.provisioned.hash]


def test_management_key_never_reaches_the_hook_env(fake, tmp_path):
    from agent_eval.hooks import build_hook_env

    plan = plan_mod.build_plan(_config(tmp_path, fake.base_url), run_id="r7")
    env = build_hook_env("ws", "r7", "eval.yaml", ".", "m", plan=plan, run_dir=tmp_path)
    assert "OPENROUTER_MANAGEMENT_KEY" not in env and "OPENROUTER_API_KEY" not in env
    assert plan.key not in json.dumps(env)
    plan.close()
