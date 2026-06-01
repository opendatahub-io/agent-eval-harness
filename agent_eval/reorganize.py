"""Reorganize root-level eval config into eval/ directory layout."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from agent_eval.config import _is_valid_eval_name


@dataclass
class ReorganizationResult:
    """Result of a reorganization operation."""
    moved: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    target_config: Optional[Path] = None


def reorganize_root_config(project_root: Path, eval_name: str) -> ReorganizationResult:
    """Move root-level eval.yaml into eval/<eval_name>/ nested layout.

    Updates dataset.path references to be relative to the new config
    location. Does NOT rewrite outputs[].path (workspace-relative).
    """
    if not _is_valid_eval_name(eval_name):
        raise ValueError(f"Invalid eval name: {eval_name!r}")

    result = ReorganizationResult()
    source_config = project_root / "eval.yaml"

    if not source_config.is_file():
        raise FileNotFoundError(f"No eval.yaml at project root: {source_config}")

    target_dir = project_root / "eval" / eval_name
    target_config = target_dir / "eval.yaml"

    if target_config.exists():
        raise FileExistsError(f"Target already exists: {target_config}")

    target_dir.mkdir(parents=True, exist_ok=True)

    with open(source_config) as f:
        raw = yaml.safe_load(f) or {}

    old_dataset_path = raw.get("dataset", {}).get("path", "")

    if old_dataset_path and not Path(old_dataset_path).is_absolute():
        old_abs = (project_root / old_dataset_path).resolve()
        if old_abs.exists() and old_abs.is_relative_to(project_root.resolve()):
            new_rel = Path(os.path.relpath(old_abs, target_dir.resolve()))
            if "dataset" not in raw:
                raw["dataset"] = {}
            raw["dataset"]["path"] = str(new_rel)

    with open(target_config, "w") as f:
        yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)

    source_config.unlink()
    result.moved.append(("eval.yaml", str(target_config.relative_to(project_root))))

    source_md = project_root / "eval.md"
    if source_md.is_file():
        target_md = target_dir / "eval.md"
        target_md.write_text(source_md.read_text())
        source_md.unlink()
        result.moved.append(("eval.md", str(target_md.relative_to(project_root))))
    else:
        result.warnings.append("eval.md not found at project root, skipping")

    result.target_config = target_config
    return result
