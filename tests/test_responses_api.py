"""Tests for the Responses API runner.

Each test class targets a specific concern. Tests are written to catch
the actual bugs found during review — not just confirm happy paths.

Includes an embedded mock server and integration tests that exercise
the full 7-step runner lifecycle over real HTTP.
"""
import json
import os
import threading
import time
import uuid
import pytest
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch, MagicMock, call
from pathlib import Path
import tempfile


# ---------------------------------------------------------------------------
# Embedded mock Responses API server
# ---------------------------------------------------------------------------

_mock_skills: dict[str, dict] = {}
_mock_containers: dict[str, dict] = {}
_mock_container_files: dict[str, dict[str, dict]] = {}


class _MockHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

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

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v1/models":
            self._json({"object": "list", "data": [
                {"id": "mock-model", "object": "model", "owned_by": "mock"}]})
            return
        if "/files/" in path and path.endswith("/content"):
            parts = path.split("/")
            f = _mock_container_files.get(parts[3], {}).get(parts[5])
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
            files = _mock_container_files.get(cid, {})
            fl = [{"id": fid, "object": "container_file",
                   "path": f["path"], "size": len(f["content"])}
                  for fid, f in files.items()]
            self._json({"object": "list", "data": fl,
                        "first_id": fl[0]["id"] if fl else None,
                        "last_id": fl[-1]["id"] if fl else None,
                        "has_more": False})
            return
        self._json({"detail": "Not Found"}, 404)

    def do_POST(self):
        path = self.path
        body = self._body()
        if path == "/v1/skills":
            sid = f"skill_{uuid.uuid4().hex[:8]}"
            _mock_skills[sid] = {"id": sid}
            self._json({"id": sid, "object": "skill", "name": sid})
            return
        if path == "/v1/containers":
            cid = f"ctr_{uuid.uuid4().hex[:8]}"
            data = json.loads(body) if body else {}
            _mock_containers[cid] = {"id": cid, "name": data.get("name", ""),
                                     "status": "running"}
            _mock_container_files[cid] = {}
            self._json({"id": cid, "object": "container", "status": "running"})
            return
        if path.startswith("/v1/containers/") and path.endswith("/files"):
            cid = path.split("/")[3]
            if cid not in _mock_containers:
                self._json({"detail": "Not found"}, 404)
                return
            ct = self.headers.get("Content-Type", "")
            fid = f"file_{uuid.uuid4().hex[:8]}"
            fp, fc = ("/workspace/unknown", body)
            if "multipart" in ct:
                fp, fc = self._parse_multipart(body, ct)
            _mock_container_files[cid][fid] = {"path": fp, "content": fc}
            self._json({"id": fid, "object": "container_file",
                        "path": fp, "size": len(fc)})
            return
        if path == "/v1/responses":
            data = json.loads(body) if body else {}
            model = data.get("model", "mock-model")
            rid = f"resp_{uuid.uuid4().hex[:8]}"
            for tool in data.get("tools", []):
                cid = tool.get("environment", {}).get("container_id")
                if cid and cid in _mock_container_files:
                    ofid = f"file_{uuid.uuid4().hex[:8]}"
                    _mock_container_files[cid][ofid] = {
                        "path": "/workspace/output/result.txt",
                        "content": b"Mock agent output: task completed successfully.",
                    }
            self._json({
                "id": rid, "object": "response", "status": "completed",
                "model": model,
                "output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text",
                                         "text": "I executed the skill successfully. "
                                                 "Created output/result.txt with results."}]}],
                "usage": {"prompt_tokens": 150, "completion_tokens": 42,
                          "total_tokens": 192},
            })
            return
        self._json({"detail": "Not Found"}, 404)

    def do_DELETE(self):
        path = self.path
        if path.startswith("/v1/containers/"):
            cid = path.split("/")[3]
            _mock_containers.pop(cid, None)
            _mock_container_files.pop(cid, None)
            self._json({"deleted": True})
            return
        self._json({"detail": "Not Found"}, 404)


def _start_mock_server():
    _mock_skills.clear()
    _mock_containers.clear()
    _mock_container_files.clear()
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


class TestRunnersRegistry:
    def test_responses_api_in_runners(self):
        from agent_eval.agent import RUNNERS
        assert "responses-api" in RUNNERS

    def test_runner_class_is_correct(self):
        from agent_eval.agent import RUNNERS
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        assert RUNNERS["responses-api"] is ResponsesAPIRunner


class TestABCCompliance:
    def test_is_eval_runner_subclass(self):
        from agent_eval.agent.base import EvalRunner
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        assert issubclass(ResponsesAPIRunner, EvalRunner)

    def test_name_property(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        assert runner.name == "responses-api"

    def test_run_skill_signature_matches_abc(self):
        import inspect
        from agent_eval.agent.base import EvalRunner
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        abc_sig = inspect.signature(EvalRunner.run_skill)
        impl_sig = inspect.signature(ResponsesAPIRunner.run_skill)
        assert (list(abc_sig.parameters.keys())
                == list(impl_sig.parameters.keys()))


class TestClientInit:
    def test_constructor_with_explicit_params(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(
            base_url="http://localhost:8000",
            api_key="sk-test",
            default_model="gpt-4o",
        )
        assert runner._base_url == "http://localhost:8000"
        assert runner._api_key == "sk-test"
        assert runner._default_model == "gpt-4o"

    def test_constructor_falls_back_to_env(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        with patch.dict(os.environ, {
            "OPENAI_BASE_URL": "http://env-host:9000",
            "OPENAI_API_KEY": "sk-env",
            "OPENAI_MODEL": "gpt-4o-mini",
        }):
            runner = ResponsesAPIRunner()
            assert runner._base_url == "http://env-host:9000"
            assert runner._api_key == "sk-env"
            assert runner._default_model == "gpt-4o-mini"

    def test_explicit_params_override_env(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://env:8000"}):
            runner = ResponsesAPIRunner(base_url="http://explicit:9000")
            assert runner._base_url == "http://explicit:9000"

    def test_kwargs_are_absorbed(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(
            base_url="http://localhost:8000",
            permissions={"allow": ["*"]},
            effort="high",
            plugin_dirs=[],
        )
        assert runner.name == "responses-api"


class TestSkillUpload:
    def setup_method(self):
        from agent_eval.agent import responses_api
        responses_api._global_skill_cache.clear()

    def test_upload_called_once_then_cached(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")

        mock_client = MagicMock()
        mock_skill = MagicMock()
        mock_skill.id = "skill-abc123"
        mock_client.skills.create.return_value = mock_skill

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "my-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("# Test Skill")

            sid1 = runner._upload_skill(mock_client, skill_dir, "my-skill")
            sid2 = runner._upload_skill(mock_client, skill_dir, "my-skill")
            assert sid1 == sid2 == "skill-abc123"
            assert mock_client.skills.create.call_count == 1

    def test_different_skills_uploaded_separately(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")

        mock_client = MagicMock()
        counter = {"n": 0}

        def make_skill(**kwargs):
            counter["n"] += 1
            s = MagicMock()
            s.id = f"skill-{counter['n']}"
            return s

        mock_client.skills.create.side_effect = make_skill

        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("skill-a", "skill-b"):
                d = Path(tmpdir) / name
                d.mkdir()
                (d / "SKILL.md").write_text(f"# {name}")

            sid_a = runner._upload_skill(
                mock_client, Path(tmpdir) / "skill-a", "skill-a")
            sid_b = runner._upload_skill(
                mock_client, Path(tmpdir) / "skill-b", "skill-b")
            assert sid_a != sid_b
            assert counter["n"] == 2

    def test_cache_shared_across_instances(self):
        """Parallel workers (separate runner instances) must share one upload."""
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner1 = ResponsesAPIRunner(base_url="http://localhost:8000")
        runner2 = ResponsesAPIRunner(base_url="http://localhost:8000")

        mock_client = MagicMock()
        mock_skill = MagicMock()
        mock_skill.id = "skill-shared"
        mock_client.skills.create.return_value = mock_skill

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "shared-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("# Shared")

            sid1 = runner1._upload_skill(mock_client, skill_dir, "shared-skill")
            sid2 = runner2._upload_skill(mock_client, skill_dir, "shared-skill")
            assert sid1 == sid2 == "skill-shared"
            assert mock_client.skills.create.call_count == 1

    def test_concurrent_uploads_only_call_api_once(self):
        """TOCTOU regression: concurrent threads must not duplicate uploads."""
        from agent_eval.agent.responses_api import ResponsesAPIRunner

        upload_count = 0
        upload_lock = threading.Lock()

        def slow_create(**kwargs):
            nonlocal upload_count
            time.sleep(0.05)
            with upload_lock:
                upload_count += 1
            s = MagicMock()
            s.id = "skill-concurrent"
            return s

        mock_client = MagicMock()
        mock_client.skills.create.side_effect = slow_create

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "conc-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("# Concurrent")

            results = [None] * 4
            def worker(idx):
                r = ResponsesAPIRunner(base_url="http://localhost:8000")
                results[idx] = r._upload_skill(
                    mock_client, skill_dir, "conc-skill")

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert all(r == "skill-concurrent" for r in results)
            assert upload_count == 1, (
                f"Expected 1 API call, got {upload_count} — TOCTOU race")


class TestContainerLifecycle:
    def test_create_container_attaches_skill(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "ctr-123"
        mock_client.containers.create.return_value = mock_container

        cid = runner._create_container(mock_client, "skill-abc")
        assert cid == "ctr-123"
        kw = mock_client.containers.create.call_args.kwargs
        assert kw["skills"] == [
            {"type": "skill_reference", "skill_id": "skill-abc"}]

    def test_create_container_memory_limit_string_format(self):
        """memory_limit must be one of the API-accepted tier strings."""
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(
            base_url="http://localhost:8000", memory_limit_mb=1024)
        mock_client = MagicMock()
        mock_client.containers.create.return_value = MagicMock(id="ctr-456")

        runner._create_container(mock_client, "skill-xyz")
        kw = mock_client.containers.create.call_args.kwargs
        assert kw["memory_limit"] == "1g"
        assert isinstance(kw["memory_limit"], str)

    def test_memory_limit_tiers(self):
        """Verify MB-to-tier mapping for various sizes."""
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        r = ResponsesAPIRunner(base_url="http://x")
        assert r._pick_memory_limit(512) == "1g"
        assert r._pick_memory_limit(1024) == "1g"
        assert r._pick_memory_limit(2048) == "4g"
        assert r._pick_memory_limit(8000) == "16g"
        assert r._pick_memory_limit(32000) == "64g"
        assert r._pick_memory_limit(99999) == "64g"

    def test_create_container_names_are_unique(self):
        """Concurrent evals must not clash on container names."""
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        mock_client = MagicMock()
        mock_client.containers.create.return_value = MagicMock(id="ctr")

        names = set()
        for _ in range(20):
            runner._create_container(mock_client, "skill-x")
            kw = mock_client.containers.create.call_args.kwargs
            names.add(kw["name"])
        assert len(names) == 20, "Container names must be unique"

    def test_create_container_passes_network_policy(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        policy = {"type": "allowlist", "allowed_domains": ["pypi.org"]}
        runner = ResponsesAPIRunner(
            base_url="http://localhost:8000", network_policy=policy)
        mock_client = MagicMock()
        mock_client.containers.create.return_value = MagicMock(id="ctr")

        runner._create_container(mock_client, "skill-x")
        kw = mock_client.containers.create.call_args.kwargs
        assert kw["network_policy"] == policy

    def test_create_container_omits_network_policy_when_none(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        mock_client = MagicMock()
        mock_client.containers.create.return_value = MagicMock(id="ctr")

        runner._create_container(mock_client, "skill-x")
        kw = mock_client.containers.create.call_args.kwargs
        assert "network_policy" not in kw

    def test_upload_workspace_sends_correct_paths(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        mock_client = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / "input.yaml").write_text("key: value")
            (ws / "src").mkdir()
            (ws / "src" / "main.py").write_text("print('hello')")

            uploaded = runner._upload_workspace(mock_client, "ctr-123", ws)
            assert "/workspace/input.yaml" in uploaded
            assert "/workspace/src/main.py" in uploaded
            assert mock_client.containers.files.create.call_count == 2

    def test_upload_workspace_skips_symlinks(self):
        """Symlinks could read outside the workspace (CWE-59)."""
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        mock_client = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir) / "workspace"
            ws.mkdir()
            (ws / "real.txt").write_text("real content")
            (ws / "link.txt").symlink_to(ws / "real.txt")

            uploaded = runner._upload_workspace(mock_client, "ctr-123", ws)
            assert len(uploaded) == 1
            assert "/workspace/real.txt" in uploaded
            assert "/workspace/link.txt" not in uploaded

    def test_download_syncs_both_new_and_modified_files(self):
        """Modified uploaded files must be synced back, not just new ones."""
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        mock_client = MagicMock()

        new_file = MagicMock(path="/workspace/output/result.md", id="f-new")
        modified_file = MagicMock(path="/workspace/input.yaml", id="f-mod")
        system_file = MagicMock(path="/tmp/internal.log", id="f-sys")

        mock_client.containers.files.list.return_value = [
            new_file, modified_file, system_file]

        content_map = {
            "f-new": MagicMock(content=b"new result"),
            "f-mod": MagicMock(content=b"modified input"),
        }
        mock_client.containers.files.content.retrieve.side_effect = (
            lambda file_id, container_id: content_map[file_id])

        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / "input.yaml").write_text("original")

            runner._download_results(mock_client, "ctr-1", ws)

            assert (ws / "output" / "result.md").read_bytes() == b"new result"
            assert (ws / "input.yaml").read_bytes() == b"modified input"
            assert not (ws / "tmp").exists(), "Non-workspace files must be skipped"
            assert mock_client.containers.files.content.retrieve.call_count == 2

    def test_download_overwrites_local_content(self):
        """The agent may edit input.yaml in-place; the local copy must update."""
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        mock_client = MagicMock()

        mock_file = MagicMock(path="/workspace/data.txt", id="f-1")
        mock_client.containers.files.list.return_value = [mock_file]
        mock_client.containers.files.content.retrieve.return_value = (
            MagicMock(content=b"updated by agent"))

        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / "data.txt").write_text("original content")

            runner._download_results(mock_client, "ctr-1", ws)
            assert (ws / "data.txt").read_bytes() == b"updated by agent"

    def test_delete_container_logs_on_failure(self, capsys):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(
            base_url="http://localhost:8000", log_prefix="test")
        mock_client = MagicMock()
        mock_client.containers.delete.side_effect = RuntimeError("gone")

        runner._delete_container(mock_client, "ctr-123")
        captured = capsys.readouterr()
        assert "cleanup failed" in captured.out
        assert "ctr-123" in captured.out

    def test_delete_container_silent_without_log_prefix(self, capsys):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        mock_client = MagicMock()
        mock_client.containers.delete.side_effect = RuntimeError("gone")

        runner._delete_container(mock_client, "ctr-123")
        captured = capsys.readouterr()
        assert captured.out == ""


class TestRunSkill:
    """Integration-level tests for the full run_skill flow."""

    def setup_method(self):
        from agent_eval.agent import responses_api
        responses_api._global_skill_cache.clear()

    def _make_runner(self, **kwargs):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        default_model = kwargs.pop("default_model", "gpt-4o")
        return ResponsesAPIRunner(
            base_url="http://localhost:8000",
            api_key="sk-test",
            default_model=default_model,
            **kwargs,
        )

    def _mock_response(self, text="Done", prompt_tokens=100,
                        completion_tokens=50, model="gpt-4o",
                        status="completed"):
        resp = MagicMock()
        resp.id = "resp-123"
        resp.model = model
        resp.status = status
        output_msg = MagicMock()
        output_msg.type = "message"
        output_msg.content = [MagicMock(type="output_text", text=text)]
        resp.output = [output_msg]
        resp.usage = MagicMock()
        resp.usage.prompt_tokens = prompt_tokens
        resp.usage.completion_tokens = completion_tokens
        return resp

    def _run_with_mock(self, runner, mock_client, **run_kwargs):
        """Helper to run_skill with mocked client and skill dir."""
        with patch.object(runner, '_get_client', return_value=mock_client):
            with patch.object(runner, '_find_skill_dir',
                              return_value=Path("/tmp/skill")):
                with tempfile.TemporaryDirectory() as tmpdir:
                    ws = Path(tmpdir)
                    defaults = dict(
                        skill_name="test-skill", args="", workspace=ws,
                        model="gpt-4o")
                    defaults.update(run_kwargs)
                    if "workspace" not in run_kwargs:
                        defaults["workspace"] = ws
                    return runner.run_skill(**defaults)

    def _setup_mock_client(self, response=None):
        mock_client = MagicMock()
        mock_skill = MagicMock(id="skill-abc")
        mock_client.skills.create.return_value = mock_skill
        mock_client.containers.create.return_value = MagicMock(id="ctr-123")
        mock_client.containers.files.list.return_value = []
        mock_client.responses.create.return_value = (
            response or self._mock_response())
        return mock_client

    def test_success_returns_correct_result(self):
        runner = self._make_runner()
        mock_client = self._setup_mock_client()

        result = self._run_with_mock(runner, mock_client)

        assert result.exit_code == 0
        assert result.token_usage == {"input": 100, "output": 50}
        assert result.resolved_model == "gpt-4o"
        assert result.duration_s > 0
        assert result.num_turns == 1
        assert result.stderr == ""

    def test_api_error_returns_exit_code_1(self):
        runner = self._make_runner()
        mock_client = self._setup_mock_client()
        mock_client.responses.create.side_effect = Exception("API error")

        result = self._run_with_mock(runner, mock_client)

        assert result.exit_code == 1
        assert "API error" in result.stderr

    def test_incomplete_response_returns_exit_code_1(self):
        runner = self._make_runner()
        resp = self._mock_response(status="failed")
        mock_client = self._setup_mock_client(response=resp)

        result = self._run_with_mock(runner, mock_client)
        assert result.exit_code == 1

    def test_uses_container_reference_not_auto(self):
        """Must use container_reference since we pre-create the container."""
        runner = self._make_runner()
        mock_client = self._setup_mock_client()

        self._run_with_mock(runner, mock_client)

        kw = mock_client.responses.create.call_args.kwargs
        tool_env = kw["tools"][0]["environment"]
        assert tool_env["type"] == "container_reference", (
            "Must use container_reference, not container_auto")
        assert "container_id" in tool_env
        assert "skills" not in tool_env, (
            "Skills go on container creation, not the tool environment")

    def test_skills_attached_to_container_not_tool(self):
        """Skills must be attached at container creation time."""
        runner = self._make_runner()
        mock_client = self._setup_mock_client()

        self._run_with_mock(runner, mock_client)

        ctr_kw = mock_client.containers.create.call_args.kwargs
        assert "skills" in ctr_kw
        assert ctr_kw["skills"][0]["skill_id"] == "skill-abc"

    def test_model_arg_overrides_default(self):
        runner = self._make_runner(default_model="gpt-4o-mini")
        mock_client = self._setup_mock_client(
            response=self._mock_response(model="gpt-4o"))

        self._run_with_mock(runner, mock_client, model="gpt-4o")

        kw = mock_client.responses.create.call_args.kwargs
        assert kw["model"] == "gpt-4o"

    def test_system_prompt_sent_as_developer_role(self):
        runner = self._make_runner()
        mock_client = self._setup_mock_client()

        self._run_with_mock(runner, mock_client,
                            system_prompt="You are a reviewer")

        kw = mock_client.responses.create.call_args.kwargs
        msgs = kw["input"]
        assert msgs[0]["role"] == "developer"
        assert msgs[0]["content"] == "You are a reviewer"
        assert msgs[1]["role"] == "user"

    def test_no_system_prompt_sends_only_user(self):
        runner = self._make_runner()
        mock_client = self._setup_mock_client()

        self._run_with_mock(runner, mock_client)

        kw = mock_client.responses.create.call_args.kwargs
        msgs = kw["input"]
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_cleanup_on_success(self):
        runner = self._make_runner()
        mock_client = self._setup_mock_client()

        self._run_with_mock(runner, mock_client)

        mock_client.containers.delete.assert_called_once_with("ctr-123")

    def test_cleanup_on_error(self):
        runner = self._make_runner()
        mock_client = self._setup_mock_client()
        mock_client.responses.create.side_effect = Exception("boom")

        self._run_with_mock(runner, mock_client)

        mock_client.containers.delete.assert_called_once_with("ctr-123")

    def test_no_cleanup_when_container_not_created(self):
        runner = self._make_runner()
        mock_client = self._setup_mock_client()
        mock_client.skills.create.side_effect = Exception("upload failed")

        self._run_with_mock(runner, mock_client)

        mock_client.containers.delete.assert_not_called()

    def test_prompt_format(self):
        runner = self._make_runner()
        mock_client = self._setup_mock_client()

        self._run_with_mock(runner, mock_client,
                            skill_name="my-skill", args="--input foo.yaml")

        kw = mock_client.responses.create.call_args.kwargs
        user_msg = kw["input"][-1]["content"]
        assert user_msg == "/my-skill --input foo.yaml"


class TestEdgeCases:
    def setup_method(self):
        from agent_eval.agent import responses_api
        responses_api._global_skill_cache.clear()

    def test_run_skill_raises_on_no_model(self):
        """Must fail fast before any API calls."""
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        mock_client = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            with pytest.raises(ValueError, match="No model specified"):
                runner.run_skill("test-skill", "", ws, "")

        mock_client.skills.create.assert_not_called()

    def test_skill_not_found_raises(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            with pytest.raises(FileNotFoundError, match="Skill directory"):
                runner._find_skill_dir(ws, "nonexistent-skill")

    def test_find_skill_dir_dot_skills(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            skill_dir = ws / ".skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Skill")
            assert runner._find_skill_dir(ws, "my-skill") == skill_dir

    def test_find_skill_dir_skills(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            skill_dir = ws / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Skill")
            assert runner._find_skill_dir(ws, "my-skill") == skill_dir

    def test_find_skill_dir_requires_skill_md(self):
        """A directory without SKILL.md must not match."""
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / ".skills" / "my-skill").mkdir(parents=True)
            with pytest.raises(FileNotFoundError):
                runner._find_skill_dir(ws, "my-skill")

    def test_extract_output_text_empty(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        resp = MagicMock(output=[])
        assert runner._extract_output_text(resp) == ""

    def test_extract_output_text_skips_non_message_items(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        msg = MagicMock(type="message",
                        content=[MagicMock(type="output_text", text="Hello")])
        tool = MagicMock(type="shell_call", content=None)
        resp = MagicMock(output=[tool, msg])
        assert runner._extract_output_text(resp) == "Hello"

    def test_count_turns_minimum_one(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        resp = MagicMock(output=[])
        assert runner._count_turns(resp) == 1

    def test_count_turns_only_counts_messages(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        msgs = [MagicMock(type="message") for _ in range(3)]
        msgs.append(MagicMock(type="shell_call"))
        resp = MagicMock(output=msgs)
        assert runner._count_turns(resp) == 3

    def test_empty_workspace_no_files_uploaded(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        runner = ResponsesAPIRunner(base_url="http://localhost:8000")
        mock_client = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            uploaded = runner._upload_workspace(mock_client, "ctr-1", ws)
            assert len(uploaded) == 0
            mock_client.containers.files.create.assert_not_called()

    def test_no_base_url_defaults_to_empty(self):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        with patch.dict(os.environ, {}, clear=True):
            runner = ResponsesAPIRunner()
            assert runner._base_url == ""


# ---------------------------------------------------------------------------
# Integration tests — full lifecycle against embedded mock server
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mock_server():
    server, port = _start_mock_server()
    yield port
    server.shutdown()


@pytest.fixture()
def live_workspace(tmp_path):
    skill_dir = tmp_path / ".skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Test Skill\nDo something useful.")
    (tmp_path / "input.yaml").write_text("task: write a greeting\n")
    (tmp_path / "source.md").write_text("# Original\nHello world.\n")
    (tmp_path / "output").mkdir()
    return tmp_path


@pytest.fixture()
def live_runner(mock_server):
    from agent_eval.agent.responses_api import ResponsesAPIRunner
    return ResponsesAPIRunner(
        base_url=f"http://127.0.0.1:{mock_server}/v1",
        api_key="fake-key",
        default_model="mock-model",
        log_prefix="test",
    )


class TestIntegrationLifecycle:
    """End-to-end: run_skill through all 7 steps over real HTTP."""

    def test_run_skill_completes(self, live_runner, live_workspace):
        result = live_runner.run_skill(
            skill_name="test-skill", args="--verbose",
            workspace=live_workspace, model="mock-model", timeout_s=30)
        assert result.exit_code == 0
        assert result.stderr == ""
        assert "executed the skill successfully" in result.stdout
        assert result.token_usage == {"input": 150, "output": 42}
        assert result.num_turns == 1
        assert result.raw_output["status"] == "completed"

    def test_output_file_downloaded(self, live_runner, live_workspace):
        live_runner.run_skill(
            skill_name="test-skill", args="",
            workspace=live_workspace, model="mock-model")
        result_file = live_workspace / "output" / "result.txt"
        assert result_file.exists()
        assert "Mock agent output" in result_file.read_text()

    def test_container_cleaned_up(self, live_runner, live_workspace):
        live_runner.run_skill(
            skill_name="test-skill", args="",
            workspace=live_workspace, model="mock-model")
        assert len(_mock_containers) == 0
        assert len(_mock_container_files) == 0

    def test_skill_cached_across_runs(self, live_runner, live_workspace):
        live_runner.run_skill(
            skill_name="test-skill", args="",
            workspace=live_workspace, model="mock-model")
        count_after_first = len(_mock_skills)
        live_runner.run_skill(
            skill_name="test-skill", args="",
            workspace=live_workspace, model="mock-model")
        assert len(_mock_skills) == count_after_first

    def test_model_override(self, live_runner, live_workspace):
        result = live_runner.run_skill(
            skill_name="test-skill", args="",
            workspace=live_workspace, model="custom-model")
        assert result.resolved_model == "custom-model"

    def test_connection_error(self, tmp_path):
        from agent_eval.agent.responses_api import ResponsesAPIRunner
        skill_dir = tmp_path / ".skills" / "s"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# S")
        runner = ResponsesAPIRunner(
            base_url="http://127.0.0.1:1/v1",
            api_key="fake", default_model="m")
        result = runner.run_skill(
            skill_name="s", args="", workspace=tmp_path, model="m")
        assert result.exit_code == 1
        assert result.stderr != ""
