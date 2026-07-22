"""Tests for harness-snapshot → MLflow tag collection and fetch."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from agent_eval.ci_context import (
    CIContext,
    HARNESS_SNAPSHOT_ARTIFACT,
    collect_ci_context,
    collect_ci_fallback,
    context_from_snapshot,
    fetch_harness_snapshot,
    find_harness_snapshot,
    merge_mlflow_tags,
)


def test_context_from_snapshot_maps_fields():
    data = {
        "schema_version": "1",
        "agent": "triage",
        "model": "opus",
        "harness_content_sha": "abc",
        "ref_revision": "deadbeef",
        "repository_url": "https://github.com/o/r",
        "change_id": "12",
        "pipeline_run_id": "99",
        "forge_platform": "github",
        "trace_id": "4bf9",
    }
    tags = context_from_snapshot(data, eval_run_id="run-1").as_mlflow_tags()
    assert tags["eval_run_id"] == "run-1"
    assert tags["agent"] == "triage"
    assert tags["commit_sha"] == "deadbeef"
    assert tags["harness_fingerprint"] == "abc"


def test_find_and_collect_from_run_dir(tmp_path: Path):
    snap = {
        "schema_version": "1",
        "agent": "review",
        "harness_content_sha": "ff",
        "ref_revision": "sha1",
    }
    nested = tmp_path / "cases" / "c1"
    nested.mkdir(parents=True)
    (nested / HARNESS_SNAPSHOT_ARTIFACT).write_text(json.dumps(snap))

    assert find_harness_snapshot(tmp_path) is not None
    tags = collect_ci_context(eval_run_id="r", run_dir=tmp_path)
    assert tags["agent"] == "review"
    assert tags["commit_sha"] == "sha1"


def test_fetch_harness_snapshot_from_mlflow(tmp_path: Path):
    snap = {"agent": "code", "harness_content_sha": "x", "ref_revision": "abc"}
    artifact = tmp_path / HARNESS_SNAPSHOT_ARTIFACT
    artifact.write_text(json.dumps(snap))

    client = MagicMock()
    client.download_artifacts.return_value = str(artifact)

    fake_runs = MagicMock()
    fake_runs.empty = False
    fake_runs.run_id = ["mlflow-run-1"]

    data = fetch_harness_snapshot(
        "exp1",
        "eval-run-1",
        client=client,
        search_runs=lambda **kwargs: fake_runs,
    )
    assert data is not None
    assert data["agent"] == "code"
    client.download_artifacts.assert_called_once()


def test_disk_to_tags_round_trip(tmp_path: Path):
    """Vertical slice (disk handoff): write snapshot → collect tags → reload."""
    snap = {
        "schema_version": "1",
        "agent": "triage",
        "harness_content_sha": "deadbeef",
        "ref_revision": "abc123",
        "repository_url": "https://example.com/o/r",
    }
    path = tmp_path / HARNESS_SNAPSHOT_ARTIFACT
    path.write_text(json.dumps(snap))
    tags = collect_ci_context(eval_run_id="run-9", run_dir=tmp_path)
    assert tags["agent"] == "triage"
    assert tags["commit_sha"] == "abc123"
    assert tags["harness_fingerprint"] == "deadbeef"
    # Simulate MLflow artifact download returning the same file
    client = MagicMock()
    client.download_artifacts.return_value = str(path)
    fake_runs = MagicMock()
    fake_runs.empty = False
    fake_runs.run_id = ["mr1"]
    fetched = fetch_harness_snapshot(
        "exp", "run-9", client=client, search_runs=lambda **k: fake_runs
    )
    assert fetched["harness_content_sha"] == snap["harness_content_sha"]


def test_fullsend_shaped_snapshot_maps_to_provenance_tags(tmp_path: Path):
    """Producer-shaped JSON (agent runtime) → join tags Provenance expects."""
    snap = {
        "schema_version": "1",
        "recorded_at": "2026-07-22T00:00:00Z",
        "agent": "code",
        "role": "coder",
        "slug": "code",
        "model": "claude",
        "harness_path": "/harnesses/code.yaml",
        "harness_content_sha": "0123456789abcdef",
        "skills": ["code"],
        "forge_platform": "github",
        "trace_id": "4bf92a13a9009265",
        "traceparent": "00-4bf92a13a9009265-00f067aa0ba902b7-01",
        "repository_url": "https://github.com/acme/repo",
        "ref_revision": "c0ffee",
        "ref_name": "main",
        "change_id": "42",
        "pipeline_run_id": "99",
        "pipeline_run_url": "https://github.com/acme/repo/actions/runs/99",
    }
    (tmp_path / HARNESS_SNAPSHOT_ARTIFACT).write_text(json.dumps(snap))
    tags = collect_ci_context(eval_run_id="eval-1", run_dir=tmp_path)
    # Provenance / MLflow join names from the frozen contract
    assert tags["commit_sha"] == "c0ffee"
    assert tags["harness_fingerprint"] == "0123456789abcdef"
    assert tags["repository_url"] == "https://github.com/acme/repo"
    assert tags["change_id"] == "42"
    assert tags["pipeline_run_id"] == "99"
    assert tags["forge_platform"] == "github"
    assert tags["trace_id"] == "4bf92a13a9009265"
    assert tags["eval_run_id"] == "eval-1"


def test_prefer_mlflow_over_disk(tmp_path: Path, monkeypatch):
    disk = tmp_path / HARNESS_SNAPSHOT_ARTIFACT
    disk.write_text(json.dumps({"agent": "from-disk", "harness_content_sha": "d"}))

    ml_snap = {"agent": "from-mlflow", "harness_content_sha": "m"}
    monkeypatch.setattr(
        "agent_eval.ci_context.fetch_harness_snapshot",
        lambda *a, **k: ml_snap,
    )
    tags = collect_ci_context(
        eval_run_id="r",
        run_dir=tmp_path,
        experiment_id="e1",
        prefer_mlflow=True,
    )
    assert tags["agent"] == "from-mlflow"


def test_ci_fallback_gitlab(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith(("GITHUB_", "CI_", "AGENT_EVAL_", "BITBUCKET_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_COMMIT_SHA", "glsha")
    monkeypatch.setenv("CI_PROJECT_PATH", "g/p")
    monkeypatch.setenv("CI_SERVER_URL", "https://gitlab.com")
    monkeypatch.setenv("CI_PIPELINE_ID", "8")
    ctx = collect_ci_fallback(eval_run_id="r")
    assert ctx.forge_platform == "gitlab"
    assert ctx.ref_revision == "glsha"
    assert "pipelines/8" in ctx.pipeline_run_url


def test_merge_mlflow_tags_config_overrides():
    merged = merge_mlflow_tags(
        {"commit_sha": "from-ci", "eval_run_id": "r1"},
        {"commit_sha": "override", "team": "ml"},
    )
    assert merged["commit_sha"] == "override"
    assert merged["team"] == "ml"


def test_ci_context_empty_tags():
    assert CIContext().as_mlflow_tags() == {}
