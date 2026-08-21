"""Tests for the soft dataset-audit preflight (workspace.py + harbor/tasks.py).

The preflight is informational, never a gate — and it is an opt-in contract:
a dataset that was NEVER audited (no dataset_audit.yaml) stays SILENT. It
WARNS on stderr only when an audit EXISTS and is stale or incomplete (stored
per-case CONTENT hashes disagree with the current case dirs, cases are
uncovered, or the file is corrupt) — and it must never raise or change exit
codes.
"""

from pathlib import Path

import pytest
import yaml

# conftest.py puts skills/eval-run/scripts on sys.path
import workspace

from agent_eval.dataset_audit import (
    AUDIT_FILENAME,
    case_content_hash,
    write_audit,
)
from agent_eval.harbor import tasks as harbor_tasks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_dataset(tmp_path, case_names=("case-001", "case-002")):
    dataset = tmp_path / "dataset"
    for name in case_names:
        case = dataset / name
        case.mkdir(parents=True)
        (case / "input.yaml").write_text(f"prompt: question for {name}\n")
    return dataset


def write_fresh_audit(dataset):
    """Write a minimal audit whose case_hashes match the current content."""
    case_dirs = sorted(d for d in dataset.iterdir() if d.is_dir())
    audit = {
        "audit_version": 1,
        "case_hashes": {d.name: case_content_hash(d) for d in case_dirs},
    }
    write_audit(audit, dataset)
    return case_dirs


# ---------------------------------------------------------------------------
# workspace.py preflight
# ---------------------------------------------------------------------------

class TestWorkspacePreflight:
    def test_silent_when_audit_never_existed(self, tmp_path, capsys):
        """Opt-in contract: a never-audited dataset produces NO warning."""
        dataset = make_dataset(tmp_path)
        case_dirs = sorted(d for d in dataset.iterdir() if d.is_dir())
        workspace._dataset_audit_preflight(dataset, case_dirs)
        assert capsys.readouterr().err == ""

    def test_silent_when_audit_fresh(self, tmp_path, capsys):
        dataset = make_dataset(tmp_path)
        case_dirs = write_fresh_audit(dataset)
        workspace._dataset_audit_preflight(dataset, case_dirs)
        assert capsys.readouterr().err == ""

    def test_warns_on_stale_hash_after_in_place_edit(self, tmp_path, capsys):
        """C3 end-to-end: an in-place input.yaml edit trips the preflight."""
        dataset = make_dataset(tmp_path)
        case_dirs = write_fresh_audit(dataset)
        (dataset / "case-001" / "input.yaml").write_text("prompt: edited\n")
        workspace._dataset_audit_preflight(dataset, case_dirs)
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "stale" in err
        assert "case-001" in err
        assert "case-002" not in err

    def test_warns_on_unrecorded_case(self, tmp_path, capsys):
        dataset = make_dataset(tmp_path)
        write_fresh_audit(dataset)
        new_case = dataset / "case-003"
        new_case.mkdir()
        (new_case / "input.yaml").write_text("prompt: new\n")
        case_dirs = sorted(d for d in dataset.iterdir() if d.is_dir())
        workspace._dataset_audit_preflight(dataset, case_dirs)
        err = capsys.readouterr().err
        assert "not covered" in err
        assert "case-003" in err

    def test_preflight_never_raises_on_corrupt_audit(self, tmp_path, capsys):
        dataset = make_dataset(tmp_path)
        (dataset / AUDIT_FILENAME).write_text("{{{[not yaml")
        case_dirs = sorted(d for d in dataset.iterdir() if d.is_dir())
        workspace._dataset_audit_preflight(dataset, case_dirs)  # no raise
        err = capsys.readouterr().err
        # An audit that EXISTS but is unreadable is incomplete, not
        # never-audited: it warns.
        assert "WARNING" in err
        assert "unreadable" in err

    def test_preflight_never_raises_on_bad_args(self, capsys):
        workspace._dataset_audit_preflight(None, None)  # no raise
        capsys.readouterr()

    def test_only_checks_selected_cases(self, tmp_path, capsys):
        """A --cases subset never warns about unselected stale cases."""
        dataset = make_dataset(tmp_path)
        write_fresh_audit(dataset)
        (dataset / "case-002" / "input.yaml").write_text("prompt: edited\n")
        workspace._dataset_audit_preflight(dataset, [dataset / "case-001"])
        assert capsys.readouterr().err == ""

    def test_audit_without_case_hashes_warns(self, tmp_path, capsys):
        dataset = make_dataset(tmp_path)
        (dataset / AUDIT_FILENAME).write_text(
            yaml.safe_dump({"audit_version": 1}))
        case_dirs = sorted(d for d in dataset.iterdir() if d.is_dir())
        workspace._dataset_audit_preflight(dataset, case_dirs)
        err = capsys.readouterr().err
        assert "case_hashes" in err


# ---------------------------------------------------------------------------
# harbor/tasks.py preflight (execution-path parity)
# ---------------------------------------------------------------------------

class TestHarborPreflight:
    def test_silent_when_audit_never_existed(self, tmp_path, capsys):
        """Opt-in contract parity with workspace.py: never audited = silent."""
        dataset = make_dataset(tmp_path)
        case_dirs = sorted(d for d in dataset.iterdir() if d.is_dir())
        harbor_tasks._dataset_audit_preflight(dataset, case_dirs)
        assert capsys.readouterr().err == ""

    def test_silent_when_audit_fresh(self, tmp_path, capsys):
        dataset = make_dataset(tmp_path)
        case_dirs = write_fresh_audit(dataset)
        harbor_tasks._dataset_audit_preflight(dataset, case_dirs)
        assert capsys.readouterr().err == ""

    def test_warns_on_stale_hash(self, tmp_path, capsys):
        dataset = make_dataset(tmp_path)
        case_dirs = write_fresh_audit(dataset)
        (dataset / "case-001" / "input.yaml").write_text("prompt: edited\n")
        harbor_tasks._dataset_audit_preflight(dataset, case_dirs)
        err = capsys.readouterr().err
        assert "stale" in err
        assert "case-001" in err

    def test_never_raises(self, capsys):
        harbor_tasks._dataset_audit_preflight(None, None)  # no raise
        capsys.readouterr()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
