"""Tests for path segment validation (CWE-22 prevention)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import _validate_path_segment, _is_valid_eval_name


class TestValidatePathSegment:
    """Test _validate_path_segment helper."""

    def test_valid_run_ids(self):
        """Valid timestamp-style run_ids should pass through unchanged."""
        valid = [
            "2026-06-01-opus",
            "test-run-123",
            "my_eval_run",
            "run.2026.06.01",
        ]
        for value in valid:
            assert _validate_path_segment(value, "run_id") == value

    def test_rejects_forward_slash(self):
        with pytest.raises(ValueError, match="path separator"):
            _validate_path_segment("foo/bar", "run_id")

    def test_rejects_backslash(self):
        with pytest.raises(ValueError, match="path separator"):
            _validate_path_segment("foo\\bar", "run_id")

    def test_rejects_parent_directory(self):
        with pytest.raises(ValueError, match="directory reference"):
            _validate_path_segment("..", "run_id")

    def test_rejects_current_directory(self):
        with pytest.raises(ValueError, match="directory reference"):
            _validate_path_segment(".", "run_id")

    def test_rejects_path_traversal_attack(self):
        """Classic CWE-22 path traversal attempts."""
        attacks = [
            "../../../tmp/evil",
            "../../etc/passwd",
            "foo/../bar",
        ]
        for attack in attacks:
            with pytest.raises(ValueError):
                _validate_path_segment(attack, "run_id")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_path_segment("", "run_id")

    def test_rejects_none(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_path_segment(None, "run_id")

    def test_rejects_control_characters(self):
        with pytest.raises(ValueError, match="invalid characters"):
            _validate_path_segment("run\x00id", "run_id")

    def test_error_includes_param_name(self):
        """Error messages should reference the parameter name."""
        with pytest.raises(ValueError, match="--run-id"):
            _validate_path_segment("../evil", "--run-id")

    def test_dotfile_names_allowed(self):
        """Names starting with dot (but not . or ..) should be allowed."""
        assert _validate_path_segment(".hidden-run", "run_id") == ".hidden-run"

    def test_consistent_with_is_valid_eval_name(self):
        """_validate_path_segment should accept exactly what
        _is_valid_eval_name accepts."""
        cases = [
            "valid-name",
            "2026-06-01-opus",
            "name_with_underscores",
            "name.with.dots",
        ]
        for case in cases:
            assert _is_valid_eval_name(case) is True
            assert _validate_path_segment(case, "test") == case

        invalid = [
            "../traversal",
            "has/slash",
            "has\\backslash",
            "..",
            ".",
        ]
        for case in invalid:
            assert _is_valid_eval_name(case) is False
            with pytest.raises(ValueError):
                _validate_path_segment(case, "test")
