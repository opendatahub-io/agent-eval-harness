"""The committed eval/anova-example works on the generic path, offline.

Exercises everything except the live agent calls + repo checkout: config
validity, matrix expansion, --dry-run cost, solve.sh's diff capture (with a
stubbed agent), and --analyze-only over the committed sample runs (→ anova.json
+ the eval-compare statistics section).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

_REPO = Path(__file__).parent.parent
for _p in ("skills/eval-anova/scripts", "skills/eval-compare/scripts"):
    p = str(_REPO / _p)
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.anova.matrix import MatrixBuilder  # noqa: E402
import orchestrate as O  # noqa: E402

EXAMPLE = _REPO / "eval" / "anova-example"
EVAL = EXAMPLE / "eval.yaml"
SAMPLE = EXAMPLE / "sample-runs" / "anova-example"


def test_config_and_matrix_valid():
    cfg = EvalConfig.from_yaml(str(EVAL))
    assert cfg.eval_name() == "anova-example"
    matrix = MatrixBuilder.from_yaml(EVAL, strict=True)
    conds = MatrixBuilder.expand_full_factorial(matrix.factors)
    assert len(conds) == 2  # 1 model × 2 contexts (a cognee A/B)
    # cli runner references the config_dir + context placeholders
    assert "{config_dir}" in cfg.runner.command and "{context}" in cfg.runner.command
    # both judges are defined: the objective tests_pass gate + the LLM rubric
    assert {j.name for j in cfg.judges} == {"tests_pass", "solution_quality"}


def test_dry_run_estimates_cost_without_executing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path))
    assert O.main(["--config", str(EVAL), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Total runs:" in out
    assert not (tmp_path / "anova-example").exists()  # nothing executed


def test_analyze_only_over_sample_runs(tmp_path):
    from analyze import analyze_runs
    import compare

    cfg = EvalConfig.from_yaml(str(EVAL))
    analysis, _ = analyze_runs(SAMPLE, cfg, write_to=tmp_path / "anova.json")

    assert analysis["design"]["n_cases"] == 4  # the four maas tasks
    # single-model matrix → the ANOVA is a one-way comparison over context
    assert analysis["anova"]["factor"] == "context"
    assert "repeated" in analysis["anova"]["method"].lower()
    # composite = tests_pass gate × normalised solution_quality, so cognee
    # (more passing tests + higher quality on the sample data) beats none
    means = {c["context"]: c["mean"] for c in analysis["condition_summaries"]}
    assert means["cognee"] > means["none"]

    stats = json.loads((tmp_path / "anova.json").read_text())
    out = tmp_path / "report"
    compare.generate_report(compare.discover_runs(SAMPLE), "Example", None, out, stats=stats)
    html = (out / "index.html").read_text()
    assert 'id="statistics"' in html and "Statistical Significance" in html


def test_solve_sh_captures_agent_diff(tmp_path):
    """solve.sh checks out a repo and captures the agent's edits as a diff.
    Uses a throwaway local repo + a stub AGENT_CMD (no network, no real agent)."""
    ws = tmp_path / "ws"; ws.mkdir()
    out = tmp_path / "out"
    (ws / "input.yaml").write_text(yaml.safe_dump({"prompt": "add a marker"}))

    src = tmp_path / "src"; src.mkdir()
    _git = lambda *a: subprocess.run(["git", "-C", str(src), *a], check=True,
                                     capture_output=True)
    _git("init", "-q")
    (src / "a.txt").write_text("hi\n")
    _git("add", "-A")
    _git("-c", "user.email=e@e", "-c", "user.name=e", "commit", "-qm", "init")
    base = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    # TEST_CMD stubs the test run so we exercise the tests_pass gate output too.
    env = dict(os.environ, MAAS_REPO_URL=str(src), MAAS_BASE_COMMIT=base,
               AGENT_CMD="echo fixed > NEWFILE.txt", TEST_CMD="true")
    r = subprocess.run(["bash", str(EXAMPLE / "solve.sh"), str(ws), str(out),
                        "test-model", ""], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "NEWFILE.txt" in (out / "solution.diff").read_text()
    assert json.loads((out / "tests.json").read_text())["passed"] is True

    # A failing test command → tests_pass gate would fail (passed: false).
    out2 = tmp_path / "out2"
    env["TEST_CMD"] = "false"
    subprocess.run(["bash", str(EXAMPLE / "solve.sh"), str(ws), str(out2),
                    "test-model", ""], env=env, capture_output=True, text=True)
    assert json.loads((out2 / "tests.json").read_text())["passed"] is False
