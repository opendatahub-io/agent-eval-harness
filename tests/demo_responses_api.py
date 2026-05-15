#!/usr/bin/env python3
"""Interactive demo: run ResponsesAPIRunner against a local mock Responses API.

Simulates the full eval harness flow with 3 realistic test cases:
  1. text-summarize — summarize a paragraph
  2. code-generate  — write a Python function
  3. bug-fix        — fix a broken script

Each case goes through the complete 7-step lifecycle:
  skill upload → container create → workspace upload → execute →
  result download → container cleanup → RunResult

Run:
    python tests/demo_responses_api.py
"""

import json
import os
import sys
import uuid
import threading
import tempfile
import textwrap
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.agent.responses_api import ResponsesAPIRunner, _global_skill_cache
from agent_eval.agent.base import RunResult

# ---------------------------------------------------------------------------
# Enhanced mock server — case-aware, verbose
# ---------------------------------------------------------------------------

_skills: dict[str, dict] = {}
_containers: dict[str, dict] = {}
_files: dict[str, dict[str, dict]] = {}

CASE_RESPONSES = {
    "text-summarize": {
        "text": (
            "The input text discusses climate change and its effects on "
            "global ecosystems. Key points: rising temperatures, melting "
            "ice caps, and biodiversity loss. The author argues for immediate "
            "policy intervention."
        ),
        "output_file": "output/summary.txt",
        "output_content": (
            "SUMMARY\n"
            "=======\n"
            "Climate change threatens global ecosystems through rising temperatures,\n"
            "melting ice caps, and biodiversity loss. Immediate policy action recommended.\n"
        ),
    },
    "code-generate": {
        "text": (
            "Here is the implementation:\n\n"
            "```python\n"
            "def fibonacci(n: int) -> list[int]:\n"
            '    \"\"\"Return the first n Fibonacci numbers.\"\"\"\n'
            "    if n <= 0:\n"
            "        return []\n"
            "    seq = [0, 1]\n"
            "    while len(seq) < n:\n"
            "        seq.append(seq[-1] + seq[-2])\n"
            "    return seq[:n]\n"
            "```\n"
        ),
        "output_file": "output/fibonacci.py",
        "output_content": (
            "def fibonacci(n: int) -> list[int]:\n"
            '    """Return the first n Fibonacci numbers."""\n'
            "    if n <= 0:\n"
            "        return []\n"
            "    seq = [0, 1]\n"
            "    while len(seq) < n:\n"
            "        seq.append(seq[-1] + seq[-2])\n"
            "    return seq[:n]\n"
        ),
    },
    "bug-fix": {
        "text": (
            "Found the bug: the loop condition used `<=` instead of `<`, "
            "causing an off-by-one IndexError. Fixed line 5 and added a "
            "bounds check."
        ),
        "output_file": "output/fixed_script.py",
        "output_content": (
            "def process_items(items: list) -> list:\n"
            '    """Process items safely with bounds checking."""\n'
            "    results = []\n"
            "    for i in range(len(items)):\n"  # fixed: was range(len(items)+1)
            "        results.append(items[i] * 2)\n"
            "    return results\n"
        ),
    },
}


def _detect_case(prompt: str) -> str:
    """Match the case by looking at the skill args in the prompt.

    The runner sends "/{skill_name} {args}" as the user message, so
    we match on the args pattern rather than input.yaml content.
    """
    p = prompt.lower()
    if "--format" in p or "bullet" in p:
        return "text-summarize"
    if "--language" in p or "python" in p:
        return "code-generate"
    if "--auto-test" in p or "fix" in p:
        return "bug-fix"
    return "text-summarize"


class DemoHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        msg = fmt % args
        print(f"    [mock-api] {msg}", flush=True)

    def _json(self, data, status=200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _parse_multipart(self, body, content_type):
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip('"')
        if not boundary:
            return "/workspace/unknown", body
        parts = body.split(f"--{boundary}".encode())
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            headers_section = part[:header_end].decode("utf-8", errors="replace")
            content = part[header_end + 4:]
            if content.endswith(b"\r\n"):
                content = content[:-2]
            filename = "/workspace/unknown"
            for line in headers_section.split("\r\n"):
                if "filename=" in line:
                    start = line.index('filename="') + len('filename="')
                    end = line.index('"', start)
                    filename = line[start:end]
                    break
            if b'name="file"' in part:
                return filename, content
        return "/workspace/unknown", body

    # -- GET --

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/v1/models":
            self._json({"object": "list", "data": [
                {"id": "mock-gpt-4o", "object": "model", "owned_by": "mock-openai"},
                {"id": "mock-llama-3", "object": "model", "owned_by": "mock-ogx"},
            ]})
            return

        if "/files/" in path and path.endswith("/content"):
            parts = path.split("/")
            cid, fid = parts[3], parts[5]
            f = _files.get(cid, {}).get(fid)
            if not f:
                self._json({"detail": "Not Found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(f["content"])))
            self.end_headers()
            self.wfile.write(f["content"])
            return

        if path.startswith("/v1/containers/") and path.endswith("/files"):
            cid = path.split("/")[3]
            container_files = _files.get(cid, {})
            fl = [{"id": fid, "object": "container_file",
                   "path": f["path"], "size": len(f["content"])}
                  for fid, f in container_files.items()]
            self._json({"object": "list", "data": fl,
                        "first_id": fl[0]["id"] if fl else None,
                        "last_id": fl[-1]["id"] if fl else None,
                        "has_more": False})
            return

        self._json({"detail": "Not Found"}, 404)

    # -- POST --

    def do_POST(self):
        path = self.path
        body = self._body()

        if path == "/v1/skills":
            sid = f"skill_{uuid.uuid4().hex[:8]}"
            _skills[sid] = {"id": sid}
            self._json({"id": sid, "object": "skill", "name": "eval-skill"})
            return

        if path == "/v1/containers":
            cid = f"ctr_{uuid.uuid4().hex[:8]}"
            data = json.loads(body) if body else {}
            _containers[cid] = {
                "id": cid,
                "name": data.get("name", ""),
                "status": "running",
                "memory_limit": data.get("memory_limit", "1g"),
            }
            _files[cid] = {}
            self._json({"id": cid, "object": "container", "status": "running"})
            return

        if path.startswith("/v1/containers/") and path.endswith("/files"):
            cid = path.split("/")[3]
            ct = self.headers.get("Content-Type", "")
            fid = f"file_{uuid.uuid4().hex[:8]}"
            fp, fc = ("/workspace/unknown", body)
            if "multipart" in ct:
                fp, fc = self._parse_multipart(body, ct)
            _files.setdefault(cid, {})[fid] = {"path": fp, "content": fc}
            self._json({"id": fid, "object": "container_file",
                        "path": fp, "size": len(fc)})
            return

        if path == "/v1/responses":
            data = json.loads(body) if body else {}
            model = data.get("model", "mock-gpt-4o")

            prompt = ""
            for msg in data.get("input", []):
                if msg.get("role") == "user":
                    prompt = msg.get("content", "")

            case_key = _detect_case(prompt)
            case_data = CASE_RESPONSES[case_key]

            for tool in data.get("tools", []):
                cid = tool.get("environment", {}).get("container_id")
                if cid and cid in _files:
                    ofid = f"file_{uuid.uuid4().hex[:8]}"
                    _files[cid][ofid] = {
                        "path": f"/workspace/{case_data['output_file']}",
                        "content": case_data["output_content"].encode(),
                    }

            rid = f"resp_{uuid.uuid4().hex[:8]}"
            self._json({
                "id": rid,
                "object": "response",
                "status": "completed",
                "model": model,
                "output": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": case_data["text"],
                    }],
                }],
                "usage": {
                    "prompt_tokens": 250 + len(prompt),
                    "completion_tokens": 80 + len(case_data["text"]),
                    "total_tokens": 330 + len(prompt) + len(case_data["text"]),
                },
            })
            return

        self._json({"detail": "Not Found"}, 404)

    # -- DELETE --

    def do_DELETE(self):
        path = self.path
        if path.startswith("/v1/containers/"):
            cid = path.split("/")[3]
            _containers.pop(cid, None)
            _files.pop(cid, None)
            self._json({"deleted": True})
            return
        self._json({"detail": "Not Found"}, 404)


# ---------------------------------------------------------------------------
# Test cases — realistic eval scenarios
# ---------------------------------------------------------------------------

CASES = [
    {
        "id": "text-summarize",
        "skill_args": "--format bullet-points",
        "input_yaml": textwrap.dedent("""\
            prompt: >
              Summarize the following text in 3 bullet points.
            text: >
              Climate change is one of the most pressing challenges facing
              humanity. Rising global temperatures are causing ice caps to melt,
              sea levels to rise, and weather patterns to become more extreme.
              These changes threaten biodiversity, food security, and human
              health. Scientists urge immediate and coordinated policy action
              to reduce greenhouse gas emissions and transition to renewable
              energy sources.
        """),
        "extra_files": {},
    },
    {
        "id": "code-generate",
        "skill_args": "--language python",
        "input_yaml": textwrap.dedent("""\
            prompt: >
              Generate a Python function called fibonacci that takes an
              integer n and returns a list of the first n Fibonacci numbers.
              Include a docstring and handle edge cases.
        """),
        "extra_files": {},
    },
    {
        "id": "bug-fix",
        "skill_args": "--auto-test",
        "input_yaml": textwrap.dedent("""\
            prompt: >
              Fix the bug in the script below. It crashes with an
              IndexError on large inputs.
            source_file: broken_script.py
        """),
        "extra_files": {
            "broken_script.py": textwrap.dedent("""\
                def process_items(items: list) -> list:
                    results = []
                    for i in range(len(items) + 1):  # BUG: off-by-one
                        results.append(items[i] * 2)
                    return results
            """),
        },
    },
]


# ---------------------------------------------------------------------------
# SKILL.md — the skill the runner will upload
# ---------------------------------------------------------------------------

SKILL_MD = textwrap.dedent("""\
    # eval-demo

    A demo skill for testing the Responses API runner.

    Accepts a prompt from input.yaml and produces output files.

    ## Usage

    /{skill_name} [args]

    Reads input.yaml from the workspace, processes the prompt,
    and writes results to the output/ directory.
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _separator(char="=", width=72):
    print(char * width)

def _header(text):
    _separator()
    print(f"  {text}")
    _separator()

def _step(num, text):
    print(f"\n  Step {num}: {text}")
    print(f"  {'─' * 60}")


def main():
    _header("Responses API Runner — Full Lifecycle Demo")
    print()
    print("  This demo runs the ResponsesAPIRunner against a local mock")
    print("  Responses API server. Every API call is real HTTP — the same")
    print("  code path that would execute against OpenAI or OGX.")
    print()

    # Clear global skill cache from any previous runs
    _global_skill_cache.clear()

    # Start mock server
    _skills.clear()
    _containers.clear()
    _files.clear()
    server = HTTPServer(("127.0.0.1", 0), DemoHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"  Mock Responses API server running on http://127.0.0.1:{port}")
    print()

    runner = ResponsesAPIRunner(
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="mock-key-for-demo",
        default_model="mock-gpt-4o",
        memory_limit_mb=512,
        log_prefix="demo",
    )

    results: list[tuple[str, RunResult]] = []

    with tempfile.TemporaryDirectory(prefix="eval-demo-") as tmpdir:
        base = Path(tmpdir)

        # Create the skill directory (shared across cases)
        skill_dir = base / "skills" / "eval-demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_MD)

        for i, case in enumerate(CASES, 1):
            case_id = case["id"]
            _header(f"Case {i}/3: {case_id}")

            _step(0, "Setting up workspace")
            workspace = base / "workspaces" / case_id
            workspace.mkdir(parents=True)

            # Symlink skill directory into workspace
            ws_skills = workspace / "skills"
            ws_skills.mkdir()
            (ws_skills / "eval-demo").symlink_to(skill_dir)

            # Write input.yaml
            (workspace / "input.yaml").write_text(case["input_yaml"])
            print(f"    Wrote input.yaml ({len(case['input_yaml'])} bytes)")

            # Write any extra files
            for fname, content in case.get("extra_files", {}).items():
                (workspace / fname).write_text(content)
                print(f"    Wrote {fname} ({len(content)} bytes)")

            # Create output directory
            (workspace / "output").mkdir(exist_ok=True)

            _step(1, "Calling runner.run_skill()")
            print(f"    skill_name = 'eval-demo'")
            print(f"    args       = '{case['skill_args']}'")
            print(f"    model      = 'mock-gpt-4o'")
            print(f"    workspace  = {workspace}")
            print()

            result = runner.run_skill(
                skill_name="eval-demo",
                args=case["skill_args"],
                workspace=workspace,
                model="mock-gpt-4o",
                system_prompt="You are an eval harness agent. Execute the skill precisely.",
                timeout_s=30,
            )

            results.append((case_id, result))

            _step(7, "RunResult")
            print(f"    exit_code      = {result.exit_code}")
            print(f"    duration_s     = {result.duration_s:.3f}")
            print(f"    token_usage    = {result.token_usage}")
            print(f"    resolved_model = {result.resolved_model}")
            print(f"    num_turns      = {result.num_turns}")
            print(f"    raw_output     = {result.raw_output}")
            if result.stderr:
                print(f"    stderr         = {result.stderr}")

            print(f"\n    stdout (model response):")
            for line in result.stdout.splitlines():
                print(f"      | {line}")

            # Show workspace contents after execution
            print(f"\n    Workspace files after execution:")
            for f in sorted(workspace.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    rel = f.relative_to(workspace)
                    size = f.stat().st_size
                    print(f"      {rel}  ({size} bytes)")

            # Show the output file content if it was downloaded
            output_dir = workspace / "output"
            if output_dir.exists():
                for f in sorted(output_dir.rglob("*")):
                    if f.is_file():
                        print(f"\n    Downloaded output ({f.relative_to(workspace)}):")
                        for line in f.read_text().splitlines():
                            print(f"      | {line}")
            print()

    # Summary
    _header("Summary")
    print()
    all_passed = all(r.exit_code == 0 for _, r in results)
    for case_id, result in results:
        status = "PASS" if result.exit_code == 0 else "FAIL"
        print(f"  [{status}]  {case_id:20s}  "
              f"tokens={result.token_usage}  "
              f"duration={result.duration_s:.3f}s")

    print()
    if all_passed:
        print("  All 3 cases completed successfully through the full")
        print("  Responses API lifecycle (7 steps each).")
    else:
        failed = [cid for cid, r in results if r.exit_code != 0]
        print(f"  {len(failed)} case(s) failed: {', '.join(failed)}")

    print()
    _separator()
    server.shutdown()
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
