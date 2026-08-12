"""Tests for the Harbor Podman environment adapter."""

from agent_eval.harbor.podman import _environment_args, _external_bind_args


def test_external_bind_args_ignores_harbor_log_mounts(tmp_path):
    args = _external_bind_args([
        {"type": "bind", "source": str(tmp_path), "target": "/logs/agent"},
        {"type": "bind", "source": str(tmp_path),
         "target": "/historical-payload-data", "read_only": True},
    ])
    assert args == [
        "-v", f"{tmp_path.resolve()}:/historical-payload-data:ro"]


def test_external_bind_args_supports_writable_mount(tmp_path):
    assert _external_bind_args([
        {"type": "bind", "source": str(tmp_path), "target": "/data"},
    ]) == ["-v", f"{tmp_path.resolve()}:/data:rw"]


def test_environment_args_do_not_expose_values():
    args = _environment_args({
        "OPENAI_API_KEY": "secret-value",
        "EVAL_SNAPSHOT_DIR": "/historical-payload-data",
    })

    assert args == ["-e", "OPENAI_API_KEY", "-e", "EVAL_SNAPSHOT_DIR"]
    assert "secret-value" not in args
