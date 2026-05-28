"""Tests for agent_eval.harbor.k8s_runner — Phase 2 gating tests.

Written BEFORE the implementation exists. Tests mock the kubernetes
client so no cluster is needed.
"""

import types
from unittest.mock import MagicMock, patch

import pytest


class TestScriptGeneration:
    def test_oracle_script_contains_solve_and_test(self):
        from agent_eval.harbor.k8s_runner import _oracle_script

        script = _oracle_script()
        assert "solve.sh" in script
        assert "test.sh" in script
        assert "HARBOR_REWARD" in script

    def test_agent_script_includes_model_flag(self):
        from agent_eval.harbor.k8s_runner import _agent_script

        script = _agent_script(model="claude-sonnet-4-6")
        assert "--model" in script
        assert "claude-sonnet-4-6" in script
        assert "test.sh" in script
        assert "HARBOR_REWARD" in script

    def test_agent_script_no_model_flag(self):
        from agent_eval.harbor.k8s_runner import _agent_script

        script = _agent_script(model="")
        assert "--model" not in script
        assert "claude" in script  # still invokes claude CLI


class TestBuildVolumes:
    def test_build_volumes_empty(self):
        from agent_eval.harbor.k8s_runner import _build_volumes

        volumes, mounts = _build_volumes(None)
        assert volumes is None
        assert mounts is None

        volumes, mounts = _build_volumes([])
        assert volumes is None
        assert mounts is None

    @patch("agent_eval.harbor.k8s_runner._K8S_AVAILABLE", True)
    @patch("agent_eval.harbor.k8s_runner.client")
    def test_build_volumes_with_secrets(self, mock_client):
        # Set up mock K8s types that behave like real objects
        mock_client.V1KeyToPath = lambda key, path: types.SimpleNamespace(key=key, path=path)
        mock_client.V1Volume = lambda name, secret: types.SimpleNamespace(name=name, secret=secret)
        mock_client.V1SecretVolumeSource = lambda secret_name, items=None: types.SimpleNamespace(secret_name=secret_name, items=items)
        mock_client.V1VolumeMount = lambda name, mount_path, read_only: types.SimpleNamespace(name=name, mount_path=mount_path, read_only=read_only)

        from agent_eval.harbor.k8s_runner import _build_volumes

        secret_volumes = [
            {
                "secret_name": "api-keys",
                "mount_path": "/secrets/api-keys",
            },
            {
                "secret_name": "certs",
                "mount_path": "/secrets/certs",
                "items": [{"key": "ca.crt", "path": "ca.crt"}],
            },
        ]

        volumes, mounts = _build_volumes(secret_volumes)
        assert len(volumes) == 2
        assert len(mounts) == 2
        assert mounts[0].mount_path == "/secrets/api-keys"
        assert mounts[0].read_only is True
        assert mounts[1].mount_path == "/secrets/certs"


class TestRunTaskJob:
    @patch("agent_eval.harbor.k8s_runner._load_k8s_config")
    @patch("agent_eval.harbor.k8s_runner.client")
    def test_run_task_job_oracle_success(self, mock_client, mock_load_config):
        from agent_eval.harbor.k8s_runner import run_task_job

        mock_batch = MagicMock()
        mock_core = MagicMock()
        mock_client.BatchV1Api.return_value = mock_batch
        mock_client.CoreV1Api.return_value = mock_core

        # Job succeeds on first status check
        mock_status = MagicMock()
        mock_status.status.succeeded = True
        mock_status.status.failed = None
        mock_batch.read_namespaced_job_status.return_value = mock_status

        # Pod logs contain reward
        mock_pod = MagicMock()
        mock_pod.metadata.name = "harbor-test-12345-abc"
        mock_core.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])
        mock_core.read_namespaced_pod_log.return_value = (
            "Running tests...\n"
            "HARBOR_REWARD=1.0\n"
        )

        # Mock constructors so they don't fail without real kubernetes
        mock_client.V1Job = MagicMock()
        mock_client.V1ObjectMeta = MagicMock()
        mock_client.V1JobSpec = MagicMock()
        mock_client.V1PodTemplateSpec = MagicMock()
        mock_client.V1PodSpec = MagicMock()
        mock_client.V1PodSecurityContext = MagicMock()
        mock_client.V1Container = MagicMock()
        mock_client.V1ResourceRequirements = MagicMock()

        result = run_task_job(
            task_name="test-task",
            task_image="registry/task:latest",
            namespace="evalhub",
            timeout_sec=300,
            agent="oracle",
        )

        assert result["reward"] == 1.0
        assert result["exit_code"] == 0
        assert result["duration_s"] >= 0
        mock_batch.create_namespaced_job.assert_called_once()
        mock_batch.delete_namespaced_job.assert_called_once()

    @patch("agent_eval.harbor.k8s_runner._load_k8s_config")
    @patch("agent_eval.harbor.k8s_runner.client")
    @patch("agent_eval.harbor.k8s_runner.time")
    def test_run_task_job_timeout(self, mock_time, mock_client, mock_load_config):
        from agent_eval.harbor.k8s_runner import run_task_job

        mock_batch = MagicMock()
        mock_core = MagicMock()
        mock_client.BatchV1Api.return_value = mock_batch
        mock_client.CoreV1Api.return_value = mock_core

        # Job never completes — monotonic advances past timeout
        call_count = [0]
        def advancing_monotonic():
            call_count[0] += 1
            return call_count[0] * 100.0  # jumps 100s per call
        mock_time.monotonic = advancing_monotonic
        mock_time.sleep = MagicMock()

        mock_status = MagicMock()
        mock_status.status.succeeded = None
        mock_status.status.failed = None
        mock_batch.read_namespaced_job_status.return_value = mock_status

        mock_core.list_namespaced_pod.return_value = MagicMock(items=[])

        mock_client.V1Job = MagicMock()
        mock_client.V1ObjectMeta = MagicMock()
        mock_client.V1JobSpec = MagicMock()
        mock_client.V1PodTemplateSpec = MagicMock()
        mock_client.V1PodSpec = MagicMock()
        mock_client.V1PodSecurityContext = MagicMock()
        mock_client.V1Container = MagicMock()
        mock_client.V1ResourceRequirements = MagicMock()

        result = run_task_job(
            task_name="timeout-task",
            task_image="registry/task:latest",
            namespace="evalhub",
            timeout_sec=10,
            agent="oracle",
        )

        assert result["reward"] == 0.0
        assert result["exit_code"] == -1

    def test_unknown_agent_raises(self):
        from agent_eval.harbor.k8s_runner import run_task_job

        with pytest.raises(ValueError, match="Unknown agent"):
            # This should fail before any K8s calls, so no mocking needed
            # But we need to mock _load_k8s_config to prevent real cluster access
            with patch("agent_eval.harbor.k8s_runner._load_k8s_config"):
                with patch("agent_eval.harbor.k8s_runner.client"):
                    run_task_job(
                        task_name="test",
                        task_image="img",
                        namespace="ns",
                        agent="invalid-agent",
                    )
