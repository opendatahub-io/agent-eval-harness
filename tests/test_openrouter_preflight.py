"""The preflight minimum (spec 014, PR-5) against the loopback fake."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.providers.base import ConfigError, parse_agent_model  # noqa: E402
from agent_eval.providers.openrouter.preflight import (  # noqa: E402
    SNAPSHOT_RELPATH, plan_models, run_preflight, table_sha)
from agent_eval.providers.openrouter.routing import RoutingTable  # noqa: E402
from openrouter_fakes import FakeOpenRouter, make_plan  # noqa: E402


@pytest.fixture
def fake():
    srv = FakeOpenRouter()
    srv.base_url = srv.start()
    yield srv
    srv.stop()


def test_strict_pass_writes_the_snapshot(fake, tmp_path):
    plan = make_plan(fake.base_url, run_id="r1")
    res = run_preflight(plan, level="strict", run_dir=tmp_path)
    assert res.failures == [] and res.warnings == [] and res.degraded_reason is None
    snap = json.loads((tmp_path / SNAPSHOT_RELPATH).read_text())
    assert snap["routing_sha"] == table_sha(plan.routing) and snap["ts"]
    assert snap["keys"]["z-ai/glm-5.2"] == {
        "variant": "exacto", "pinned_set": ["novita"],
        "catalog": {"providers": ["novita", "z-ai"], "endpoints": ["novita/fp8", "z-ai"]}}
    assert snap["catalog"]["endpoints"]["z-ai/glm-5.2"][0]["provider_name"] == "Novita"
    # the key-authenticated GETs carried the bearer, the catalog GETs did not need it
    assert ("/api/v1/key", True) in fake.requests and ("/api/v1/models/user", True) in fake.requests
    assert ("/api/v1/models", False) in fake.requests


def test_plan_models_covers_every_agent_path_id():
    plan = make_plan(background_model="qwen/qwen3-8b", hook=parse_agent_model("openrouter:/qwen/qwen3-8b"))
    assert plan_models(plan) == {"z-ai/glm-5.2": "exacto", "qwen/qwen3-8b": None}


def test_unknown_slug_and_unserved_pin_fail_strict(fake, tmp_path):
    plan = make_plan(fake.base_url, skill=parse_agent_model("openrouter:/nobody/none"),
                     subagent=parse_agent_model("openrouter:/nobody/none"))
    with pytest.raises(ConfigError, match="nobody/none: not in the OpenRouter catalog"):
        run_preflight(plan, level="strict", run_dir=tmp_path)
    table = RoutingTable.from_dict({"models": {"z-ai/glm-5.2": {"only": ["deepinfra"]}}})
    with pytest.raises(ConfigError, match="pinned provider\\(s\\) deepinfra do not serve it"):
        run_preflight(make_plan(fake.base_url, routing=table), level="strict")
    table = RoutingTable.from_dict({"models": {"z-ai/glm-5.2": {"only": ["novita"], "quantizations": ["bf16"]}}})
    with pytest.raises(ConfigError, match="quantization \\['bf16'\\]"):
        run_preflight(make_plan(fake.base_url, routing=table), level="strict")


def test_bad_key_and_ineligible_model_fail_strict(fake, tmp_path):
    with pytest.raises(ConfigError, match="inference key \\(OPENROUTER_API_KEY\\) rejected: HTTP 401"):
        run_preflight(make_plan(fake.base_url, key="sk-or-wrong"), level="strict")
    plan = make_plan(fake.base_url, skill=parse_agent_model("openrouter:/deepseek/deepseek-v4.1-flash"),
                     subagent=parse_agent_model("openrouter:/deepseek/deepseek-v4.1-flash"), pins=False)
    with pytest.raises(ConfigError, match="deepseek-v4.1-flash: not available to this key"):
        run_preflight(plan, level="strict")


def test_warn_level_degrades_instead_of_failing(fake, tmp_path, capsys):
    res = run_preflight(make_plan(fake.base_url, key="sk-or-wrong"), level="warn", run_dir=tmp_path)
    assert res.failures and res.degraded_reason == "preflight"
    assert "WARNING: preflight: inference key" in capsys.readouterr().err
    assert (tmp_path / SNAPSHOT_RELPATH).exists()


def test_catalog_failure_degrades_but_key_checks_stay_strict(tmp_path):
    srv = FakeOpenRouter(catalog_down=True)
    base = srv.start()
    try:
        res = run_preflight(make_plan(base), level="strict", run_dir=tmp_path)
        assert res.degraded_reason == "catalog" and res.failures == []
        assert any("catalog unavailable" in w for w in res.warnings)
        with pytest.raises(ConfigError, match="rejected: HTTP 401"):
            run_preflight(make_plan(base, key="nope"), level="strict")
    finally:
        srv.stop()


def test_off_does_nothing():
    res = run_preflight(make_plan("http://127.0.0.1:9/api"), level="off")
    assert res.snapshot is None and res.failures == []
    with pytest.raises(ConfigError, match="preflight level"):
        run_preflight(make_plan(), level="loud")
