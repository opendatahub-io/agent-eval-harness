#!/usr/bin/env python3
"""Spec 014 (PR-0): no-key Claude Code CLI probes against a local echo server.

Drives ``claude --print`` with a ``--settings`` env block that points
``ANTHROPIC_BASE_URL`` at a local fake Anthropic endpoint (dummy token, no real key)
and records what the CLI actually sends. Covers checklist rows:

  1   --settings env beats the user's ~/.claude/settings.json (Vertex) — for the CLI,
      its subagent, and its hook children (the requests must arrive HERE, not at Vertex)
  9   single-header ANTHROPIC_CUSTOM_HEADERS is sent on root AND subagent requests
  27  multi-header ANTHROPIC_CUSTOM_HEADERS (newline-separated) — are all lines sent?
  10  PreToolUse hook subprocess inherits the settings-env values
  22  does the CLI call POST /v1/messages/count_tokens, and what does a 404 do?
  23  ``--max-budget-usd 0`` semantics (unlimited vs immediate stop)

The fake endpoint scripts the conversation: turn 1 → tool_use Bash(echo hi) (fires the
hook), turn 2 → tool_use Agent (spawns a subagent, whose own request lands here),
subagent → end_turn, main → end_turn. No secrets involved; the report is safe to share.

    python3 specs/014-openrouter-provider/probes/probe_claude_cli.py [--claude-bin claude] [--out probe_cli_report.json]
"""
import argparse
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

LOG = []          # every request the fake endpoint saw
LOCK = threading.Lock()
STATE = {"n_messages": 0, "agent_tool_name": "Agent"}

INTERESTING_HEADERS = ("authorization", "x-api-key", "anthropic-version", "anthropic-beta", "user-agent",
                       "x-eval-run-id", "http-referer", "x-openrouter-title", "x-title", "x-session-id")


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def stream_reply(model, blocks, stop_reason):
    """Minimal valid Anthropic Messages SSE stream."""
    out = [sse("message_start", {"type": "message_start", "message": {
        "id": f"msg_fake_{STATE['n_messages']}", "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1}}})]
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            out.append(sse("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "text", "text": ""}}))
            out.append(sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                   "delta": {"type": "text_delta", "text": b["text"]}}))
        else:
            out.append(sse("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
            out.append(sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                   "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}}))
        out.append(sse("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                     "usage": {"output_tokens": 20}}))
    out.append(sse("message_stop", {"type": "message_stop"}))
    return b"".join(out)


class Fake(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence
        pass

    def _record(self, body):
        try:
            js = json.loads(body) if body else {}
        except Exception:
            js = {"raw": body[:200].decode(errors="replace")}
        sysmsg = js.get("system")
        systxt = sysmsg if isinstance(sysmsg, str) else " ".join(b.get("text", "") for b in (sysmsg or []) if isinstance(b, dict))
        tools = [t.get("name") for t in js.get("tools") or [] if isinstance(t, dict)]
        rec = {"t": round(time.monotonic() - T0, 2), "method": self.command, "path": self.path,
               "headers": {k: ("<present>" if k in ("authorization", "x-api-key") else v)
                           for k, v in ((k.lower(), v) for k, v in self.headers.items()) if k in INTERESTING_HEADERS},
               "model": js.get("model"), "stream": js.get("stream"), "n_messages": len(js.get("messages") or []),
               "is_subagent_like": ("subagent" in systxt.lower()) or ("Agent" not in tools and bool(tools) and len(tools) < 8),
               "tools": tools[:12], "system_head": systxt[:120]}
        with LOCK:
            LOG.append(rec)
        return js, rec

    def do_GET(self):
        self._record(b"")
        self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    def _read_body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = b""
            while True:
                line = self.rfile.readline().strip()
                size = int(line.split(b";")[0] or b"0", 16) if line else 0
                if size == 0:
                    self.rfile.readline(); break
                out += self.rfile.read(size); self.rfile.readline()
            return out
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        body = self._read_body()
        js, rec = self._record(body)
        path = self.path.split("?", 1)[0]   # Claude Code posts /v1/messages?beta=true
        if path.endswith("/count_tokens"):
            payload = json.dumps({"type": "error", "error": {"type": "not_found_error", "message": "count_tokens not supported"}}).encode()
            self.send_response(404); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        if not path.endswith("/messages"):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        with LOCK:
            STATE["n_messages"] += 1
            k = STATE["n_messages"]
        tools = rec["tools"]
        model = js.get("model") or "fake"
        agent_tool = "Agent" if "Agent" in tools else ("Task" if "Task" in tools else None)
        if rec["is_subagent_like"] or agent_tool is None and k > 2:
            blocks, stop = [{"type": "text", "text": "subagent says hi"}], "end_turn"
        elif k == 1:
            blocks, stop = [{"type": "tool_use", "id": "toolu_hook_1", "name": "Bash", "input": {"command": "echo hi"}}], "tool_use"
        elif k == 2 and agent_tool:
            blocks, stop = [{"type": "tool_use", "id": "toolu_agent_1", "name": agent_tool,
                             "input": {"description": "say hi", "prompt": "Reply with hi.", "subagent_type": "general-purpose"}}], "tool_use"
        else:
            blocks, stop = [{"type": "text", "text": "done"}], "end_turn"
        payload = stream_reply(model, blocks, stop) if js.get("stream") else json.dumps({
            "id": f"msg_fake_{k}", "type": "message", "role": "assistant", "model": model, "content": blocks,
            "stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": 20}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if js.get("stream") else "application/json")
        self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)


def producer_stamp(script_path):
    """Identify the exact script revision that produced a report (git sha of the script's
    last commit if available, else 'uncommitted') plus a report schema version."""
    stamp = {"script": os.path.basename(script_path), "schema": 2, "git_sha": None, "dirty": None}
    try:
        script_path = os.path.abspath(script_path)
        d = os.path.dirname(script_path)
        sha = subprocess.run(["git", "-C", d, "log", "-n", "1", "--format=%h", "--", script_path],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        status = subprocess.run(["git", "-C", d, "status", "--porcelain", "--", script_path],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        stamp["git_sha"], stamp["dirty"] = (sha or None), bool(status)
    except Exception:
        pass
    return stamp



def safe_out(path):
    """Validate an operator-supplied report path: no traversal components, existing parent
    directory. Absolute paths are allowed on purpose (operators write reports wherever they
    keep evidence); what is rejected is `..` and a non-existent/unwritable parent."""
    parts = os.path.normpath(path).split(os.sep)
    if ".." in parts:
        raise SystemExit(f"--out must not contain '..' components: {path!r}")
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(parent):
        raise SystemExit(f"--out parent directory does not exist: {parent!r}")
    return path
# Environment forwarded to the `claude` subprocess: an explicit allowlist of what Claude Code
# and the probe need (PATH/HOME/locale/TLS/proxy settings). Nothing else from the operator's
# shell — in particular no unrelated credentials — reaches the CLI or its descendants.
CLI_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
                     "TERM", "TZ", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR", "NODE_OPTIONS",
                     "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
                     "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")


def cli_env():
    return {k: os.environ[k] for k in CLI_ENV_ALLOWLIST if k in os.environ}


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def run_cli(claude, workdir, settings, prompt, extra_args, timeout=180):
    sp = os.path.join(workdir, "settings.json")
    with open(sp, "w") as f:
        json.dump(settings, f)
    env = cli_env()   # allowlist: no inherited credentials reach the CLI; the fake endpoint needs none
    cmd = [claude, "--print", "--output-format", "stream-json", "--verbose", "--settings", sp] + extra_args
    t0 = time.monotonic()
    try:  # prompt on stdin: --allowedTools is variadic and would swallow a trailing positional prompt
        p = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout, input=prompt)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired.stdout/.stderr may be bytes even with text=True — decode before
        # they reach json.dump, or the whole report is lost (CodeRabbit #220).
        def _s(v):
            return v.decode(errors="replace") if isinstance(v, (bytes, bytearray)) else (v or "")
        rc, out, err = "timeout", _s(e.stdout), _s(e.stderr)
    events = []
    for line in (out or "").splitlines():
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return {"rc": rc, "elapsed_s": round(time.monotonic() - t0, 1), "events": events, "stderr_tail": (err or "")[-800:],
            "event_summary": [{"type": e.get("type"), "subtype": e.get("subtype"), "error": (e.get("error") or e.get("message") if e.get("type") in ("result", "error") and not isinstance(e.get("message"), dict) else None)} for e in events][:12],
            "stdout_tail": (out or "")[-600:]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--out", default="probe_cli_report.json")
    a = ap.parse_args()
    claude = shutil.which(a.claude_bin)
    if not claude:
        print(f"{a.claude_bin} not on PATH"); sys.exit(2)

    global T0
    port = free_port(); T0 = time.monotonic()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"fake Anthropic endpoint on {base} (user settings untouched; Vertex vars overridden only via --settings)")

    tmp = tempfile.mkdtemp(prefix="or-cli-probe-")
    hook_env_file = os.path.join(tmp, "hook_env.json")
    hook_script = os.path.join(tmp, "dump_env.py")
    with open(hook_script, "w") as f:
        f.write("import json,os,sys\nsys.stdin.read()\nkeys=[k for k in os.environ if k.startswith(('ANTHROPIC_','CLAUDE_CODE_USE_','CLOUD_ML_'))]\n"
                "json.dump({k:('<present>' if 'TOKEN' in k or 'KEY' in k else os.environ[k]) for k in keys}, open(%r,'w'))\n" % hook_env_file)
    common_env = {"CLAUDE_CODE_USE_VERTEX": "", "ANTHROPIC_VERTEX_PROJECT_ID": "", "CLOUD_ML_REGION": "",
                  "ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": "dummy-not-a-key", "ANTHROPIC_API_KEY": "",
                  "ANTHROPIC_DEFAULT_OPUS_MODEL": "fake/model", "ANTHROPIC_DEFAULT_SONNET_MODEL": "fake/model",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "fake/model", "CLAUDE_CODE_SUBAGENT_MODEL": "fake/model",
                  "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "producer": producer_stamp(__file__),
              "claude_version": None, "scenarios": {}}
    try:
        report["claude_version"] = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        pass

    # Scenario A: single custom header + hook + subagent  (rows 1, 9, 10, 22)
    LOG.clear(); STATE["n_messages"] = 0
    settings = {"env": dict(common_env, ANTHROPIC_CUSTOM_HEADERS="x-eval-run-id: probe-single"),
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {hook_script}"}]}]}}
    resA = run_cli(claude, tmp, settings, "Run the tool calls you are given.", ["--model", "fake/model", "--max-turns", "6",
                                                                              "--allowedTools", "Bash(echo *),Agent,Task"])
    logA = list(LOG)
    hook_env = json.load(open(hook_env_file)) if os.path.exists(hook_env_file) else None
    msgs = [r for r in logA if r["path"].split("?",1)[0].endswith("/messages")]
    sub = [r for r in msgs if r["is_subagent_like"]]
    ct = [r for r in logA if r["path"].split("?",1)[0].endswith("/count_tokens")]
    report["scenarios"]["A_single_header_hook_subagent"] = {
        "cli_rc": resA["rc"], "elapsed_s": resA["elapsed_s"], "requests_total": len(logA),
        "messages_requests": len(msgs), "subagent_like_requests": len(sub),
        "all_requests_hit_fake_endpoint_not_vertex": len(msgs) > 0,
        "single_header_on_root": any(r["headers"].get("x-eval-run-id") == "probe-single" for r in msgs if not r["is_subagent_like"]),
        "single_header_on_subagent": any(r["headers"].get("x-eval-run-id") == "probe-single" for r in sub) if sub else None,
        "count_tokens_calls": len(ct), "count_tokens_paths": sorted({r["path"] for r in ct}),
        "cli_result_subtype": next((e.get("subtype") for e in resA["events"] if e.get("type") == "result"), None),
        "cli_num_turns": next((e.get("num_turns") for e in resA["events"] if e.get("type") == "result"), None),
        "tool_uses_seen": [b.get("name") for e in resA["events"] if e.get("type") == "assistant"
                           for b in (e.get("message") or {}).get("content", []) if isinstance(b, dict) and b.get("type") == "tool_use"],
        "hook_ran": hook_env is not None,
        "hook_env_has_settings_values": ({k: hook_env.get(k) for k in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_AUTH_TOKEN")} if hook_env else None),
        "request_log": logA[:12], "event_summary": resA["event_summary"], "stdout_tail": resA["stdout_tail"], "stderr_tail": resA["stderr_tail"]}

    # Scenario B: multi-header form  (row 27)
    LOG.clear(); STATE["n_messages"] = 0
    settings = {"env": dict(common_env, ANTHROPIC_CUSTOM_HEADERS="HTTP-Referer: https://example.invalid/aeh\nX-OpenRouter-Title: aeh-probe\nx-eval-run-id: probe-multi")}
    # same scripted tool flow as A so the multi-header form is observed on a SUBAGENT request too
    settings["hooks"] = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {hook_script}"}]}]}
    resB = run_cli(claude, tmp, settings, "Run the tool calls you are given.", ["--model", "fake/model", "--max-turns", "6",
                                                                             "--allowedTools", "Bash(echo *),Agent,Task"])
    msgsB = [r for r in LOG if r["path"].split("?",1)[0].endswith("/messages")]
    subB = [r for r in msgsB if r["is_subagent_like"]]
    three = ("http-referer", "x-openrouter-title", "x-eval-run-id")
    report["scenarios"]["B_multi_header"] = {
        "cli_rc": resB["rc"], "messages_requests": len(msgsB), "subagent_like_requests": len(subB),
        "headers_seen_on_first_request": (msgsB[0]["headers"] if msgsB else None),
        "all_three_present": bool(msgsB) and all(h in msgsB[0]["headers"] for h in three),
        "all_three_present_on_every_root_request": bool(msgsB) and all(all(h in r["headers"] for h in three) for r in msgsB if not r["is_subagent_like"]),
        "all_three_present_on_subagent": (all(all(h in r["headers"] for h in three) for r in subB) if subB else None),
        "request_log": list(LOG)[:8], "event_summary": resB["event_summary"], "stdout_tail": resB["stdout_tail"], "stderr_tail": resB["stderr_tail"]}

    # Scenario C: --max-budget-usd 0  (row 23)
    LOG.clear(); STATE["n_messages"] = 0
    settings = {"env": dict(common_env)}
    resC = run_cli(claude, tmp, settings, "Say hi.", ["--model", "fake/model", "--max-turns", "1", "--max-budget-usd", "0"])
    report["scenarios"]["C_max_budget_zero"] = {
        "cli_rc": resC["rc"], "messages_requests": len([r for r in LOG if r["path"].split("?",1)[0].endswith("/messages")]),
        "cli_result_subtype": next((e.get("subtype") for e in resC["events"] if e.get("type") == "result"), None),
        "interpretation": None, "event_summary": resC["event_summary"], "stderr_tail": resC["stderr_tail"]}
    c = report["scenarios"]["C_max_budget_zero"]
    # The contract under test (checklist row 23): the CLI must REJECT a zero budget before any
    # request. Record the outcome rather than asserting, so the report is always written.
    c["rejected_before_request"] = (c["cli_rc"] != 0 and c["messages_requests"] == 0
                                    and "must be a positive number greater than 0" in (c["stderr_tail"] or ""))
    c["interpretation"] = ("0 = rejected before any request (CLI validation error) — execute.py must omit the flag for a zero/absent cap"
                           if c["rejected_before_request"]
                           else "0 = unlimited (request was made, run completed) — UNEXPECTED for the recorded contract"
                           if c["messages_requests"] > 0 and c["cli_result_subtype"] == "success"
                           else "unexpected outcome: see cli_rc / messages_requests / stderr_tail")

    srv.shutdown(); shutil.rmtree(tmp, ignore_errors=True)
    with open(safe_out(a.out), "w") as f:
        json.dump(report, f, indent=2)
    for k, v in report["scenarios"].items():
        print(f"\n== {k} ==")
        print(json.dumps({kk: vv for kk, vv in v.items() if kk not in ("request_log", "stderr_tail")}, indent=1)[:1800])
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
