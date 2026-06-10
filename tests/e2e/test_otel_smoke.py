"""E2E smoke tests for OTel trace capture with Claude Code and OpenCode.

These tests invoke real API calls and are skipped by default.
Run with: python3 -m pytest tests/e2e/test_otel_smoke.py -v -s -m e2e

Claude Code test requires ANTHROPIC_API_KEY.
OpenCode test requires GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from agent_eval.agent import RUNNERS
from agent_eval.config import EvalConfig, OTelConfig, RunnerConfig
from agent_eval.otel.span_mapper import get_span_mapper, parse_opencode_events

pytestmark = pytest.mark.e2e

FIXTURES = Path(__file__).parent / "fixtures"
CASES_DIR = FIXTURES / "otel-smoke-cases"
OPENCODE_CASES_DIR = FIXTURES / "otel-smoke-opencode-cases"


def _load_case(cases_dir, case_id):
    return yaml.safe_load((cases_dir / case_id / "input.yaml").read_text())


class TestClaudeCodeOTel:
    """Validate OTel pipeline: receiver → spans → ClaudeCodeSpanMapper → events."""

    def test_otel_spans_captured(self):
        """Claude Code exports OTel spans that the receiver collects."""
        runner_cls = RUNNERS["claude-code"]
        otel = OTelConfig(enabled=True, content=True)
        runner = runner_cls(otel_config=otel, effort="low")

        case = _load_case(CASES_DIR, "math-001")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            output_dir = Path(tmpdir) / "output"
            output_dir.mkdir()

            result = runner.run_skill(
                skill_name="",
                args=case["prompt"],
                workspace=workspace,
                model="sonnet",
                timeout_s=60,
                output_dir=output_dir,
            )

            assert result.exit_code == 0
            assert case["expected"].lower() in result.stdout.lower()

            otel_path = output_dir / "otel_spans.json"
            assert otel_path.exists(), "otel_spans.json not written"

            data = json.loads(otel_path.read_text())
            resource_spans = data.get("resourceSpans", [])
            assert len(resource_spans) > 0, "No ResourceSpans received"

    def test_span_mapper_produces_events(self):
        """ClaudeCodeSpanMapper converts spans to canonical event format."""
        runner_cls = RUNNERS["claude-code"]
        otel = OTelConfig(enabled=True, content=True)
        runner = runner_cls(otel_config=otel, effort="low")

        case = _load_case(CASES_DIR, "math-002")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            output_dir = Path(tmpdir) / "output"
            output_dir.mkdir()

            result = runner.run_skill(
                skill_name="",
                args=case["prompt"],
                workspace=workspace,
                model="sonnet",
                timeout_s=60,
                output_dir=output_dir,
            )

            assert result.exit_code == 0

            otel_path = output_dir / "otel_spans.json"
            resource_spans = json.loads(otel_path.read_text()).get("resourceSpans", [])

            mapper = get_span_mapper("claude-code")
            events = mapper.map_spans(resource_spans)
            usage = mapper.extract_usage(resource_spans)

            assert len(events) >= 2, f"Expected >=2 events, got {len(events)}"

            types = [e["type"] for e in events]
            assert "system" in types
            assert "result" in types

            assert usage["num_turns"] >= 1
            assert usage["resolved_model"] is not None
            assert usage["token_usage"]["input"] > 0
            assert usage["token_usage"]["output"] > 0

    def test_otel_usage_matches_stream_json(self):
        """OTel-extracted token counts match stream-json extraction."""
        runner_cls = RUNNERS["claude-code"]
        otel = OTelConfig(enabled=True, content=True)
        runner = runner_cls(otel_config=otel, effort="low", log_prefix="e2e")

        case = _load_case(CASES_DIR, "math-001")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            output_dir = Path(tmpdir) / "output"
            output_dir.mkdir()

            result = runner.run_skill(
                skill_name="",
                args=case["prompt"],
                workspace=workspace,
                model="sonnet",
                timeout_s=60,
                output_dir=output_dir,
            )

            assert result.exit_code == 0

            otel_path = output_dir / "otel_spans.json"
            resource_spans = json.loads(otel_path.read_text()).get("resourceSpans", [])
            mapper = get_span_mapper("claude-code")
            otel_usage = mapper.extract_usage(resource_spans)

            assert otel_usage["token_usage"]["input"] == result.token_usage["input"]
            assert otel_usage["token_usage"]["output"] == result.token_usage["output"]
            assert otel_usage["num_turns"] == result.num_turns


@pytest.mark.skipif(
    not os.environ.get("GOOGLE_CLOUD_PROJECT"),
    reason="GOOGLE_CLOUD_PROJECT not set",
)
class TestOpenCodeOTel:
    """Validate OpenCode runner with JSON event fallback."""

    def test_json_events_extracted(self):
        """OpenCode JSON stdout events are parsed into canonical format."""
        runner_cls = RUNNERS["opencode"]
        otel = OTelConfig(enabled=True)
        runner = runner_cls(otel_config=otel)

        case = _load_case(OPENCODE_CASES_DIR, "math-001")
        model = "google-vertex-anthropic/claude-sonnet-4-6@default"

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            output_dir = Path(tmpdir) / "output"
            output_dir.mkdir()

            result = runner.run_skill(
                skill_name="",
                args=case["prompt"],
                workspace=workspace,
                model=model,
                timeout_s=60,
                output_dir=output_dir,
            )

            assert result.exit_code == 0
            assert case["expected"].lower() in result.stdout.lower()

            events = parse_opencode_events(result.stdout)
            assert len(events) >= 2

            types = [e["type"] for e in events]
            assert "assistant" in types
            assert "result" in types

    def test_usage_extracted_from_events(self):
        """Token usage and cost are extracted from step_finish events."""
        runner_cls = RUNNERS["opencode"]
        otel = OTelConfig(enabled=True)
        runner = runner_cls(otel_config=otel)

        case = _load_case(OPENCODE_CASES_DIR, "math-002")
        model = "google-vertex-anthropic/claude-sonnet-4-6@default"

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            output_dir = Path(tmpdir) / "output"
            output_dir.mkdir()

            result = runner.run_skill(
                skill_name="",
                args=case["prompt"],
                workspace=workspace,
                model=model,
                timeout_s=60,
                output_dir=output_dir,
            )

            assert result.exit_code == 0
            assert result.cost_usd is not None and result.cost_usd > 0
            assert result.num_turns is not None and result.num_turns >= 1
            assert result.token_usage is not None
            assert result.token_usage["output"] > 0
