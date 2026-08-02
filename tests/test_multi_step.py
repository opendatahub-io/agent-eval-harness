"""Multi-step execution (execution.steps): config, execute, score, Harbor."""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import execute as ex  # noqa: E402  (skills/eval-run/scripts via conftest)
import score as sc  # noqa: E402
from agent_eval.agent.base import RunResult  # noqa: E402
from agent_eval.config import EvalConfig, resolve_arguments  # noqa: E402
from agent_eval.events import parse_stream_events  # noqa: E402


def _write(tmp_path, body, name="eval.yaml"):
    p = tmp_path / name
    p.write_text(body.replace("__TMP__", str(tmp_path)))
    return p


def _jsonl(text):
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text",
                                                "text": text}]}}) + "\n"


def _result(exit_code=0, target=None, stdout=None):
    return RunResult(
        exit_code=exit_code, stdout=stdout if stdout is not None else _jsonl(f"OUT-{target}"),
        stderr="", duration_s=1.0, token_usage={"input": 1, "output": 1},
        cost_usd=0.01, num_turns=2, resolved_model="m", models_used=["m"],
        per_model_usage={}, per_model_turns={})


class _FakeRunner:
    """Records calls, writes a per-target file into the shared workspace."""

    def __init__(self, capture=None, fail_targets=()):
        self.capture = capture if capture is not None else []
        self.fail_targets = set(fail_targets)

    @classmethod
    def from_config(cls, config, **kw):
        return cls()

    @property
    def name(self):
        return "fake"

    def execute(self, target=None, args="", workspace=None, model=None, **kw):
        self.capture.append({"target": target, "args": args})
        ws = Path(workspace)
        (ws / "output").mkdir(parents=True, exist_ok=True)
        (ws / "output" / f"{target or 'prompt'}.md").write_text(f"from {target}")
        ec = 1 if target in self.fail_targets else 0
        return _result(exit_code=ec, target=target)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:

    def test_steps_and_per_step_runner_parse(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: case
  steps:
    - {id: a, skill: s1, arguments: "x", env: {K: v}}
    - {id: b, skill: s2, arguments: "{{ steps.a.output }}", runner: {type: claude-code, effort: high}}
"""))
        steps = cfg.execution.steps
        assert [s.id for s in steps] == ["a", "b"]
        assert steps[0].env == {"K": "v"}
        assert steps[1].runner.effort == "high"

    def test_single_skill_normalizes_to_one_step(self):
        c = EvalConfig()
        c.execution.skill = "my-skill"
        c.execution.arguments = "{{ input.x }}"
        rs = c.execution.resolved_steps()
        assert len(rs) == 1 and rs[0].skill == "my-skill"

    def test_multi_step_resolved_steps_verbatim(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  steps:
    - {id: a, skill: s1}
    - {id: b, skill: s2}
"""))
        assert [s.id for s in cfg.execution.resolved_steps()] == ["a", "b"]

    @pytest.mark.parametrize("body,msg", [
        ("execution:\n  skill: x\n  steps:\n    - {id: a, skill: y}\n",
         "mutually exclusive"),
        ("execution:\n  mode: batch\n  steps:\n    - {id: a, skill: y}\n",
         "mode: case"),
        ("execution:\n  steps:\n    - {id: a, skill: y}\n    - {id: a, skill: z}\n",
         "unique"),
        ("execution:\n  steps:\n    - {skill: y}\n", "non-empty 'id'"),
        ("execution:\n  steps:\n    - {id: a}\n", "skill or prompt"),
        ("execution:\n  steps:\n    - {id: a, skill: y, prompt: z}\n",
         "mutually exclusive"),
    ])
    def test_validation_errors(self, tmp_path, body, msg):
        with pytest.raises(ValueError, match=msg):
            EvalConfig.from_yaml(_write(tmp_path, "name: t\n" + body))

    def test_unknown_judge_step_raises(self, tmp_path):
        with pytest.raises(ValueError, match="does not match any execution step"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  steps:
    - {id: a, skill: y}
judges:
  - {name: j, step: nope, prompt: p}
"""))

    def test_steps_template_namespace(self):
        assert resolve_arguments(
            "{{ steps.a.output }}", {}, steps={"a": {"output": "R"}}) == "R"

    def test_step_hook_phases_parse(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  steps:
    - {id: a, skill: y}
hooks:
  before_step: [{command: "echo hi"}]
  after_step: [{command: "echo bye"}]
"""))
        assert cfg.hooks.before_step[0].command == "echo hi"
        assert cfg.hooks.after_step[0].command == "echo bye"


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

class TestExecute:

    def _cfg(self, tmp_path):
        return EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: case
  steps:
    - {id: refine, skill: strategy-refine, arguments: "STRAT-{{ input.id }}"}
    - {id: review, skill: strategy-review, arguments: "{{ steps.refine.output }}"}
dataset: {path: __TMP__/cases}
outputs:
  - path: output
"""))

    def _case(self, tmp_path):
        cw = tmp_path / "cases" / "case-1"
        cw.mkdir(parents=True)
        (cw / "input.yaml").write_text("id: '99'\n")
        return cw

    def test_step_loop_templating_and_shared_workspace(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cw = self._case(tmp_path)
        out = tmp_path / "run"
        out.mkdir()
        cap = []
        _, res = ex._run_multi_step_case(
            _FakeRunner(cap), "case-1", cw, out, "opus", None, None, "",
            5.0, 600, 1, 1, cfg)
        # step 2's arguments referenced step 1's output
        assert [c["args"] for c in cap] == ["STRAT-99", "OUT-strategy-refine"]
        assert res["exit_code"] == 0
        assert list(res["steps"].keys()) == ["refine", "review"]
        assert res["cost_usd"] == pytest.approx(0.02)  # summed across steps
        # per-step stdout saved for step-scoped judges
        base = out / "cases" / "case-1" / "steps"
        assert (base / "refine" / "stdout.log").exists()
        assert (base / "review" / "stdout.log").exists()
        # shared workspace: both steps' files persist
        names = {p.name for p in (cw / "output").iterdir()}
        assert {"strategy-refine.md", "strategy-review.md"} <= names

    def test_on_failure_fail_aborts_remaining_steps(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cw = self._case(tmp_path)
        out = tmp_path / "run"
        out.mkdir()
        cap = []
        _, res = ex._run_multi_step_case(
            _FakeRunner(cap, fail_targets={"strategy-refine"}), "case-1", cw,
            out, "opus", None, None, "", 5.0, 600, 1, 1, cfg)
        assert [c["target"] for c in cap] == ["strategy-refine"]  # stopped
        assert res["exit_code"] == 1
        assert res.get("aborted_at_step") == "refine"


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

class TestScore:

    def test_per_step_scoping(self, tmp_path):
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: case
  steps:
    - {id: refine, skill: s1, arguments: x}
    - {id: review, skill: s2, arguments: y}
dataset: {path: __TMP__/cases}
judges:
  - {name: refine_j, step: refine, check: "return ('R1' in outputs.get('conversation','') and 'R2' not in outputs.get('conversation',''), '')"}
  - {name: whole_j, check: "return ('R2' in outputs.get('conversation',''), '')"}
"""))
        cd = tmp_path / "cases" / "case-1"
        (cd / "steps" / "refine").mkdir(parents=True)
        (cd / "steps" / "review").mkdir(parents=True)
        (cd / "steps" / "refine" / "stdout.log").write_text(_jsonl("R1"))
        (cd / "steps" / "review" / "stdout.log").write_text(_jsonl("R2"))
        (cd / "events.json").write_text(
            json.dumps(parse_stream_events(_jsonl("R2"))))
        res = sc.score_cases(sc.load_judges(cfg), [cd], cfg)
        pc = res["per_case"]["case-1"]
        assert pc["refine_j"]["value"] is True   # saw only refine's trace
        assert pc["refine_j"]["step"] == "refine"
        assert pc["whole_j"]["value"] is True     # whole case = final step


# ---------------------------------------------------------------------------
# Harbor generation
# ---------------------------------------------------------------------------

class TestHarbor:

    def _case(self, tmp_path):
        (tmp_path / "cases" / "case-1").mkdir(parents=True)
        (tmp_path / "cases" / "case-1" / "input.yaml").write_text("id: '5'\n")

    def test_generate_multi_step_package(self, tmp_path):
        from agent_eval.harbor.tasks import generate_tasks
        self._case(tmp_path)
        cfgf = _write(tmp_path, """
name: strat
execution:
  mode: case
  steps:
    - {id: create, skill: s-create, arguments: "X-{{ input.id }}"}
    - {id: review, skill: s-review, arguments: "X-{{ input.id }}"}
dataset: {path: __TMP__/cases}
outputs:
  - path: artifacts
judges:
  - {name: g, step: create, prompt: "p {{ conversation }}"}
  - {name: whole, prompt: "p {{ outputs }}"}
""")
        cfg = EvalConfig.from_yaml(cfgf)
        out = tmp_path / "tasks"
        generate_tasks(cfg, cfgf, out, "img", workdir="/workspace")
        td = out / "case-1"
        toml = (td / "task.toml").read_text()
        assert 'schema_version = "1.4"' in toml
        assert toml.count("[[steps]]") == 2
        assert 'multi_step_reward_strategy = "final"' in toml
        assert (td / "steps" / "create" / "instruction.md").read_text().strip() \
            == "/s-create X-5"
        create_j = yaml.safe_load(
            (td / "steps" / "create" / "tests" / "eval.yaml").read_text())["judges"]
        review_j = yaml.safe_load(
            (td / "steps" / "review" / "tests" / "eval.yaml").read_text())["judges"]
        assert [j["name"] for j in create_j] == ["g"]     # step-scoped here
        assert [j["name"] for j in review_j] == ["whole"]  # whole-case on final
        assert all("step" not in j for j in create_j + review_j)

    def test_no_judge_step_gets_trivial_verifier(self, tmp_path):
        from agent_eval.harbor.tasks import generate_tasks
        self._case(tmp_path)
        cfgf = _write(tmp_path, """
name: strat
execution:
  mode: case
  steps:
    - {id: setup, skill: s-setup, arguments: x}
    - {id: run, skill: s-run, arguments: y}
dataset: {path: __TMP__/cases}
judges:
  - {name: whole, prompt: "p {{ outputs }}"}
""")
        cfg = EvalConfig.from_yaml(cfgf)
        out = tmp_path / "tasks"
        generate_tasks(cfg, cfgf, out, "img")
        setup_test = (out / "case-1" / "steps" / "setup" / "tests" / "test.sh").read_text()
        assert '"reward": 1.0' in setup_test  # trivial pass, no eval.yaml
        assert not (out / "case-1" / "steps" / "setup" / "tests" / "eval.yaml").exists()

    def test_steps_template_ref_errors_for_harbor(self, tmp_path):
        from agent_eval.harbor.tasks import generate_tasks
        self._case(tmp_path)
        cfgf = _write(tmp_path, """
name: t
execution:
  mode: case
  steps:
    - {id: a, skill: s1, arguments: x}
    - {id: b, skill: s2, arguments: "{{ steps.a.output }}"}
dataset: {path: __TMP__/cases}
""")
        cfg = EvalConfig.from_yaml(cfgf)
        with pytest.raises(ValueError, match="do not round-trip to Harbor"):
            generate_tasks(cfg, cfgf, tmp_path / "tasks", "img")


# ---------------------------------------------------------------------------
# Hardening (CodeRabbit PR #172)
# ---------------------------------------------------------------------------

class TestHardening:

    @pytest.mark.parametrize("sid,msg", [
        ("../evil", "path separators"),
        ("a/b", "path separators"),
        ("..", "relative directory reference"),
    ])
    def test_step_id_path_traversal_rejected(self, tmp_path, sid, msg):
        with pytest.raises(ValueError, match=msg):
            EvalConfig.from_yaml(_write(
                tmp_path,
                "name: t\nexecution:\n  steps:\n"
                f'    - {{id: "{sid}", skill: y}}\n'))

    def test_step_env_must_be_mapping(self, tmp_path):
        with pytest.raises(ValueError, match="env must be a mapping"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  steps:
    - {id: a, skill: y, env: "K=v"}
"""))

    def test_step_timeout_must_be_positive_int(self, tmp_path):
        with pytest.raises(ValueError, match="timeout must be a positive integer"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  steps:
    - {id: a, skill: y, timeout: -5}
"""))

    def test_step_budget_must_be_non_negative(self, tmp_path):
        with pytest.raises(ValueError, match="max_budget_usd must be a non-negative"):
            EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  steps:
    - {id: a, skill: y, max_budget_usd: -1}
"""))

    def test_harbor_per_step_timeout_honored(self, tmp_path):
        from agent_eval.harbor.tasks import generate_tasks
        (tmp_path / "cases" / "case-1").mkdir(parents=True)
        (tmp_path / "cases" / "case-1" / "input.yaml").write_text("id: '1'\n")
        cfgf = _write(tmp_path, """
name: t
execution:
  mode: case
  steps:
    - {id: a, skill: s1, arguments: x, timeout: 42}
    - {id: b, skill: s2, arguments: y}
dataset: {path: __TMP__/cases}
""")
        cfg = EvalConfig.from_yaml(cfgf)
        generate_tasks(cfg, cfgf, tmp_path / "tasks", "img", agent_timeout=1800.0)
        toml = (tmp_path / "tasks" / "case-1" / "task.toml").read_text()
        assert "timeout_sec = 42" in toml       # step a honored step.timeout
        assert "timeout_sec = 1800.0" in toml    # step b fell back to default

    def test_harbor_toml_escapes_quotes(self, tmp_path):
        tomllib = pytest.importorskip("tomllib")
        from agent_eval.harbor.tasks import generate_tasks
        (tmp_path / "cases" / "case-1").mkdir(parents=True)
        (tmp_path / "cases" / "case-1" / "input.yaml").write_text("id: '1'\n")
        cfgf = _write(tmp_path, """
name: 'weird "quoted" name'
execution:
  mode: case
  steps:
    - {id: a, skill: s1, arguments: x}
dataset: {path: __TMP__/cases}
""")
        cfg = EvalConfig.from_yaml(cfgf)
        generate_tasks(cfg, cfgf, tmp_path / "tasks", "img")
        toml = (tmp_path / "tasks" / "case-1" / "task.toml").read_text()
        parsed = tomllib.loads(toml)  # must be valid TOML despite the quotes
        assert '"quoted"' in parsed["task"]["name"]

    def test_harbor_missing_input_error_omits_steps_hint(self, tmp_path):
        from agent_eval.harbor.tasks import generate_tasks
        (tmp_path / "cases" / "case-1").mkdir(parents=True)
        (tmp_path / "cases" / "case-1" / "input.yaml").write_text("id: '1'\n")
        cfgf = _write(tmp_path, """
name: t
execution:
  mode: case
  steps:
    - {id: a, skill: s1, arguments: "{{ input.missing }}"}
dataset: {path: __TMP__/cases}
""")
        cfg = EvalConfig.from_yaml(cfgf)
        with pytest.raises(ValueError) as ei:
            generate_tasks(cfg, cfgf, tmp_path / "tasks", "img")
        assert "cannot be resolved" in str(ei.value)
        assert "round-trip to Harbor" not in str(ei.value)  # not a steps.* failure

    def test_score_skips_symlinked_step_stdout(self, tmp_path):
        import os
        secret = tmp_path / "secret.txt"
        secret.write_text("TOPSECRET")
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: case
  steps:
    - {id: a, skill: s1, arguments: x}
dataset: {path: __TMP__/cases}
judges:
  - {name: j, step: a, check: "return (True, '')"}
"""))
        cd = tmp_path / "cases" / "case-1"
        (cd / "steps" / "a").mkdir(parents=True)
        os.symlink(secret, cd / "steps" / "a" / "stdout.log")  # planted symlink
        rec = sc.load_case_record(cd, cfg)
        assert "TOPSECRET" not in rec["steps"]["a"].get("conversation", "")
