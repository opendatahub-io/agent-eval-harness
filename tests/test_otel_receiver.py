"""Tests for the in-process OTLP/HTTP receiver."""

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_eval.otel.receiver import OTLPReceiver


@pytest.fixture
def receiver(tmp_path):
    """Provide a started receiver that auto-stops after the test."""
    r = OTLPReceiver(output_dir=tmp_path)
    r.start()
    yield r
    try:
        r.stop(flush_timeout_s=2)
    except RuntimeError:
        pass  # already stopped


def _post_traces(endpoint, resource_spans):
    """Send a /v1/traces POST with the given ResourceSpans list."""
    payload = json.dumps({"resourceSpans": resource_spans}).encode()
    req = urllib.request.Request(
        f"{endpoint}/v1/traces",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


def _make_resource_span(service_name="test", span_name="test.span"):
    """Build a minimal OTLP ResourceSpans object."""
    return {
        "resource": {
            "attributes": [
                {"key": "service.name", "value": {"stringValue": service_name}},
            ],
        },
        "scopeSpans": [{
            "scope": {"name": "test"},
            "spans": [{
                "traceId": "0" * 32,
                "spanId": "0" * 16,
                "name": span_name,
                "kind": 1,
                "startTimeUnixNano": "1000000000",
                "endTimeUnixNano": "2000000000",
                "attributes": [],
                "status": {},
            }],
        }],
    }


class TestOTLPReceiverLifecycle:

    def test_start_assigns_port(self, tmp_path):
        r = OTLPReceiver(output_dir=tmp_path)
        port = r.start()
        assert isinstance(port, int)
        assert port > 0
        r.stop()

    def test_endpoint_property(self, receiver):
        assert receiver.endpoint.startswith("http://127.0.0.1:")

    def test_endpoint_before_start_raises(self, tmp_path):
        r = OTLPReceiver(output_dir=tmp_path)
        with pytest.raises(RuntimeError):
            _ = r.endpoint

    def test_stop_before_start_raises(self, tmp_path):
        r = OTLPReceiver(output_dir=tmp_path)
        with pytest.raises(RuntimeError):
            r.stop()

    def test_stop_writes_output_file(self, tmp_path):
        r = OTLPReceiver(output_dir=tmp_path)
        r.start()
        path = r.stop()
        assert path.exists()
        data = json.loads(path.read_text())
        assert "resourceSpans" in data

    def test_stop_creates_output_dir(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        r = OTLPReceiver(output_dir=nested)
        r.start()
        path = r.stop()
        assert path.exists()


class TestOTLPReceiverTraces:

    def test_single_post(self, receiver, tmp_path):
        rs = _make_resource_span()
        status = _post_traces(receiver.endpoint, [rs])
        assert status == 200
        assert receiver.span_count == 1

        path = receiver.stop()
        data = json.loads(path.read_text())
        assert len(data["resourceSpans"]) == 1
        assert data["resourceSpans"][0]["resource"] == rs["resource"]

    def test_multiple_posts(self, receiver, tmp_path):
        for i in range(5):
            _post_traces(receiver.endpoint, [_make_resource_span(span_name=f"span-{i}")])
        assert receiver.span_count == 5

        path = receiver.stop()
        data = json.loads(path.read_text())
        assert len(data["resourceSpans"]) == 5

    def test_batch_post(self, receiver, tmp_path):
        spans = [_make_resource_span(span_name=f"span-{i}") for i in range(3)]
        _post_traces(receiver.endpoint, spans)
        assert receiver.span_count == 3

    def test_empty_resource_spans(self, receiver, tmp_path):
        _post_traces(receiver.endpoint, [])
        assert receiver.span_count == 0
        path = receiver.stop()
        data = json.loads(path.read_text())
        assert data["resourceSpans"] == []

    def test_concurrent_posts(self, receiver, tmp_path):
        n = 20
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [
                pool.submit(_post_traces, receiver.endpoint,
                            [_make_resource_span(span_name=f"span-{i}")])
                for i in range(n)
            ]
            for f in futs:
                assert f.result() == 200
        assert receiver.span_count == n


class TestOTLPReceiverErrors:

    def test_wrong_path_returns_404(self, receiver):
        req = urllib.request.Request(
            f"{receiver.endpoint}/v1/metrics",
            data=b'{}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 404

    def test_wrong_content_type_returns_415(self, receiver):
        req = urllib.request.Request(
            f"{receiver.endpoint}/v1/traces",
            data=b'\x00\x01\x02',
            headers={"Content-Type": "application/x-protobuf"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 415

    def test_invalid_json_returns_400(self, receiver):
        req = urllib.request.Request(
            f"{receiver.endpoint}/v1/traces",
            data=b'not json',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400

    def test_span_count_zero_before_any_post(self, tmp_path):
        r = OTLPReceiver(output_dir=tmp_path)
        assert r.span_count == 0
