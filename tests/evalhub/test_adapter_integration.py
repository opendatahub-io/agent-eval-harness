"""End-to-end adapter test with mocked infrastructure."""
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import yaml
from agent_eval.agent.base import RunResult
from agent_eval.evalhub.adapter import AgentEvalAdapter


def _setup_eval_project(tmp_path):
    """Create a minimal eval project with config and dataset."""
    config = {
        "name": "integration-test",
        "skill": "test-skill",
        "execution": {"mode": "case", "arguments": "--input {query}"},
        "dataset": {"path": "cases", "schema": "query field"},
        "outputs": [{"path": "output", "schema": "Result files"}],
        "judges": [
            {
                "name": "ran_successfully",
                "check": "return (outputs.get('exit_code', 1) == 0, 'exit check')",
            }
        ],
        "runner": {
            "type": "claude-code",
            "effort": "normal",
        },
        "permissions": {
            "allow": ["Read"],
        },
    }
    eval_path = tmp_path / "eval.yaml"
    with open(eval_path, "w") as f:
        yaml.dump(config, f)
    for i, query in enumerate(["what is AI?", "explain TDD"], start=1):
        case_dir = tmp_path / "cases" / f"case-{i:03d}"
        case_dir.mkdir(parents=True)
        with open(case_dir / "input.yaml", "w") as f:
            yaml.dump({"query": query}, f)
    return eval_path


def test_full_lifecycle(tmp_path):
    """Test the full adapter lifecycle from JobSpec to JobResults."""
    eval_path = _setup_eval_project(tmp_path)
    from agent_eval.evalhub.s3_dataset import DatasetInfo

    cases_dir = tmp_path / "cases"

    mock_runner = MagicMock()
    mock_runner.name = "claude-code"
    mock_runner.run_skill.return_value = RunResult(
        exit_code=0, stdout="done", stderr="",
        duration_s=20.0, cost_usd=0.08, num_turns=6,
    )

    mock_runner_cls = MagicMock()
    mock_runner_cls.from_config.return_value = mock_runner

    with (
        patch("agent_eval.evalhub.adapter.download_dataset") as mock_download,
        patch("agent_eval.evalhub.adapter.RUNNERS", {"claude-code": mock_runner_cls}),
        patch("agent_eval.evalhub.adapter._framework_adapter_init"),
    ):
        mock_download.return_value = DatasetInfo(
            num_cases=2, case_ids=["case-001", "case-002"], dest=cases_dir)

        adapter = AgentEvalAdapter(eval_config_path=str(eval_path))
        mock_spec = MagicMock()
        mock_spec.id = "integration-job"
        mock_spec.benchmark_id = "integration-test"
        mock_spec.benchmark_index = 0
        mock_spec.model = MagicMock()
        mock_spec.model.name = "claude-sonnet-4-6"
        mock_spec.parameters = {"s3_bucket": "test", "s3_prefix": "cases/"}

        mock_callbacks = MagicMock()
        results = adapter.run_benchmark_job(mock_spec, mock_callbacks)

    assert results.id == "integration-job"
    assert results.model_name == "claude-sonnet-4-6"
    assert results.num_examples_evaluated == 2
    assert results.duration_seconds > 0

    metric_map = {r.metric_name: r.metric_value for r in results.results}
    assert "exit_code" in metric_map
    assert "duration_seconds" in metric_map
    assert "cost_usd" in metric_map

    # Runner created via from_config (not direct constructor)
    mock_runner_cls.from_config.assert_called_once()

    # Runner called twice (once per case)
    assert mock_runner.run_skill.call_count == 2
    first_call = mock_runner.run_skill.call_args_list[0]
    actual_skill = first_call.kwargs.get("skill_name")
    assert actual_skill == "test-skill"

    assert mock_callbacks.report_status.call_count >= 3
    mock_download.assert_not_called()  # local dataset exists
    mock_callbacks.report_results.assert_not_called()  # entrypoint's job
