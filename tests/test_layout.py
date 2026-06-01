"""Unit tests for infer_layout()."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import DiscoveryResult, infer_layout


def _dr(path_str: str, name: str = "s", is_root: bool = False):
    return DiscoveryResult(path=Path(path_str), eval_name=name, is_root=is_root)


def test_nested():
    configs = [
        _dr("eval/alpha/eval.yaml", "alpha"),
        _dr("eval/beta/eval.yaml", "beta"),
    ]
    assert infer_layout(configs) == "nested"


def test_flat():
    configs = [
        _dr("eval/alpha.yaml", "alpha"),
        _dr("eval/beta.yaml", "beta"),
    ]
    assert infer_layout(configs) == "flat"


def test_root():
    configs = [_dr("eval.yaml", "my-skill", is_root=True)]
    assert infer_layout(configs) == "root"


def test_mixed_root_and_nested():
    configs = [
        _dr("eval.yaml", "root-skill", is_root=True),
        _dr("eval/nested/eval.yaml", "nested-skill"),
    ]
    assert infer_layout(configs) == "mixed"


def test_mixed_nested_and_flat():
    configs = [
        _dr("eval/nested/eval.yaml", "nested-skill"),
        _dr("eval/flat.yaml", "flat-skill"),
    ]
    assert infer_layout(configs) == "mixed"


def test_none():
    assert infer_layout([]) == "none"
