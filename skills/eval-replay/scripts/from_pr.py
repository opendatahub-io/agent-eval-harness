#!/usr/bin/env python3
"""Generate eval cases from historical GitHub PRs.

Fetches PR metadata, diffs, and review comments via the `gh` CLI, then
writes a case directory per PR that eval-run can score against.

Usage:
    python3 from_pr.py --repo org/name --pr 123 [--pr 456] \
        --strategy review --output-dir /absolute/path/to/cases
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

GH_BIN = shutil.which("gh")
GIT_BIN = shutil.which("git")
if GH_BIN is None or GIT_BIN is None:
    _missing = [n for n, b in [("gh", GH_BIN), ("git", GIT_BIN)] if b is None]
    raise RuntimeError(f"Required executables not found: {', '.join(_missing)}")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ReviewComment:
    path: str
    line: int | None
    body: str
    author: str
    state: str  # review-level: APPROVED, CHANGES_REQUESTED, COMMENTED
    submitted_at: str = ""  # ISO 8601 timestamp for ordering


@dataclass
class PrMeta:
    number: int
    title: str
    body: str
    author: str
    state: str  # MERGED, OPEN, CLOSED
    base_ref: str
    head_ref: str
    head_sha: str
    merge_commit_sha: str
    reviewers: list[str]
    labels: list[str]
    merged_at: str
    changed_files: list[str]


# ---------------------------------------------------------------------------
# Forge adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ForgeAdapter(Protocol):
    def fetch_pr_meta(self, repo: str, pr: int) -> PrMeta: ...
    def fetch_diff(self, repo: str, pr: int) -> str: ...
    def fetch_reviews(self, repo: str, pr: int) -> list[ReviewComment]: ...
    def compute_merge_base(self, repo: str, pr: int) -> str: ...


# ---------------------------------------------------------------------------
# GitHub adapter (gh CLI)
# ---------------------------------------------------------------------------


class GitHubAdapter:
    """Fetch PR data via the gh CLI — no PyGitHub dependency."""

    def _gh(self, *args: str) -> str:
        result = subprocess.run(
            [GH_BIN, *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"gh {' '.join(args)} failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result.stdout

    def _gh_api(self, endpoint: str) -> Any:
        raw = self._gh("api", endpoint, "--paginate").strip()
        if not raw:
            return []
        chunks = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if len(chunks) == 1:
            return chunks[0]
        merged: list[Any] = []
        for chunk in chunks:
            merged.extend(chunk if isinstance(chunk, list) else [chunk])
        return merged

    def fetch_pr_meta(self, repo: str, pr: int) -> PrMeta:
        data = self._gh_api(f"repos/{repo}/pulls/{pr}")
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for PR #{pr}, got {type(data).__name__}")

        files_data = self._gh_api(f"repos/{repo}/pulls/{pr}/files")
        reviews_data = self._gh_api(f"repos/{repo}/pulls/{pr}/reviews")

        reviewers = sorted(
            {
                r["user"]["login"]
                for r in reviews_data
                if r.get("user") and r["state"] != "COMMENTED"
            }
        )

        state = "MERGED" if data.get("merged_at") else data["state"].upper()

        return PrMeta(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            author=data["user"]["login"],
            state=state,
            base_ref=data["base"]["ref"],
            head_ref=data["head"]["ref"],
            head_sha=data["head"]["sha"],
            merge_commit_sha=data.get("merge_commit_sha") or "",
            reviewers=reviewers,
            labels=[lbl["name"] for lbl in data.get("labels", [])],
            merged_at=data.get("merged_at") or "",
            changed_files=[f["filename"] for f in files_data],
        )

    def fetch_diff(self, repo: str, pr: int) -> str:
        return self._gh(
            "api",
            f"repos/{repo}/pulls/{pr}",
            "-H",
            "Accept: application/vnd.github.v3.diff",
        )

    def fetch_reviews(self, repo: str, pr: int) -> list[ReviewComment]:
        reviews = self._gh_api(f"repos/{repo}/pulls/{pr}/reviews")
        comments = self._gh_api(f"repos/{repo}/pulls/{pr}/comments")

        review_lookup: dict[int, dict[str, str]] = {
            r["id"]: {"state": r["state"], "submitted_at": r.get("submitted_at", "")}
            for r in reviews
        }

        result: list[ReviewComment] = []

        for c in comments:
            rid = c.get("pull_request_review_id")
            rl = review_lookup.get(rid, {}) if rid else {}
            result.append(
                ReviewComment(
                    path=c.get("path", ""),
                    line=c.get("original_line") or c.get("line"),
                    body=c.get("body", ""),
                    author=c["user"]["login"] if c.get("user") else "",
                    state=rl.get("state", "COMMENTED"),
                    submitted_at=rl.get("submitted_at", ""),
                )
            )

        for r in reviews:
            body = (r.get("body") or "").strip()
            if body:
                result.append(
                    ReviewComment(
                        path="",
                        line=None,
                        body=body,
                        author=r["user"]["login"] if r.get("user") else "",
                        state=r["state"],
                        submitted_at=r.get("submitted_at", ""),
                    )
                )

        return result

    def compute_merge_base(self, repo: str, pr: int) -> str:
        data = self._gh_api(f"repos/{repo}/pulls/{pr}")
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for PR #{pr}, got {type(data).__name__}")
        compare = self._gh_api(
            f"repos/{repo}/compare/{data['base']['sha']}...{data['head']['sha']}"
        )
        if not isinstance(compare, dict):
            return data["base"]["sha"]
        return compare.get("merge_base_commit", {}).get("sha", data["base"]["sha"])


# ---------------------------------------------------------------------------
# Shallow clone isolation
# ---------------------------------------------------------------------------


def _run_git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        [GIT_BIN, *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _purge_refs(clone_dir: Path) -> None:
    refs = _run_git("for-each-ref", "--format=%(refname)", cwd=clone_dir)
    for ref in refs.splitlines():
        if ref.strip():
            _run_git("update-ref", "-d", ref.strip(), cwd=clone_dir)
    _run_git("reflog", "expire", "--expire=now", "--all", cwd=clone_dir)
    _run_git("gc", "--prune=now", cwd=clone_dir)


def _check_contamination(clone_dir: Path, post_merge_sha: str) -> None:
    if not post_merge_sha:
        return
    check = subprocess.run(
        [GIT_BIN, "cat-file", "-t", post_merge_sha],
        capture_output=True,
        text=True,
        cwd=clone_dir,
    )
    if check.returncode == 0:
        print(
            f"WARNING: post-merge SHA {post_merge_sha[:12]} still reachable "
            f"— contamination prevention may have failed",
            file=sys.stderr,
        )


def create_isolated_clone(
    repo: str,
    merge_base_sha: str,
    post_merge_sha: str,
    clone_dir: Path,
) -> Path:
    """Create a contamination-safe shallow clone at the merge-base.

    clone_dir must not exist — caller is responsible for choosing a path
    that is not a descendant/ancestor of the ground truth case directory.
    """
    if clone_dir.exists() or clone_dir.is_symlink():
        raise FileExistsError(
            f"{clone_dir} already exists; remove it or use a fresh output directory"
        )

    clone_dir.mkdir(parents=True)
    repo_url = f"https://github.com/{repo}.git"

    _run_git("init", cwd=clone_dir)
    _run_git("remote", "add", "origin", repo_url, cwd=clone_dir)
    _run_git("fetch", "--depth", "1", "origin", merge_base_sha, cwd=clone_dir)
    _run_git("checkout", "FETCH_HEAD", cwd=clone_dir)
    _run_git("remote", "remove", "origin", cwd=clone_dir)

    _purge_refs(clone_dir)
    _check_contamination(clone_dir, post_merge_sha)

    return clone_dir


# ---------------------------------------------------------------------------
# Case generation
# ---------------------------------------------------------------------------

PROMPT_TEMPLATES: dict[str, str] = {
    "review": (
        "Review the following pull request.\n\n"
        "Title: {title}\n"
        "Description: {body}\n\n"
        "Changed files:\n{files}\n\n"
        "Diff:\n{diff}\n\n"
        "Provide your review with a verdict (approve or request_changes) "
        "and list any issues found with file paths and line numbers."
    ),
    "fix": (
        "Fix the issue described in this pull request.\n\n"
        "Title: {title}\n"
        "Description: {body}\n\n"
        "Files that need changes:\n{files}"
    ),
    "scan": (
        "Scan the following files for security vulnerabilities.\n\n"
        "Context: {title}\n"
        "Description: {body}\n\n"
        "Files to scan:\n{files}\n\n"
        "Diff:\n{diff}"
    ),
}


def _build_prompt(meta: PrMeta, strategy: str, diff: str = "") -> str:
    template = PROMPT_TEMPLATES.get(strategy)
    if template is None:
        raise ValueError(f"Unknown strategy: {strategy}")
    files_list = "\n".join(f"  - {f}" for f in meta.changed_files)
    return template.format(
        title=meta.title, body=meta.body, files=files_list, diff=diff
    )


def _derive_verdict(reviews: list[ReviewComment]) -> str:
    """Derive verdict from the latest non-comment review per reviewer.

    A reviewer may submit CHANGES_REQUESTED then later APPROVED; only
    their final actionable review state counts.
    """
    latest_by_reviewer: dict[str, str] = {}
    ordered = sorted(reviews, key=lambda r: r.submitted_at)
    for r in ordered:
        if r.author and r.state not in ("COMMENTED", ""):
            latest_by_reviewer[r.author] = r.state

    final_states = set(latest_by_reviewer.values())
    if "CHANGES_REQUESTED" in final_states:
        return "changes_requested"
    if "APPROVED" in final_states:
        return "approved"
    return "commented"


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def generate_case(
    adapter: ForgeAdapter,
    repo: str,
    pr: int,
    strategy: str,
    output_dir: Path,
    clone_root: Path | None = None,
    *,
    skip_clone: bool = False,
) -> Path:
    """Fetch PR data and write a single eval case directory.

    clone_root: if provided, clones go to clone_root/pr-<N>/ — a path
    with no filesystem relationship to output_dir, so the agent cannot
    traverse from the clone to ground truth.  Defaults to a temp dir.
    """
    meta = adapter.fetch_pr_meta(repo, pr)
    if meta.state != "MERGED":
        raise ValueError(
            f"PR #{pr} is {meta.state}, not MERGED — replay requires merged PRs"
        )
    diff = adapter.fetch_diff(repo, pr)
    reviews = adapter.fetch_reviews(repo, pr)
    merge_base = adapter.compute_merge_base(repo, pr)

    case_dir = output_dir / f"pr-{pr}"
    case_dir.mkdir(parents=True, exist_ok=True)

    repo_path = ""
    if not skip_clone:
        if clone_root is None:
            clone_root = Path(tempfile.mkdtemp(prefix="eval-replay-"))
        clone_target = clone_root / f"pr-{pr}"
        clone_dir = create_isolated_clone(
            repo, merge_base, meta.merge_commit_sha, clone_target
        )
        repo_path = str(clone_dir.resolve())

    input_data: dict[str, Any] = {
        "prompt": _build_prompt(
            meta, strategy, diff=diff if strategy in ("review", "scan") else ""
        ),
        "pr_title": meta.title,
        "pr_body": meta.body,
        "changed_files": meta.changed_files,
        "repo_path": repo_path,
        "strategy": strategy,
    }
    if strategy in ("review", "scan"):
        input_data["diff"] = diff

    _write_yaml(case_dir / "input.yaml", input_data)

    (case_dir / "reference.patch").write_text(diff)

    _write_yaml(
        case_dir / "annotations.yaml",
        {
            "pr_number": meta.number,
            "author": meta.author,
            "reviewers": meta.reviewers,
            "labels": meta.labels,
            "merge_timestamp": meta.merged_at,
            "verdict": _derive_verdict(reviews),
            "expected_files": sorted({r.path for r in reviews if r.path}),
            "review_comments": [
                {
                    "file": r.path,
                    "line": r.line,
                    "body": r.body,
                    "author": r.author,
                    "review_state": r.state,
                }
                for r in reviews
            ],
            "expected_diff": "reference.patch",
            "merge_base_sha": merge_base,
            "strategy": strategy,
        },
    )

    return case_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", required=True, help="GitHub repo (org/name)")
    parser.add_argument(
        "--pr",
        type=int,
        action="append",
        required=True,
        help="PR number (repeatable)",
    )
    parser.add_argument(
        "--strategy",
        choices=["review", "fix", "scan"],
        default="review",
        help="Evaluation strategy (default: review)",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Output directory for cases (absolute path)",
    )
    parser.add_argument(
        "--clone-dir",
        default=None,
        help=(
            "Root directory for repo clones, isolated from case data. "
            "Defaults to a temp directory so the agent cannot traverse "
            "from the clone to ground truth."
        ),
    )
    parser.add_argument(
        "--skip-clone",
        action="store_true",
        help="Skip shallow clone (use for testing without git)",
    )

    args = parser.parse_args()
    adapter = GitHubAdapter()
    output = Path(args.output_dir).resolve()

    if args.clone_dir:
        clone_root: Path | None = Path(args.clone_dir).resolve()
        clone_root.mkdir(parents=True, exist_ok=True)
    elif not args.skip_clone:
        clone_root = Path(tempfile.mkdtemp(prefix="eval-replay-"))
    else:
        clone_root = None

    if clone_root:
        print(f"Clones directory: {clone_root}", file=sys.stderr)

    for pr_num in args.pr:
        print(f"Generating case for PR #{pr_num}...", file=sys.stderr)
        case_dir = generate_case(
            adapter,
            args.repo,
            pr_num,
            args.strategy,
            output,
            clone_root=clone_root,
            skip_clone=args.skip_clone,
        )
        print(f"  -> {case_dir}", file=sys.stderr)

    print(f"Generated {len(args.pr)} case(s) in {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
