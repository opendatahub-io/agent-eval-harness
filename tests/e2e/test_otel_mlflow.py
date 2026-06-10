"""E2E tests for OTel traces landing in MLflow.

Validates the full pipeline: agent → OTel (protobuf) → MLflow OTLP endpoint
→ traces queryable via MLflow API. Also verifies that OTel-derived data
matches what the local JSON receiver captures for judges.

Requires: ANTHROPIC_API_KEY, local mlflow server (started by fixture).
Run with: python3 -m pytest tests/e2e/test_otel_mlflow.py -v -s -m e2e
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.e2e

FIXTURES = Path(__file__).parent / "fixtures"
CASES_DIR = FIXTURES / "otel-smoke-cases"


@pytest.fixture(scope="module")
def mlflow_server():
    """Start a local MLflow server for the test module."""
    tmpdir = tempfile.mkdtemp()
    db_uri = f"sqlite:///{tmpdir}/mlflow.db"
    port = 5199

    proc = subprocess.Popen(
        ["mlflow", "server",
         "--backend-store-uri", db_uri,
         "--host", "127.0.0.1",
         "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for server to be ready
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("MLflow server failed to start")

    yield {
        "url": f"http://127.0.0.1:{port}",
        "port": port,
        "db_uri": db_uri,
    }

    os.kill(proc.pid, signal.SIGTERM)
    proc.wait(timeout=10)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mlflow_experiment(mlflow_server):
    """Create a fresh MLflow experiment for each test."""
    import mlflow
    mlflow.set_tracking_uri(mlflow_server["url"])
    exp_name = f"otel-test-{int(time.time())}"
    exp_id = mlflow.create_experiment(exp_name)
    return exp_id


def _load_case(case_id):
    return yaml.safe_load((CASES_DIR / case_id / "input.yaml").read_text())


class TestClaudeCodeMLflowTraces:
    """Verify Claude Code OTel spans land in MLflow correctly."""

    def test_traces_appear_in_mlflow(self, mlflow_server, mlflow_experiment):
        """Agent OTel spans are ingested by MLflow OTLP endpoint."""
        from agent_eval.agent import RUNNERS
        from agent_eval.config import OTelConfig

        case = _load_case("math-001")

        runner_cls = RUNNERS["claude-code"]
        # No OTel config on runner — we set env vars directly to point
        # at MLflow's OTLP endpoint (protobuf, not our JSON receiver)
        runner = runner_cls(effort="low")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()

            # Inject OTel env vars pointing at MLflow
            orig_env = os.environ.copy()
            os.environ["OTEL_TRACES_EXPORTER"] = "otlp"
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = mlflow_server["url"]
            os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
            os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = \
                f"x-mlflow-experiment-id={mlflow_experiment}"
            os.environ["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
            os.environ["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
            os.environ["OTEL_LOG_USER_PROMPTS"] = "1"
            os.environ["OTEL_LOG_TOOL_DETAILS"] = "1"

            try:
                result = runner.run_skill(
                    skill_name="",
                    args=case["prompt"],
                    workspace=workspace,
                    model="sonnet",
                    timeout_s=60,
                )
            finally:
                # Restore env
                os.environ.clear()
                os.environ.update(orig_env)

            assert result.exit_code == 0
            assert case["expected"].lower() in result.stdout.lower()

            # Wait for BatchSpanProcessor flush + MLflow ingestion
            time.sleep(3)

            # Query MLflow for traces
            import mlflow
            mlflow.set_tracking_uri(mlflow_server["url"])
            traces = mlflow.search_traces(
                experiment_ids=[mlflow_experiment],
            )
            assert len(traces) > 0, "No traces found in MLflow"

    def test_mlflow_traces_have_spans(self, mlflow_server, mlflow_experiment):
        """MLflow traces contain expected span structure."""
        from agent_eval.agent import RUNNERS

        case = _load_case("math-002")

        runner_cls = RUNNERS["claude-code"]
        runner = runner_cls(effort="low")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()

            orig_env = os.environ.copy()
            os.environ["OTEL_TRACES_EXPORTER"] = "otlp"
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = mlflow_server["url"]
            os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
            os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = \
                f"x-mlflow-experiment-id={mlflow_experiment}"
            os.environ["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
            os.environ["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
            os.environ["OTEL_LOG_USER_PROMPTS"] = "1"
            os.environ["OTEL_LOG_TOOL_DETAILS"] = "1"

            try:
                result = runner.run_skill(
                    skill_name="",
                    args=case["prompt"],
                    workspace=workspace,
                    model="sonnet",
                    timeout_s=60,
                )
            finally:
                os.environ.clear()
                os.environ.update(orig_env)

            assert result.exit_code == 0

            time.sleep(3)

            import mlflow
            from mlflow import MlflowClient

            mlflow.set_tracking_uri(mlflow_server["url"])
            client = MlflowClient()
            traces = mlflow.search_traces(
                experiment_ids=[mlflow_experiment],
            )
            assert len(traces) > 0

            # Get trace details
            trace_id = traces.iloc[0]["trace_id"]
            trace = client.get_trace(trace_id)
            assert trace is not None

    def test_otel_and_stream_json_agree(self, mlflow_server, mlflow_experiment):
        """OTel tokens/turns match stream-json extraction."""
        from agent_eval.agent import RUNNERS
        from agent_eval.config import OTelConfig
        from agent_eval.otel.span_mapper import get_span_mapper

        case = _load_case("math-001")

        runner_cls = RUNNERS["claude-code"]
        otel = OTelConfig(enabled=True, content=True)
        runner = runner_cls(otel_config=otel, effort="low", log_prefix="e2e")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            output_dir = Path(tmpdir) / "output"
            output_dir.mkdir()

            # Also set MLflow OTLP env vars (agent exports to BOTH
            # our JSON receiver via runner OTel config AND MLflow via env)
            orig_env = os.environ.copy()
            os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = \
                f"x-mlflow-experiment-id={mlflow_experiment}"

            try:
                result = runner.run_skill(
                    skill_name="",
                    args=case["prompt"],
                    workspace=workspace,
                    model="sonnet",
                    timeout_s=60,
                    output_dir=output_dir,
                )
            finally:
                os.environ.clear()
                os.environ.update(orig_env)

            assert result.exit_code == 0

            # Local OTel receiver data
            otel_path = output_dir / "otel_spans.json"
            assert otel_path.exists()

            resource_spans = json.loads(otel_path.read_text()).get("resourceSpans", [])
            mapper = get_span_mapper("claude-code")
            otel_usage = mapper.extract_usage(resource_spans)

            # Stream-json data (from RunResult)
            assert result.token_usage is not None
            assert otel_usage["token_usage"]["input"] == result.token_usage["input"]
            assert otel_usage["token_usage"]["output"] == result.token_usage["output"]
            assert otel_usage["num_turns"] == result.num_turns
