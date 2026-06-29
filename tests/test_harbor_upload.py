"""Tests for chunked file upload in KubernetesEnvironment.

upload_dir/upload_file must not pass the whole base64 blob as a single shell
argument: Linux caps one argv entry at MAX_ARG_STRLEN (128 KiB) regardless of
the larger total ARG_MAX, so a big blob fails with E2BIG. This silently broke
"upload agent logs back to environment" (a ~0.5-1.5 MB dir) on every multi-step
trial. The blob is now written to a temp file in sub-limit chunks.

kubernetes.py imports the `harbor` library (present only in the Harbor CLI's
interpreter), so skip cleanly where it is absent.
"""

import asyncio
import base64
import io
import logging
import sys
import tarfile
from pathlib import Path

import pytest

# Probe the exact submodule kubernetes.py needs: an unrelated top-level `harbor`
# module in CI would pass importorskip("harbor") but then fail collection on
# `from harbor.environments.base import ...`.
pytest.importorskip("harbor.environments.base")
pytest.importorskip("kubernetes")

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.harbor.kubernetes import KubernetesEnvironment

_MAX_ARG_STRLEN = 131072  # Linux per-argument cap


def _env():
    env = object.__new__(KubernetesEnvironment)
    env.logger = logging.getLogger("test-upload")
    return env


def test_chunked_write_stays_under_single_arg_limit():
    env = _env()
    cmds = []

    async def fake_checked(cmd, what):
        cmds.append(cmd)

    env._checked_exec = fake_checked
    asyncio.run(env._write_b64_chunked("a" * 250_000, "/tmp/x.b64", "t"))

    assert len(cmds) == 3                          # 100k + 100k + 50k
    assert " > " in cmds[0] and " >> " not in cmds[0]   # first truncates
    assert all(" >> " in c for c in cmds[1:])           # rest append
    for c in cmds:                                  # no oversized argv entry
        assert all(len(tok) < _MAX_ARG_STRLEN for tok in c.split())


def test_chunked_write_empty_creates_file():
    env = _env()
    cmds = []

    async def fake_checked(cmd, what):
        cmds.append(cmd)

    env._checked_exec = fake_checked
    asyncio.run(env._write_b64_chunked("", "/tmp/x.b64", "t"))
    assert len(cmds) == 1 and cmds[0].startswith(":")


def test_upload_dir_gzips_and_chunks(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "claude-code.txt").write_text("x" * 50_000)
    (src / "b.txt").write_text("hello")

    env = _env()
    captured = {}
    cmds = []

    async def fake_chunked(b64, path, what):
        captured["b64"] = b64

    async def fake_checked(cmd, what):
        cmds.append(cmd)

    env._write_b64_chunked = fake_chunked
    env._checked_exec = fake_checked
    asyncio.run(env.upload_dir(src, "/workspace/logs"))

    # The blob handed to the chunked writer must be a gzip tar of the dir.
    raw = base64.b64decode(captured["b64"])
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        assert sorted(tf.getnames()) == ["b.txt", "claude-code.txt"]
    # Extraction decodes from the temp file and untars with gzip.
    extract = [c for c in cmds if "tar xz" in c]
    assert extract and "base64 -d" in extract[0]


def test_upload_file_chunks_and_decodes(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"\x00\x01" * 100_000)  # 200 KB binary

    env = _env()
    captured = {}
    cmds = []

    async def fake_chunked(b64, path, what):
        captured["b64"] = b64

    async def fake_checked(cmd, what):
        cmds.append(cmd)

    env._write_b64_chunked = fake_chunked
    env._checked_exec = fake_checked
    asyncio.run(env.upload_file(f, "/workspace/f.bin"))

    assert base64.b64decode(captured["b64"]) == b"\x00\x01" * 100_000
    assert any("base64 -d" in c and "/workspace/f.bin" in c for c in cmds)
