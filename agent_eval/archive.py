"""ResultsArchiver — experiment archival with preflight validation.

Preflight validates RHAI_RESULTS_REPO early (Phase 0, not Phase 8).
Headless mode fails fast if the env var is missing or invalid.
On archival failure, falls back to /tmp/agent-eval-unarchived/{experiment_id}/.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FALLBACK_DIR = Path("/tmp/agent-eval-unarchived")


def _safe_child_name(value: str) -> str:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or path.name != value
        or value in {".", ".."}
        or ".." in path.parts
    ):
        raise ValueError(f"Invalid experiment_id: {value!r}")
    return value


class ResultsArchiver:
    """Archives experiment results to a git-backed results repo."""

    def __init__(self, repo_path: Path | None = None) -> None:
        self.repo_path = Path(repo_path).resolve() if repo_path else None

    @staticmethod
    def validate_repo(repo_path: Path) -> bool:
        repo_path = Path(repo_path).resolve()
        if not repo_path.is_dir():
            return False
        if not (repo_path / ".git").exists():
            return False
        return True

    @staticmethod
    def resolve_repo_path(interactive: bool = True) -> Path:
        env_val = os.environ.get("RHAI_RESULTS_REPO")
        if env_val:
            repo = Path(env_val).resolve()
            if not ResultsArchiver.validate_repo(repo):
                raise ValueError(
                    f"RHAI_RESULTS_REPO={env_val} is not a valid git repository"
                )
            return repo

        if not interactive:
            raise ValueError(
                "RHAI_RESULTS_REPO is not set and running in headless mode. "
                "Set the environment variable to a valid git repo path."
            )

        user_path = input("Enter path to results repo: ").strip()
        repo = Path(user_path).resolve()
        if not ResultsArchiver.validate_repo(repo):
            raise ValueError(f"{user_path} is not a valid git repository")
        return repo

    def archive_experiment(
        self,
        experiment_id: str,
        data: dict[str, Any],
        *,
        fallback: bool = True,
    ) -> Path:
        safe_experiment_id = _safe_child_name(experiment_id)
        if self.repo_path and self.validate_repo(self.repo_path):
            exp_dir = self.repo_path / safe_experiment_id
            exp_dir.mkdir(parents=True, exist_ok=True)
            result_file = exp_dir / "results.json"
            result_file.write_text(json.dumps(data, indent=2, default=str))
            logger.info("Archived to %s", result_file)
            return result_file

        if not fallback:
            raise ValueError(
                f"Cannot archive: repo path {self.repo_path} is not valid "
                "and fallback is disabled"
            )

        fallback_dir = FALLBACK_DIR / safe_experiment_id
        fallback_dir.mkdir(parents=True, exist_ok=True)
        result_file = fallback_dir / "results.json"
        result_file.write_text(json.dumps(data, indent=2, default=str))
        logger.warning(
            "Archival to repo failed. Results saved to fallback: %s", result_file
        )
        return result_file
