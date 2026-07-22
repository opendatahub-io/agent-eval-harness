"""Harness snapshot join keys for MLflow tags and artifact I/O.

The agent runtime may write ``harness-snapshot.json`` into case/run output.
``log_results`` reads that file (disk handoff), projects join fields to MLflow
tags, and uploads the artifact. Later readers should fetch from MLflow — the
same discovery style as ``inputs/`` artifacts — not scrape CI env.

This module does not own the cross-project join schema; it only maps the
snapshot file and MLflow tags. Do not confuse with eval-dataset
``generation.strategy`` case-generation provenance.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

HARNESS_SNAPSHOT_ARTIFACT = "harness-snapshot.json"

_SNAPSHOT_TAG_FIELDS: frozenset[str] = frozenset(
    {
        "agent",
        "role",
        "slug",
        "model",
        "harness_content_sha",
        "forge_platform",
        "trace_id",
        "repository_url",
        "ref_revision",
        "ref_name",
        "change_id",
        "pipeline_run_id",
        "pipeline_run_url",
    }
)


@dataclass(frozen=True)
class CIContext:
    """Join keys for an eval/MLflow run (subset of harness-snapshot fields)."""

    eval_run_id: str = ""
    agent: str = ""
    role: str = ""
    slug: str = ""
    model: str = ""
    harness_content_sha: str = ""
    forge_platform: str = ""
    trace_id: str = ""
    repository_url: str = ""
    ref_revision: str = ""
    ref_name: str = ""
    change_id: str = ""
    pipeline_run_id: str = ""
    pipeline_run_url: str = ""

    def as_mlflow_tags(self) -> dict[str, str]:
        """Non-empty fields as MLflow tags (`commit_sha` aliases `ref_revision`)."""
        tags: dict[str, str] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if not value:
                continue
            if f.name == "ref_revision":
                tags["commit_sha"] = value
                tags["ref_revision"] = value
            elif f.name == "harness_content_sha":
                tags["harness_content_sha"] = value
                tags["harness_fingerprint"] = value
            else:
                tags[f.name] = value
        return tags


def load_harness_snapshot(path: Path | str) -> dict:
    """Parse harness-snapshot.json; raises on missing/invalid file."""
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"harness-snapshot must be a JSON object: {path}")
    return data


def context_from_snapshot(
    data: Mapping,
    *,
    eval_run_id: str = "",
) -> CIContext:
    """Build CIContext from a parsed harness-snapshot.json object."""
    kwargs: dict[str, str] = {"eval_run_id": eval_run_id}
    for key in _SNAPSHOT_TAG_FIELDS:
        val = data.get(key)
        if val is None or val == "":
            continue
        kwargs[key] = str(val).strip()
    return CIContext(**kwargs)


def find_harness_snapshot(run_dir: Path | str) -> Path | None:
    """Locate harness-snapshot.json under a run or case output directory.

    Preference: run root → cases/*/output/ → cases/*/ → any nested copy.
    """
    root = Path(run_dir)
    direct = root / HARNESS_SNAPSHOT_ARTIFACT
    if direct.is_file():
        return direct
    cases = root / "cases"
    if cases.is_dir():
        for case_dir in sorted(cases.iterdir()):
            if not case_dir.is_dir():
                continue
            for candidate in (
                case_dir / "output" / HARNESS_SNAPSHOT_ARTIFACT,
                case_dir / HARNESS_SNAPSHOT_ARTIFACT,
            ):
                if candidate.is_file():
                    return candidate
    for path in sorted(root.rglob(HARNESS_SNAPSHOT_ARTIFACT)):
        return path
    return None


def collect_from_snapshot_dir(
    run_dir: Path | str,
    *,
    eval_run_id: str = "",
) -> CIContext | None:
    """Load snapshot from run_dir if present; else None."""
    path = find_harness_snapshot(run_dir)
    if path is None:
        return None
    return context_from_snapshot(load_harness_snapshot(path), eval_run_id=eval_run_id)


def fetch_harness_snapshot(
    experiment_id: str,
    eval_run_id: str,
    *,
    client: Any | None = None,
    search_runs: Any | None = None,
) -> dict | None:
    """Download harness-snapshot.json from the MLflow run named ``eval_run_id``.

    Discovery matches ``inputs/`` enrichment: search runs where
    ``tags.mlflow.runName == eval_run_id``, then download the artifact.
    Returns parsed JSON or None if missing.
    """
    if not experiment_id or not eval_run_id:
        return None

    if search_runs is None or client is None:
        try:
            import mlflow
            from mlflow import MlflowClient
        except ImportError:
            return None
        if client is None:
            client = MlflowClient()
        if search_runs is None:
            search_runs = mlflow.search_runs

    try:
        runs = search_runs(
            experiment_ids=[experiment_id],
            filter_string=f"tags.mlflow.runName = '{eval_run_id}'",
        )
    except Exception:
        return None
    if runs is None or getattr(runs, "empty", True):
        return None

    for mlflow_run_id in list(runs.run_id):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                local = client.download_artifacts(
                    mlflow_run_id, HARNESS_SNAPSHOT_ARTIFACT, tmp
                )
            except Exception:
                continue
            path = Path(local)
            if path.is_dir():
                path = path / HARNESS_SNAPSHOT_ARTIFACT
            if path.is_file():
                return load_harness_snapshot(path)
    return None


def collect_ci_fallback(*, eval_run_id: str = "") -> CIContext:
    """Best-effort CI env when no snapshot file or MLflow artifact exists."""
    ref = _first(
        "AGENT_EVAL_VCS_REF_REVISION",
        "GITHUB_SHA",
        "CI_COMMIT_SHA",
        "BITBUCKET_COMMIT",
    )
    repo = _first("GITHUB_REPOSITORY", "CI_PROJECT_PATH")
    server = _first("GITHUB_SERVER_URL", "CI_SERVER_URL").rstrip("/")
    repo_url = _first("AGENT_EVAL_VCS_REPOSITORY_URL")
    if not repo_url and server and repo:
        repo_url = f"{server}/{repo}"

    run_id = _first(
        "GITHUB_RUN_ID",
        "CI_PIPELINE_ID",
        "BITBUCKET_BUILD_NUMBER",
    )
    run_url = _first("AGENT_EVAL_CICD_PIPELINE_RUN_URL")
    platform = _first("AGENT_EVAL_FORGE_PLATFORM") or _infer_platform()
    if not run_url and platform == "github" and server and repo and run_id:
        run_url = f"{server}/{repo}/actions/runs/{quote(run_id)}"
    elif not run_url and platform == "gitlab" and repo_url and run_id:
        run_url = f"{repo_url}/-/pipelines/{run_id}"

    return CIContext(
        eval_run_id=eval_run_id,
        forge_platform=platform,
        trace_id=_first("AGENT_EVAL_TRACE_ID"),
        repository_url=repo_url,
        ref_revision=ref,
        ref_name=_first(
            "GITHUB_REF_NAME",
            "CI_COMMIT_REF_NAME",
            "BITBUCKET_BRANCH",
        ),
        change_id=_first(
            "GITHUB_PR_NUMBER",
            "CI_MERGE_REQUEST_IID",
            "BITBUCKET_PR_ID",
        ),
        pipeline_run_id=run_id,
        pipeline_run_url=run_url,
    )


def collect_ci_context(
    *,
    eval_run_id: str = "",
    run_dir: Path | str | None = None,
    experiment_id: str | None = None,
    prefer_mlflow: bool = False,
    mlflow_client: Any | None = None,
) -> dict[str, str]:
    """MLflow tags from snapshot (disk and/or MLflow) with CI env last.

    Order when ``prefer_mlflow`` is False (``log_results`` handoff):
      1. ``harness-snapshot.json`` under ``run_dir``
      2. ``HARNESS_SNAPSHOT_PATH``
      3. MLflow artifact if ``experiment_id`` set
      4. CI env fallback

    Order when ``prefer_mlflow`` is True (later readers):
      1. MLflow artifact
      2. disk / ``HARNESS_SNAPSHOT_PATH``
      3. CI env fallback
    """
    def from_mlflow() -> dict[str, str] | None:
        if not experiment_id:
            return None
        data = fetch_harness_snapshot(
            experiment_id, eval_run_id, client=mlflow_client
        )
        if data is None:
            return None
        return context_from_snapshot(data, eval_run_id=eval_run_id).as_mlflow_tags()

    def from_disk() -> dict[str, str] | None:
        if run_dir is not None:
            ctx = collect_from_snapshot_dir(run_dir, eval_run_id=eval_run_id)
            if ctx is not None:
                return ctx.as_mlflow_tags()
        snap_path = os.environ.get("HARNESS_SNAPSHOT_PATH", "").strip()
        if snap_path and Path(snap_path).is_file():
            return context_from_snapshot(
                load_harness_snapshot(snap_path),
                eval_run_id=eval_run_id,
            ).as_mlflow_tags()
        return None

    if prefer_mlflow:
        order = (from_mlflow, from_disk)
    else:
        order = (from_disk, from_mlflow)

    for loader in order:
        tags = loader()
        if tags:
            return tags
    return collect_ci_fallback(eval_run_id=eval_run_id).as_mlflow_tags()


def merge_mlflow_tags(
    ci_tags: Mapping[str, str],
    config_tags: Mapping[str, str] | None,
) -> dict[str, str]:
    """Merge collected tags with eval.yaml tags; config wins on conflict."""
    merged = dict(ci_tags)
    if config_tags:
        merged.update({str(k): str(v) for k, v in config_tags.items()})
    return merged


def _first(*keys: str) -> str:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _infer_platform() -> str:
    if os.environ.get("GITHUB_ACTIONS"):
        return "github"
    if os.environ.get("GITLAB_CI"):
        return "gitlab"
    if os.environ.get("BITBUCKET_BUILD_NUMBER"):
        return "bitbucket"
    return ""
