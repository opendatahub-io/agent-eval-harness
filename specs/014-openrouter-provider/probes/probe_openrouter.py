#!/usr/bin/env python3
"""Spec 014 (PR-0): OpenRouter verification probes — results only, never the key.

Runs the KEY probes from ``specs/014-openrouter-provider/spec.md`` (Verification
checklist) that are executable BEFORE the shaping gateway exists, plus the public
no-key probes. Reads ``OPENROUTER_API_KEY`` from the environment, never prints or
stores it (every error string is redacted), and writes ``probe_report.json``
containing outcomes and evidence only.

    export OPENROUTER_API_KEY=sk-or-...        # in YOUR shell; the script never echoes it
    python3 specs/014-openrouter-provider/probes/probe_openrouter.py                      # all runnable probes
    python3 specs/014-openrouter-provider/probes/probe_openrouter.py --only 3 6 13        # a subset
    python3 specs/014-openrouter-provider/probes/probe_openrouter.py --include-openai     # also probe 8 (gpt-5* judge call)
    python3 specs/014-openrouter-provider/probes/probe_openrouter.py --no-claude          # skip the `claude --print` probe (12)

Cost: ~45 tiny requests on the default model (z-ai/glm-5.3-flash) ≈ a few cents.
Probe 8 (opt-in) adds one judge-shaped call per OpenAI slug. Probe 12 runs one
``claude --print`` turn DIRECTLY against openrouter.ai using a temporary
``--settings`` file (0600, deleted afterwards) that holds the key literally — this is
the direct-mode env template under test; it is the only place the key is written.

stdlib only (no httpx/openai/anthropic needed). Python 3.9+.
"""
import argparse
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://openrouter.ai/api/v1"
KEY = os.environ.get("OPENROUTER_API_KEY", "")
HEADERS_BASE = {"HTTP-Referer": "https://github.com/opendatahub-io/agent-eval-harness",
                "X-Title": "agent-eval-harness spec-014 probes"}


# ---------------------------------------------------------------- helpers

def red(s):
    """Redact the key from any string that might reach the report or stdout."""
    s = str(s)
    return s.replace(KEY, "<REDACTED>") if KEY else s


def _req(method, path, body=None, headers=None, auth="bearer", timeout=120, raw_url=None):
    url = raw_url or (API + path)
    h = dict(HEADERS_BASE)
    h["content-type"] = "application/json"
    if auth == "bearer":
        h["Authorization"] = "Bearer " + KEY
    elif auth == "x-api-key":
        h["x-api-key"] = KEY
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, dict(resp.headers), resp
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        try:
            js = json.loads(txt)
        except Exception:
            js = {"raw": txt[:500]}
        return e.code, dict(e.headers or {}), js
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
        # DNS/connect/TLS/timeout failures: report as status 0 with a transport error instead of
        # raising, so a probe records 'fail'/'error' and the report is still written.
        return 0, {}, {"error": {"type": "transport_error", "message": red(repr(e))[:300]}}


def post_json(path, body, headers=None, auth="bearer"):
    st, hd, r = _req("POST", path, body, headers, auth)
    if hasattr(r, "read"):
        try:
            r = json.loads(r.read().decode(errors="replace"))
        except Exception as e:
            r = {"parse_error": red(e)}
    return st, hd, r


def get_json(path, auth="bearer"):
    st, hd, r = _req("GET", path, None, None, auth)
    if hasattr(r, "read"):
        try:
            r = json.loads(r.read().decode(errors="replace"))
        except Exception as e:
            r = {"parse_error": red(e)}
    return st, hd, r


def post_sse(path, body, headers=None, auth="bearer", timeout=180):
    """Stream a /messages request; return (status, resp_headers, events, comments, elapsed)."""
    body = dict(body, stream=True)
    st, hd, r = _req("POST", path, body, headers, auth, timeout=timeout)
    events, comments = [], []
    if not hasattr(r, "read"):
        return st, hd, [{"event": "error", "data": r}], comments, 0.0
    t0 = time.monotonic()
    ev_name, data_lines = None, []
    for raw in r:
        line = raw.decode(errors="replace").rstrip("\n").rstrip("\r")
        if line.startswith(":"):
            comments.append(line)
            continue
        if line == "":
            if data_lines:
                data = "\n".join(data_lines)
                try:
                    js = json.loads(data)
                except Exception:
                    js = {"raw": data[:300]}
                events.append({"event": ev_name or js.get("type"), "data": js})
            ev_name, data_lines = None, []
            continue
        if line.startswith("event:"):
            ev_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    return st, hd, events, comments, time.monotonic() - t0


def msg_body(model, prompt, max_tokens=32, provider=None, tools=None, tool_choice=None, extra=None):
    b = {"model": model, "max_tokens": max_tokens,
         "messages": [{"role": "user", "content": prompt}]}
    if provider:
        b["provider"] = provider
    if tools:
        b["tools"] = tools
    if tool_choice:
        b["tool_choice"] = tool_choice
    if extra:
        b.update(extra)
    return b


WEATHER_TOOL = {"name": "get_weather", "description": "Get the weather for a city",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}},
                                 "required": ["city"]}}


def fields_of(ev):
    """Which interesting fields an SSE event carries (top-level and under .message)."""
    d = ev.get("data") or {}
    out = []
    for k in ("provider", "openrouter_metadata", "usage"):
        if k in d:
            out.append(k + ("(cost)" if k == "usage" and isinstance(d[k], dict) and "cost" in d[k] else ""))
    m = d.get("message") if isinstance(d.get("message"), dict) else None
    if m:
        for k in ("id", "model", "provider", "usage"):
            if k in m:
                out.append("message." + k)
    return out


def selected_endpoint(metadata):
    """OpenRouter's openrouter_metadata (VERIFIED shape, probe #3 2026-09-16) lists endpoints
    under endpoints.available[] with a boolean `selected`; there is NO endpoints.selected field.
    Returns the selected entry ({provider, model(permaslug), selected}) or None."""
    if not isinstance(metadata, dict):
        return None
    eps = metadata.get("endpoints")
    avail = eps.get("available") if isinstance(eps, dict) else None
    if not isinstance(avail, list):
        return None
    return next((e for e in avail if isinstance(e, dict) and e.get("selected")), None)



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


def has_tool_use(js):
    """True if an Anthropic-format response contains at least one tool_use block."""
    return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in (js.get("content") or []))


def norm_provider(name, catalog):
    """Map a display name or slug to a slug using the public /providers catalog."""
    if not name:
        return None
    n = name.lower()
    for p in catalog:
        if n in (p.get("slug", "").lower(), p.get("name", "").lower()):
            return p.get("slug")
    return n


# ---------------------------------------------------------------- probes

def probe_13(a, R):
    """x-api-key auth path (non-empty ANTHROPIC_API_KEY) on /messages."""
    st, hd, js = post_json("/messages", msg_body(a.model, "Reply with the word ready.", 8), auth="x-api-key")
    ok = st == 200
    R.add(13, "pass" if ok else "fail",
          {"status": st, "works_via_x_api_key": ok,
           "error": red(json.dumps(js.get("error", js))[:300]) if not ok else None},
          note="200 = x-api-key accepted (blank ANTHROPIC_API_KEY is then hygiene, not correctness); "
               "401 with a key present = the x-api-key path is rejected, so the blank is mandatory.")


def probe_3(a, R):
    """SSE placement of provider / openrouter_metadata / usage.cost on /messages."""
    st, hd, evs, comments, _ = post_sse("/messages", msg_body(a.model, "Reply with the word ready.", 8,
                                                              provider={"order": a.providers}),
                                        headers={"X-OpenRouter-Metadata": "enabled"})
    placement = {}
    for ev in evs:
        f = fields_of(ev)
        if f:
            placement.setdefault(ev["event"], sorted(set(f)))
    selected, raw_md = None, None
    for ev in evs:
        md = (ev.get("data") or {}).get("openrouter_metadata")
        if md is not None:
            raw_md = md
            selected = selected_endpoint(md) or selected
    # pass requires the routing metadata AND a selected endpoint (the metadata header was
    # sent), not merely HTTP 200 (CodeRabbit #220).
    R.add(3, "pass" if (st == 200 and raw_md is not None and selected is not None) else ("inconclusive" if st == 200 else "fail"),
          {"status": st, "event_sequence": [e["event"] for e in evs][:14], "field_placement": placement,
           "metadata_header_honoured": raw_md is not None, "endpoints_selected": selected,
           "openrouter_metadata_raw": raw_md,
           "generation_id_header": hd.get("X-Generation-Id") or hd.get("x-generation-id"),
           "keepalive_comments": len(comments)})


def probe_6(a, R, n):
    """usage.cost == /generation total_cost; event carrying cost; /generation latency."""
    rows, mism, lat = [], 0, []
    for i in range(n):
        st, hd, evs, _, _ = post_sse("/messages", msg_body(a.model, f"Say ready ({i}).", 8,
                                                           provider={"order": a.providers}))
        gid = cost = ev_cost = None
        for ev in evs:
            d = ev.get("data") or {}
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            gid = gid or m.get("id") or (d.get("id") if str(d.get("id", "")).startswith("gen-") else None)
            u = d.get("usage") if isinstance(d.get("usage"), dict) else (m.get("usage") if isinstance(m.get("usage"), dict) else None)
            if u and "cost" in u:
                cost, ev_cost = u["cost"], ev["event"]
        t0 = time.monotonic()
        gen, gst = None, None
        while time.monotonic() - t0 < 30:
            gst, _, gen = get_json("/generation?id=" + urllib.parse.quote(str(gid)))
            if gst == 200:
                break
            time.sleep(1.0)
        l = time.monotonic() - t0 if gst == 200 else None
        total = ((gen or {}).get("data") or {}).get("total_cost") if gst == 200 else None
        eq = (cost is not None and total is not None and abs(float(cost) - float(total)) < 1e-9)
        if not eq:
            mism += 1
        if l is not None:
            lat.append(l)
        rows.append({"gen_id_prefix": str(gid)[:4] if gid else None, "usage_cost": cost, "cost_event": ev_cost,
                     "generation_total_cost": total, "generation_status": gst, "generation_latency_s": round(l, 2) if l else None,
                     "equal": eq, "provider_name": ((gen or {}).get("data") or {}).get("provider_name") if gst == 200 else None})
    R.add(6, "pass" if rows and mism == 0 else ("inconclusive" if rows else "fail"),
          {"requests": len(rows), "mismatches": mism, "cost_event": sorted({r["cost_event"] for r in rows if r["cost_event"]}),
           "generation_latency_s": {"min": round(min(lat), 2), "max": round(max(lat), 2)} if lat else None,
           "generation_unavailable_within_30s": sum(1 for r in rows if r["generation_status"] != 200), "sample": rows[:3]})


def probe_4(a, R, catalog):
    """slug vs display name in provider.order (allow_fallbacks:false)."""
    slug = a.providers[0]
    display = next((p["name"] for p in catalog if p.get("slug") == slug), slug.capitalize())
    out = {}
    for form in (slug, display):
        st, hd, js = post_json("/messages", msg_body(a.model, "Say ready.", 8,
                                                     provider={"order": [form], "allow_fallbacks": False}),
                               headers={"X-OpenRouter-Metadata": "enabled"})
        sel = selected_endpoint(js.get("openrouter_metadata")) if st == 200 else None
        out[form] = {"status": st, "provider": js.get("provider") if st == 200 else None, "endpoints_selected": sel,
                     "error": red(json.dumps(js.get("error"))[:200]) if st != 200 else None}
    both = all(v["status"] == 200 for v in out.values())
    R.add(4, "pass" if both else "inconclusive",
          {"slug_form": slug, "display_form": display, "results": out,
           "matching_rule": "both accepted" if both else ("slug only" if out[slug]["status"] == 200 else "display only" if out[display]["status"] == 200 else "neither")})


def probe_5(a, R, catalog, n):
    """order + allow_fallbacks:false vs Auto Exacto on tool-calling requests."""
    allowed = {norm_provider(p, catalog) for p in a.providers}
    configs = [
        ("order+no_fallbacks+require_parameters+forced_tool", {"order": a.providers, "allow_fallbacks": False, "require_parameters": True}, {"type": "tool", "name": "get_weather"}),
        ("order+no_fallbacks+forced_tool", {"order": a.providers, "allow_fallbacks": False}, {"type": "tool", "name": "get_weather"}),
        ("order+no_fallbacks+auto_tool", {"order": a.providers, "allow_fallbacks": False}, {"type": "auto"}),
        ("no_pins+forced_tool", None, {"type": "tool", "name": "get_weather"}),
    ]
    isolation, working = {}, None
    for name, prov, tc in configs:  # 2 requests each to find a configuration that works
        st, hd, js = post_json("/messages", msg_body(a.model, "What is the weather in Paris?", 128, provider=prov,
                                                     tools=[WEATHER_TOOL], tool_choice=tc),
                               headers={"X-OpenRouter-Metadata": "enabled"})
        tool_called = st == 200 and has_tool_use(js)
        isolation[name] = {"status": st, "provider": js.get("provider") if st == 200 else None,
                           "stop_reason": js.get("stop_reason") if st == 200 else None, "tool_called": tool_called,
                           "error": red(json.dumps(js.get("error", js)))[:300] if st != 200 else None}
        if tool_called and working is None and prov is not None:
            working = (name, prov, tc)
    tally, errors, no_tool_call, first_err = {}, 0, 0, None
    if working:
        name, prov, tc = working
        for i in range(n):
            st, hd, js = post_json("/messages", msg_body(a.model, f"What is the weather in Paris? (#{i})", 128, provider=prov,
                                                         tools=[WEATHER_TOOL], tool_choice=tc),
                                   headers={"X-OpenRouter-Metadata": "enabled"})
            if st != 200:
                errors += 1
                first_err = first_err or red(json.dumps(js.get("error", js)))[:300]
                continue
            if not has_tool_use(js):   # a text-only answer did not exercise the tool-calling route
                no_tool_call += 1
                continue
            p = norm_provider(js.get("provider"), catalog)
            tally[p] = tally.get(p, 0) + 1
    outside = {k: v for k, v in tally.items() if k not in allowed}
    R.add(5, "pass" if tally and not outside and errors == 0 and no_tool_call == 0 else ("fail" if outside else "inconclusive"),
          {"isolation": isolation, "config_used_for_tally": working[0] if working else None, "requests": n if working else 0,
           "errors": errors, "responses_without_tool_call": no_tool_call, "first_error": first_err,
           "served_by": tally, "allowed": sorted(allowed), "outside_order_list": outside})


def probe_7(a, R, settle):
    """GET /key usage settle time after one request."""
    def _usage(st, k):
        u = ((k or {}).get("data") or {}).get("usage") if st == 200 else None
        return u if isinstance(u, (int, float)) else None
    st0, _, k0 = get_json("/key")
    u0 = _usage(st0, k0)
    pst, _, pjs = post_json("/messages", msg_body(a.model, "Say ready.", 8, provider={"order": a.providers}))
    if u0 is None or pst != 200:
        R.add(7, "fail", {"key_endpoint_status": st0, "usage_before": u0, "request_status": pst,
                          "error": red(json.dumps(pjs.get("error", pjs)))[:300] if pst != 200 else "no numeric usage from /key"})
        return
    t0, last_change, last_val, failed_reads = time.monotonic(), None, u0, 0
    while time.monotonic() - t0 < settle:
        time.sleep(5)
        st, _, k = get_json("/key")
        u = _usage(st, k)
        if u is None:            # transient /key error: never counts as a change
            failed_reads += 1
            continue
        if u != last_val:
            last_change, last_val = round(time.monotonic() - t0, 1), u
    R.add(7, "pass" if last_change is not None else "inconclusive",
          {"key_endpoint_status": st0, "request_status": pst, "usage_before": u0, "usage_after": last_val,
           "seconds_until_last_change": last_change, "window_s": settle, "failed_key_reads": failed_reads,
           "note": "observed-only: the change is attributed to this request by timing, not by id "
                   "(unrelated account activity in the window would also move the counter); "
                   "None = no change observed in the window"})


def probe_8(a, R, models):
    """judge-shaped chat/completions: forced tool_choice + max_tokens on openai/gpt-5* / o3*."""
    out = {}
    for m in models:
        body = {"model": m, "max_tokens": 64,
                "messages": [{"role": "user", "content": "Grade this: 'ok'. Call the tool."}],
                "tools": [{"type": "function", "function": {"name": "grade", "parameters": {
                    "type": "object", "properties": {"rationale": {"type": "string"}, "score": {"type": "integer"}},
                    "required": ["rationale", "score"]}}}],
                "tool_choice": {"type": "function", "function": {"name": "grade"}}}
        st, hd, js = post_json("/chat/completions", body)
        args = None
        try:
            args = js["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        except Exception:
            pass
        out[m] = {"status": st, "arguments_type": type(args).__name__ if args is not None else None,
                  "arguments_is_json_string": isinstance(args, str) and _is_json(args),
                  "usage_cost": (js.get("usage") or {}).get("cost") if st == 200 else None,
                  "error": red(json.dumps(js.get("error"))[:200]) if st != 200 else None}
    ok = bool(out) and all(v["status"] == 200 and v["arguments_is_json_string"] for v in out.values())
    R.add(8, "pass" if ok else ("skipped" if not out else "fail"), {"results": out, **({"reason": "empty --openai-models"} if not out else {})})


def _is_json(s):
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def probe_16(a, R):
    """keep-alive comment lines on a long streaming request."""
    st, hd, evs, comments, el = post_sse("/messages", msg_body(a.model, "Write a numbered list of 150 distinct animals, one per line.", 700,
                                                              provider={"order": a.providers}))
    R.add(16, "pass" if (st == 200 and comments) else ("inconclusive" if st == 200 else "fail"),
          {"status": st, "elapsed_s": round(el, 1), "keepalive_comments": len(comments),
           "comment_samples": [c[:40] for c in comments[:3]], "events": len(evs)},
          note="pass = keep-alive comments observed; inconclusive = 200 but none observed (fast stream, "
               "no heartbeat); the local echo-server half lives in probe_claude_cli.py.")


def probe_19(a, R):
    """model-id forms on /messages: bare, :exacto variant, [1m] marker — echoed model + status."""
    out = {}
    for form in (a.model, a.model + ":exacto", a.model + "[1m]"):
        st, hd, js = post_json("/messages", msg_body(form, "Say ready.", 8, provider={"order": a.providers}),
                               headers={"X-OpenRouter-Metadata": "enabled"})
        out[form] = {"status": st, "echoed_model": js.get("model") if st == 200 else None,
                     "provider": js.get("provider") if st == 200 else None,
                     "error": red(json.dumps(js.get("error"))[:200]) if st != 200 else None}
    R.add(19, "pass" if all(v["status"] == 200 for v in out.values()) else "inconclusive", {"results": out},
          note="API-side half (no gateway): what OpenRouter echoes per form; the gateway/CLI half needs PR-5.")


def probe_20(a, R):
    """Claude Code's anthropic-beta headers against a non-Anthropic model."""
    st, hd, js = post_json("/messages", msg_body(a.model, "Say ready.", 8, provider={"order": a.providers}),
                           headers={"anthropic-beta": "interleaved-thinking-2025-05-14,context-1m-2025-08-07",
                                    "anthropic-version": "2023-06-01"})
    R.add(20, "pass" if st == 200 else "fail", {"status": st, "error": red(json.dumps(js.get("error"))[:200]) if st != 200 else None})


def probe_12(a, R):
    """Direct-mode `claude --print`: gen- ids in stream-json, model echo, modelUsage keys, /generation 200."""
    claude = shutil.which(a.claude_bin)
    if not claude:
        R.add(12, "skipped", {"reason": f"{a.claude_bin} not on PATH"})
        return
    tmp = tempfile.mkdtemp(prefix="or-probe12-")
    try:
        settings = {"env": {
            "CLAUDE_CODE_USE_VERTEX": "", "ANTHROPIC_VERTEX_PROJECT_ID": "", "CLOUD_ML_REGION": "",
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "ANTHROPIC_AUTH_TOKEN": KEY, "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": a.model, "ANTHROPIC_DEFAULT_SONNET_MODEL": a.model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": a.model, "CLAUDE_CODE_SUBAGENT_MODEL": a.model}}
        sp = os.path.join(tmp, "settings.json")
        fd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f)
        env = cli_env()   # allowlist; the credential reaches the CLI only via the 0600 settings file
        cmd = [claude, "--print", "--output-format", "stream-json", "--verbose", "--max-turns", "1",
               "--settings", sp, "--model", a.model, "Reply with exactly the word: ready"]
        t0 = time.monotonic()
        p = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, timeout=240)
        ids, models, usage_keys, cost, result_sub, gen_cost = [], set(), [], None, None, None
        for line in p.stdout.splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            m = ev.get("message") if isinstance(ev.get("message"), dict) else None
            if ev.get("type") == "assistant" and m:
                if m.get("id"):
                    ids.append(m["id"])
                if m.get("model"):
                    models.add(m["model"])
            if ev.get("type") == "result":
                usage_keys = sorted((ev.get("modelUsage") or {}).keys())
                cost, result_sub = ev.get("total_cost_usd"), ev.get("subtype")
        gen_ok, gen_lat = None, None
        gen_ids = [i for i in ids if str(i).startswith("gen-")]
        if gen_ids:
            t1 = time.monotonic()
            while time.monotonic() - t1 < 45:  # /generation lags ~8-13s after the request (probe 6)
                gst, _, gj = get_json("/generation?id=" + urllib.parse.quote(gen_ids[-1]))
                if gst == 200:
                    gen_ok, gen_lat = True, round(time.monotonic() - t1, 1)
                    gen_cost = ((gj or {}).get("data") or {}).get("total_cost")
                    break
                time.sleep(2)
            gen_ok = bool(gen_ok)
        status = ("fail" if p.returncode != 0 else "pass" if (gen_ids and gen_ok) else "inconclusive")
        R.add(12, status,
              {"exit_code": p.returncode, "elapsed_s": round(time.monotonic() - t0, 1), "result_subtype": result_sub,
               "message_id_prefixes": sorted({str(i).split("-")[0].split("_")[0] for i in ids}),
               "gen_ids_seen": len(gen_ids), "generation_lookup_200": gen_ok, "generation_lookup_latency_s": gen_lat,
               "generation_total_cost_last_turn": (gen_cost if gen_ok else None),
               "assistant_message_model": sorted(models), "modelUsage_keys": usage_keys,
               "claude_total_cost_usd_estimate": cost, "stderr_tail": red(p.stderr[-600:])},
              note="Also exercises probe 1 (settings env beats user Vertex) and probe 20 (beta headers) end-to-end.")
    except subprocess.TimeoutExpired:
        R.add(12, "fail", {"error": "claude --print timed out after 240s"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def probe_18(a, R):
    """(no key) endpoint status enum values for the model under test."""
    st, _, js = get_json(f"/models/{a.model}/endpoints", auth=None)
    eps = ((js or {}).get("data") or {}).get("endpoints") or []
    R.add(18, "pass" if st == 200 else "fail",
          {"status": st, "endpoints": len(eps),
           "status_values": sorted({e.get("status") for e in eps}, key=lambda x: (x is None, x)),
           "per_provider": [{"provider": e.get("provider_name"), "quant": e.get("quantization"), "status": e.get("status"),
                             "uptime_30m": e.get("uptime_last_30m"), "max_out": e.get("max_completion_tokens")} for e in eps][:12]})


def probe_17(a, R):
    """(no key) TLS to openrouter.ai with the default trust store (corporate CA check)."""
    try:
        ctx = ssl.create_default_context()
        c = http.client.HTTPSConnection("openrouter.ai", 443, context=ctx, timeout=15)
        c.request("GET", "/api/v1/providers")
        r = c.getresponse()
        R.add(17, "pass" if r.status == 200 else "fail", {"status": r.status, "tls_ok": True})
    except Exception as e:
        R.add(17, "fail", {"tls_ok": False, "error": red(e)})


def producer_stamp(script_path):
    """Identify the exact script revision that produced a report (git sha of the script's
    last commit if available, else None) plus a report schema version. Reports produced by
    an older revision are historical evidence; see specs/014-openrouter-provider/probes/README.md."""
    import subprocess as _sp
    stamp = {"script": os.path.basename(script_path), "schema": 2, "git_sha": None, "dirty": None}
    try:
        script_path = os.path.abspath(script_path)
        d = os.path.dirname(script_path)
        sha = _sp.run(["git", "-C", d, "log", "-n", "1", "--format=%h", "--", script_path],
                      capture_output=True, text=True, timeout=10).stdout.strip()
        status = _sp.run(["git", "-C", d, "status", "--porcelain", "--", script_path],
                         capture_output=True, text=True, timeout=10).stdout.strip()
        stamp["git_sha"], stamp["dirty"] = (sha or None), bool(status)
    except Exception:
        pass
    return stamp


# ---------------------------------------------------------------- report

class Report:
    def __init__(self):
        self.probes = []

    def add(self, pid, status, evidence, note=None):
        row = {"id": pid, "status": status, "evidence": evidence}
        if note:
            row["note"] = note
        self.probes.append(row)
        print(f"  probe {pid:>2}: {status.upper():13} {red(json.dumps(evidence))[:150]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="z-ai/glm-5.3-flash", help="cheap model with >=2 providers")
    ap.add_argument("--providers", default="z-ai,novita", help="comma list of provider slugs for order pins")
    ap.add_argument("--only", nargs="*", type=int, default=None, help="probe ids to run")
    ap.add_argument("--n5", type=int, default=20, help="tool-calling requests for probe 5")
    ap.add_argument("--n6", type=int, default=10, help="requests for probe 6")
    ap.add_argument("--settle-secs", type=int, default=120, help="probe 7 window")
    ap.add_argument("--include-openai", action="store_true", help="run probe 8 (paid OpenAI judge-shaped calls)")
    ap.add_argument("--openai-models", default="openai/gpt-5.2", help="comma list for probe 8")
    ap.add_argument("--no-claude", action="store_true", help="skip probe 12 (claude --print direct run)")
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--out", default="probe_report.json")
    a = ap.parse_args()
    a.providers = [p.strip() for p in a.providers.split(",") if p.strip()]

    R = Report()
    want = lambda i: a.only is None or i in a.only
    print(f"OpenRouter spec-014 probes — model={a.model} providers={a.providers} key_present={bool(KEY)}")

    for pid, fn in ((17, lambda: probe_17(a, R)), (18, lambda: probe_18(a, R))):
        if want(pid):
            try:
                fn()
            except Exception as e:
                R.add(pid, "error", {"error": red(repr(e))[:300]})
    try:
        st, _, cat = get_json("/providers", auth=None)
    except Exception as e:
        st, cat = 0, {"error": red(repr(e))[:300]}
    catalog = ((cat or {}).get("data") or []) if st == 200 else []
    if st != 200:
        print(f"WARNING: /providers catalog unavailable (status {st}); provider-name normalisation degraded.")

    if not KEY:
        print("OPENROUTER_API_KEY not set — KEY probes skipped (3,4,5,6,7,8,12,13,16,19,20).")
    else:
        for pid, fn in ((13, lambda: probe_13(a, R)), (3, lambda: probe_3(a, R)), (6, lambda: probe_6(a, R, a.n6)),
                        (4, lambda: probe_4(a, R, catalog)), (5, lambda: probe_5(a, R, catalog, a.n5)),
                        (19, lambda: probe_19(a, R)), (20, lambda: probe_20(a, R)), (16, lambda: probe_16(a, R)),
                        (7, lambda: probe_7(a, R, a.settle_secs))):
            if want(pid):
                try:
                    fn()
                except Exception as e:  # never let one probe abort the report
                    R.add(pid, "error", {"error": red(repr(e))[:300]})
        if want(8) and a.include_openai:
            try:
                probe_8(a, R, [m.strip() for m in a.openai_models.split(",") if m.strip()])
            except Exception as e:
                R.add(8, "error", {"error": red(repr(e))[:300]})
        if want(12) and not a.no_claude:
            try:
                probe_12(a, R)
            except Exception as e:
                R.add(12, "error", {"error": red(repr(e))[:300]})

    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "producer": producer_stamp(__file__),
              "model": a.model, "providers": a.providers,
              "key_present": bool(KEY), "probes": R.probes}
    with open(safe_out(a.out), "w") as f:
        json.dump(json.loads(red(json.dumps(report))), f, indent=2)
    print(f"\nwrote {a.out} ({len(R.probes)} probes) — contains outcomes only; safe to share.")


if __name__ == "__main__":
    main()
