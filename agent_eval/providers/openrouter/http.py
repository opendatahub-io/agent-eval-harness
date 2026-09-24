"""The one thin HTTP helper every OpenRouter client module uses (spec 014,
Decision 24): stdlib ``urllib.request`` over the default SSL context, which
``agent_eval._bootstrap`` already injects with ``truststore``. Small,
non-streaming JSON calls only. Exceptions never carry headers or keys.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from agent_eval.providers.openrouter.errors import classify, describe

BASE_URL = "https://openrouter.ai/api"
DEFAULT_TIMEOUT_S = 15
USER_AGENT = "agent-eval-harness"


class OpenRouterHTTPError(Exception):
    """A non-2xx answer or a transport failure. ``status`` is ``None`` for
    transport errors; ``error_type`` is OpenRouter's own type when the body
    carried one; ``retry_after`` is the parsed header in seconds."""

    def __init__(self, status: Optional[int], message: str, *, error_type: Optional[str] = None,
                 retry_after: Optional[float] = None, url: Optional[str] = None):
        super().__init__(f"HTTP {status}: {message}" if status else f"request failed: {message}")
        self.status = status
        self.message = message
        self.error_type = error_type
        self.retry_after = retry_after
        self.url = url

    @property
    def error_class(self):
        return classify(self.error_type, self.status, self.message)


def _parse_retry_after(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _error_from_body(status: int, body: bytes, headers, url: str) -> OpenRouterHTTPError:
    error_type = None
    message = ""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            meta = err.get("metadata") or {}
            error_type = (err.get("type") or meta.get("type") or
                          (meta.get("raw") or {}).get("type") if isinstance(meta, dict) else None)
            if not error_type and isinstance(err.get("code"), str):
                error_type = err["code"]
        elif isinstance(err, str):
            message = err
    except (ValueError, AttributeError):
        message = body[:200].decode("utf-8", errors="replace")
    if not message:
        message = f"status {status}"
    return OpenRouterHTTPError(status, describe(status, message), error_type=error_type,
                               retry_after=_parse_retry_after(headers.get("Retry-After")),
                               url=url)


def request_json(method: str, url: str, *, key: Optional[str] = None, body=None,
                 timeout: float = DEFAULT_TIMEOUT_S, headers: Optional[dict] = None):
    """Issue one JSON request. Returns the decoded body (``{}`` for an empty
    2xx). Raises :class:`OpenRouterHTTPError` on non-2xx and transport errors;
    the exception text never includes the request headers."""
    req_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    if key:
        req_headers["Authorization"] = f"Bearer {key}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https only
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        raise _error_from_body(exc.code, raw, exc.headers or {}, url) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise OpenRouterHTTPError(None, str(reason)[:200], error_type="timeout"
                                  if isinstance(exc, (socket.timeout, TimeoutError)) else None,
                                  url=url) from None
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise OpenRouterHTTPError(None, f"non-JSON response: {exc}", url=url) from None


def get_json(url: str, *, key: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT_S,
             params: Optional[dict] = None, headers: Optional[dict] = None):
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    return request_json("GET", url, key=key, timeout=timeout, headers=headers)


def post_json(url: str, body, *, key: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT_S):
    return request_json("POST", url, key=key, body=body, timeout=timeout)


def delete(url: str, *, key: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT_S):
    return request_json("DELETE", url, key=key, timeout=timeout)
