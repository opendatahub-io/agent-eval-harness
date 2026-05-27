"""Tests for split_dataset.py edge cases."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "skills/eval-optimize/scripts"))

from split_dataset import split_cases


def test_single_case_all_splits_populated():
    result = split_cases(["a"], "40:20:40")
    assert len(result["train"]) >= 1
    assert len(result["selection"]) >= 1
    assert len(result["test"]) >= 1


def test_two_cases_all_splits_populated():
    result = split_cases(["a", "b"], "40:20:40")
    assert len(result["train"]) >= 1
    assert len(result["selection"]) >= 1
    assert len(result["test"]) >= 1


def test_tiny_dataset_uses_all_cases_everywhere():
    result = split_cases(["a", "b"], "40:20:40")
    assert set(result["train"]) == {"a", "b"}
    assert set(result["selection"]) == {"a", "b"}
    assert set(result["test"]) == {"a", "b"}


def test_small_dataset_train_selection_overlap():
    cases = [f"case-{i:03d}" for i in range(5)]
    result = split_cases(cases, "40:20:40")
    assert len(result["train"]) >= 1
    assert result["selection"] == result["train"]
    assert len(result["test"]) >= 1


def test_normal_dataset_no_overlap():
    cases = [f"case-{i:03d}" for i in range(20)]
    result = split_cases(cases, "40:20:40")
    train_set = set(result["train"])
    sel_set = set(result["selection"])
    test_set = set(result["test"])
    assert train_set.isdisjoint(sel_set)
    assert train_set.isdisjoint(test_set)
    assert sel_set.isdisjoint(test_set)
    assert train_set | sel_set | test_set == set(cases)


def test_deterministic_with_same_seed():
    cases = [f"case-{i:03d}" for i in range(20)]
    r1 = split_cases(cases, "40:20:40", seed=42)
    r2 = split_cases(cases, "40:20:40", seed=42)
    assert r1["train"] == r2["train"]
    assert r1["selection"] == r2["selection"]
    assert r1["test"] == r2["test"]


def test_different_seed_different_split():
    cases = [f"case-{i:03d}" for i in range(20)]
    r1 = split_cases(cases, "40:20:40", seed=42)
    r2 = split_cases(cases, "40:20:40", seed=99)
    assert r1["train"] != r2["train"]


def test_ratio_respected_approximately():
    cases = [f"case-{i:03d}" for i in range(100)]
    result = split_cases(cases, "40:20:40")
    assert 35 <= len(result["train"]) <= 45
    assert 15 <= len(result["selection"]) <= 25
    assert 35 <= len(result["test"]) <= 45


def test_all_cases_accounted_for():
    cases = [f"case-{i:03d}" for i in range(20)]
    result = split_cases(cases, "40:20:40")
    all_assigned = result["train"] + result["selection"] + result["test"]
    assert sorted(all_assigned) == sorted(cases)
