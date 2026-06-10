"""Lightweight in-process OTLP/HTTP receiver for eval trace capture.

Starts an HTTP server on a random port, accepts OTLP/HTTP JSON trace
exports at POST /v1/traces, accumulates ResourceSpans payloads, and
writes them to otel_spans.json on shutdown.

No external dependencies — stdlib only.
"""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

log = logging.getLogger(__name__)

_OUTPUT_FILENAME = "otel_spans.json"


class _OTLPHandler(BaseHTTPRequestHandler):
    """Handles OTLP/HTTP JSON trace export requests."""

    def do_POST(self):
        if self.path != "/v1/traces":
            self.send_error(404)
            return

        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            self.send_error(
                415, f"Unsupported content type: {content_type}. "
                     f"Only application/json is supported.")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_error(400, "Invalid JSON")
            return

        resource_spans = payload.get("resourceSpans", [])
        if resource_spans:
            with self.server.lock:
                self.server.resource_spans.extend(resource_spans)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, format, *args):
        pass


class OTLPReceiver:
    """In-process OTLP/HTTP JSON receiver for eval trace capture.

    Usage::

        receiver = OTLPReceiver(output_dir=Path("case-01/"))
        port = receiver.start()
        # ... run agent with OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:{port}
        path = receiver.stop()
        # path == case-01/otel_spans.json
    """

    def __init__(self, output_dir: Path):
        self._output_dir = Path(output_dir)
        self._server = None
        self._thread = None

    def start(self) -> int:
        """Start the receiver server. Returns the assigned port."""
        self._server = HTTPServer(("127.0.0.1", 0), _OTLPHandler)
        self._server.lock = threading.Lock()
        self._server.resource_spans = []

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()

        port = self._server.server_address[1]
        log.debug("OTLP receiver started on port %d", port)
        return port

    @property
    def endpoint(self) -> str:
        """OTLP endpoint URL for agent env vars."""
        if not self._server:
            raise RuntimeError("Receiver not started")
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}"

    @property
    def span_count(self) -> int:
        """Number of ResourceSpans received so far."""
        if not self._server:
            return 0
        with self._server.lock:
            return len(self._server.resource_spans)

    def stop(self, flush_timeout_s: float = 5.0) -> Path:
        """Shutdown server, write collected spans, return output path.

        Blocks up to *flush_timeout_s* for the server thread to finish.
        """
        if not self._server:
            raise RuntimeError("Receiver not started")

        self._server.shutdown()
        self._thread.join(timeout=flush_timeout_s)
        self._server.server_close()

        with self._server.lock:
            resource_spans = list(self._server.resource_spans)

        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / _OUTPUT_FILENAME
        output_path.write_text(json.dumps(
            {"resourceSpans": resource_spans},
            indent=2,
        ))

        log.debug("OTLP receiver stopped: %d ResourceSpans written to %s",
                   len(resource_spans), output_path)

        self._server = None
        self._thread = None
        return output_path
