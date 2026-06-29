"""Tests for K8s resource creation (agent_eval/harbor/k8s_resources.py)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_eval.harbor import k8s_resources as kr


def _mock_core():
    core = MagicMock()
    core.create_namespaced_config_map.return_value = None
    core.create_namespaced_secret.return_value = None
    return core


def test_collect_files(tmp_path):
    (tmp_path / "a.py").write_text("print('a')")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("# b")
    (tmp_path / "binary.png").write_bytes(b"\x89PNG")
    files = kr._collect_files(tmp_path, {".py", ".md"})
    assert "a.py" in files
    assert "sub--b.md" in files
    assert "binary.png" not in files


def test_create_project_configmap(tmp_path):
    (tmp_path / ".claude" / "skills" / "my-skill").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "my-skill" / "SKILL.md").write_text("# Skill")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "helper.py").write_text("def help(): pass")
    (tmp_path / "CLAUDE.md").write_text("# Project")

    core = _mock_core()
    with patch.object(kr, "_ensure_client", return_value=core):
        kr.create_project_configmap(tmp_path, "my-project", "test-ns")

    core.create_namespaced_config_map.assert_called_once()
    cm = core.create_namespaced_config_map.call_args[0][1]
    assert cm.metadata.name == "my-project"
    assert cm.metadata.namespace == "test-ns"
    assert "CLAUDE.md" in cm.data
    assert any("SKILL.md" in k for k in cm.data)
    assert any("helper.py" in k for k in cm.data)
    assert cm.metadata.labels["app.kubernetes.io/managed-by"] == "agent-eval-harness"


def test_create_eval_configmap(tmp_path):
    (tmp_path / "eval.yaml").write_text("name: test\nskill: my-skill\n")
    (tmp_path / "tool_handlers.yaml").write_text("handlers: []\n")

    core = _mock_core()
    with patch.object(kr, "_ensure_client", return_value=core):
        kr.create_eval_configmap(tmp_path / "eval.yaml", "my-eval", "test-ns")

    cm = core.create_namespaced_config_map.call_args[0][1]
    assert "eval.yaml" in cm.data
    assert "tool_handlers.yaml" in cm.data


def test_create_creds_secret(tmp_path):
    creds = tmp_path / "sa-key.json"
    creds.write_text('{"type": "service_account"}')

    core = _mock_core()
    with patch.object(kr, "_ensure_client", return_value=core):
        kr.create_creds_secret(creds, "vertex-creds", "test-ns")

    core.create_namespaced_secret.assert_called_once()
    secret = core.create_namespaced_secret.call_args[0][1]
    assert "key.json" in secret.data


def test_create_env_secret():
    core = _mock_core()
    with patch.object(kr, "_ensure_client", return_value=core):
        kr.create_env_secret(
            {"ANTHROPIC_API_KEY": "sk-test"}, "model-keys", "test-ns")

    secret = core.create_namespaced_secret.call_args[0][1]
    assert "ANTHROPIC_API_KEY" in secret.data


def test_configmap_update_on_conflict():
    """If ConfigMap already exists (409), it should be updated."""
    from kubernetes.client.rest import ApiException
    core = _mock_core()
    exc = ApiException(status=409)
    core.create_namespaced_config_map.side_effect = exc

    kr._apply_configmap(core, "test", "ns", {"key": "value"})
    core.replace_namespaced_config_map.assert_called_once()


def test_cleanup():
    core = _mock_core()
    cm1 = MagicMock()
    cm1.metadata.name = "cm1"
    core.list_namespaced_config_map.return_value.items = [cm1]
    core.list_namespaced_secret.return_value.items = []

    with patch.object(kr, "_ensure_client", return_value=core):
        deleted = kr.cleanup("test-ns")

    assert deleted == 1
    core.delete_namespaced_config_map.assert_called_once_with("cm1", "test-ns")
