"""execute.py end to end under a provider plan (spec 014, audit-mode hygiene):
a fake ``claude`` and a loopback OpenRouter, no key. Asserts the startup
order (plan → preflight → session), the per-case bindings, the run-end pass
(backfill drain, key usage, final reconcile before the strict flags), the
files in the run dir, and that no secret leaks into argv, the child env, the
run dir or stderr."""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import execute as ex  # noqa: E402  (skills/eval-run/scripts via conftest)
from agent_eval.providers.openrouter.session import ProviderSession  # noqa: E402
from openrouter_fakes import FAKE_KEY, GEN_COST, FakeOpenRouter, fake_claude_records, install_fake_claude  # noqa: E402


class FastSession(ProviderSession):
    def __init__(self, *a, **kw):
        kw.update(first_poll_s=0.0, poll_s=0.01, give_up_s=0.3, settle_s=0.0, key_poll_s=0.0, key_max_s=2.0)
        super().__init__(*a, **kw)


def _setup(tmp_path, monkeypatch, base_url, *, prompt="say hi HOOKCALL", providers=True, strict=()):
    install_fake_claude(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    for cid in ("case-1", "case-2"):
        (ws / "cases" / cid).mkdir(parents=True)
        (ws / "cases" / cid / "input.yaml").write_text(f"id: {cid}\n")
    (ws / "case_order.yaml").write_text(yaml.safe_dump(["case-1", "case-2"]))
    cfg = {
        "name": "t",
        "runner": {"type": "claude-code"},
        "execution": {"mode": "case", "prompt": prompt, "max_budget_usd": 2},
        "dataset": {"path": str(ws / "cases")},
        "outputs": [{"path": "output"}],
        "models": {"skill": "openrouter:/z-ai/glm-5.2:exacto"},
    }
    if providers:
        cfg["models"]["providers"] = {"openrouter": {
            "base_url": base_url, "attribution": {"title": "t", "run_id_header": True},
            "routing": {"models": {"z-ai/glm-5.2": {"order": ["novita"], "allow_fallbacks": False}}}}}
    config = tmp_path / "eval.yaml"
    config.write_text(yaml.safe_dump(cfg, sort_keys=False))
    out = tmp_path / "runs" / "run-x"
    monkeypatch.setattr(ex, "_SESSION_FACTORY", FastSession)
    monkeypatch.setattr(ex, "_setup_console_log", lambda *_: None)
    monkeypatch.setattr(ex, "_PROVIDER_PLAN", None)
    monkeypatch.setattr(ex, "_PROVIDER_SESSION", None)
    monkeypatch.setattr(sys, "argv", ["execute.py", "--config", str(config), "--workspace", str(ws),
                                      "--output", str(out), "--run-id", "run-x", *strict])
    # hostile host env
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-host-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "sk-or-mgmt-secret")
    return ws, out


def _main():
    with pytest.raises(SystemExit) as exc:
        ex.main()
    return exc.value.code


def test_case_mode_run_under_a_plan(tmp_path, monkeypatch, capsys):
    fake = FakeOpenRouter()
    base = fake.start()
    try:
        ws, out = _setup(tmp_path, monkeypatch, base, strict=("--strict-cost", "--strict-routing"))
        code = _main()
        err = capsys.readouterr().err
    finally:
        fake.stop()
    assert code == 0, err
    assert "Provider: openrouter direct | model: z-ai/glm-5.2:exacto | enforcement: audit" in err
    assert "key exposed to agent: operator key sha256:" in err
    # run dir: snapshot, ledger, hook ids, reconciled results
    assert (out / "provider" / "routing_snapshot.json").exists()
    rows = [json.loads(line) for line in (out / "provider" / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 6 and {r["case_id"] for r in rows} == {"case-1", "case-2"}
    assert sorted(r["role"] for r in rows) == ["agent"] * 4 + ["hook"] * 2
    run = json.loads((out / "run_result.json").read_text())
    assert run["exit_code"] == 0 and run["cost_source"] == "openrouter:generation"
    assert run["cost_usd"] == pytest.approx(4 * GEN_COST, abs=1e-6)
    assert run["cost_usd_estimate"] == 1.0 and run["cases_priced"] == 2
    assert run["hook_cost_usd"] == pytest.approx(2 * GEN_COST, abs=1e-6)
    assert run["cost_coverage"]["key_usage_delta_usd"] == pytest.approx(6 * GEN_COST, abs=1e-6)
    assert run["routing"]["violations"] == [] and run["routing"]["audit_complete"] is True
    assert run["routing"]["snapshot"] and run["provider"]["key_hash"].startswith("sha256:")
    assert run["eval_params"]["provider"]["routing_enforcement"] == "audit"
    assert run["eval_params"]["budget"] == {"cli_cap_usd": 100.0, "invocation_usd": 2, "run_usd": None,
                                            "enforcement": "cli-estimate"}
    assert run["budget"]["cli_cap_usd"] == 100.0
    case = json.loads((out / "cases" / "case-1" / "run_result.json").read_text())
    assert case["cost_source"] == "openrouter:generation" and case["cost_usd"] == pytest.approx(2 * GEN_COST, abs=1e-6)
    assert case["cost_usd_estimate"] == 0.5 and case["error_class"] is None
    assert "strict_failures" not in run
    # every CLI launch: bare id, inflated cap, the overlay, a scrubbed env, per-case hook ids
    recs = fake_claude_records(ws / "cases" / "case-1") + fake_claude_records(ws / "cases" / "case-2")
    assert len(recs) == 2
    for rec in recs:
        argv = rec["argv"]
        assert argv[argv.index("--model") + 1] == "z-ai/glm-5.2:exacto"
        assert argv[argv.index("--max-budget-usd") + 1] == "100.0"
        assert rec["settings"]["env"]["ANTHROPIC_BASE_URL"] == base
        assert rec["settings"]["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "X-OpenRouter-Title: t\nx-eval-run-id: run-x"
        assert rec["token_sha"] and "OPENROUTER_API_KEY" not in rec["env_keys"]
        assert "CLAUDE_CODE_USE_VERTEX" not in rec["env_keys"] and "ANTHROPIC_API_KEY" not in rec["env_keys"]
        assert rec["env"]["AGENT_EVAL_HOOK_IDS"].startswith(str(out / "provider" / "hook-ids-case-"))
    # nothing secret in the run dir or on stderr
    for path in out.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert FAKE_KEY not in text and "sk-or-mgmt-secret" not in text and "sk-ant-host-secret" not in text, path
    assert FAKE_KEY not in err and "sk-or-mgmt-secret" not in err
    assert not list((ws / "cases" / "case-1" / ".claude").glob(".eval-overlay.json"))


def test_strict_cost_fails_when_generations_never_land(tmp_path, monkeypatch, capsys):
    fake = FakeOpenRouter(lag_calls=10 ** 6)           # /generation never answers 200
    base = fake.start()
    try:
        ws, out = _setup(tmp_path, monkeypatch, base, prompt="plain", strict=("--strict-cost",))
        code = _main()
        err = capsys.readouterr().err
    finally:
        fake.stop()
    run = json.loads((out / "run_result.json").read_text())
    # the key-usage delta cannot help either (no priced generation moved the counter): unavailable
    assert run["cost_source"] == "unavailable" and run["cost_usd"] is None
    assert code == 2 and run["exit_code"] == 2 and run["strict_failures"] == ["cost_source unavailable"]
    assert "STRICT: cost_source unavailable" in err


def test_declared_block_without_an_openrouter_role_is_inert(tmp_path, monkeypatch, capsys):
    fake = FakeOpenRouter()
    base = fake.start()
    try:
        ws, out = _setup(tmp_path, monkeypatch, base, prompt="plain")
        monkeypatch.setattr(sys, "argv", sys.argv + ["--model", "claude-sonnet-4-5"])
        code = _main()
        err = capsys.readouterr().err
    finally:
        fake.stop()
    assert code == 0
    assert "routing table inactive" in err and "Provider: openrouter" not in err
    assert not (out / "provider").exists()
    run = json.loads((out / "run_result.json").read_text())
    assert "cost_source" not in run and run["cost_usd"] == 1.0            # the CLI's own numbers, untouched
    rec = fake_claude_records(ws / "cases" / "case-1")[-1]
    assert rec["argv"][rec["argv"].index("--model") + 1] == "claude-sonnet-4-5"
    assert "CLAUDE_CODE_USE_VERTEX" in rec["env_keys"]                       # forwarding unchanged
    assert fake.requests == []


def test_preflight_failure_exits_2_before_any_case(tmp_path, monkeypatch, capsys):
    fake = FakeOpenRouter()
    base = fake.start()
    try:
        ws, out = _setup(tmp_path, monkeypatch, base)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-wrong")
        code = _main()
        err = capsys.readouterr().err
    finally:
        fake.stop()
    assert code == 2 and "preflight failed" in err and "rejected: HTTP 401" in err
    assert fake_claude_records(ws / "cases" / "case-1") == []
    assert "sk-or-wrong" not in err


def test_a_non_claude_runner_is_refused_under_a_plan(tmp_path, monkeypatch, capsys):
    """A per-step runner override is refused by the config loader; the
    ``--agent`` CLI override by execute.py — either way before any case runs."""
    fake = FakeOpenRouter()
    base = fake.start()
    try:
        ws, out = _setup(tmp_path, monkeypatch, base)
        argv = list(sys.argv)
        monkeypatch.setattr(sys, "argv", argv + ["--agent", "codex"])
        code = _main()
        err = capsys.readouterr().err
        assert code == 2 and "runner 'codex' cannot run" in err
        monkeypatch.setattr(sys, "argv", argv)
        cfg = yaml.safe_load((tmp_path / "eval.yaml").read_text())
        cfg["execution"] = {"mode": "case", "steps": [
            {"id": "a", "skill": "s1", "arguments": "x"},
            {"id": "b", "skill": "s2", "arguments": "y", "runner": {"type": "codex"}}]}
        (tmp_path / "eval.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        with pytest.raises(ValueError, match="execution.steps\\[1\\].runner.type.*codex"):
            ex.main()
    finally:
        fake.stop()
    assert fake_claude_records(ws / "cases" / "case-1") == []


def test_a_cli_activated_plan_still_refuses_a_non_claude_step_runner(tmp_path, monkeypatch, capsys):
    """The loader only sees the config's skill model; a plan activated by
    --model must apply the same rule to per-step runner overrides."""
    fake = FakeOpenRouter()
    base = fake.start()
    try:
        ws, out = _setup(tmp_path, monkeypatch, base)
        cfg = yaml.safe_load((tmp_path / "eval.yaml").read_text())
        cfg["models"]["skill"] = "claude-sonnet-4-5"
        cfg["execution"] = {"mode": "case", "steps": [
            {"id": "a", "skill": "s1", "arguments": "x"},
            {"id": "b", "skill": "s2", "arguments": "y", "runner": {"type": "codex"}}]}
        (tmp_path / "eval.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        monkeypatch.setattr(sys, "argv", sys.argv + ["--model", "openrouter:/z-ai/glm-5.2:exacto"])
        code = _main()
        err = capsys.readouterr().err
    finally:
        fake.stop()
    assert code == 2 and "execution.steps[].runner.type 'codex'" in err
    assert fake_claude_records(ws / "cases" / "case-1") == []

