"""Tests for agent_eval/reliability.py — chance-corrected IRR primitives.

Oracle fixtures are vendored inline with attribution. Pinned coefficient
values were cross-checked at authoring time against the PyPI ``krippendorff``
package (dev-only scratch venv, never a runtime dep) and agree to 6 decimals.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_eval.reliability import (
    DEFAULT_MIN_UNITS,
    INTERVAL,
    LEVELS,
    NOMINAL,
    ORDINAL,
    REASON_BELOW_FLOOR,
    REASON_INSUFFICIENT_DATA,
    REASON_PERFECT_AGREEMENT,
    REASON_UNDEFINED,
    ConfidenceInterval,
    IRRResult,
    bootstrap_ci,
    cohen_kappa,
    fleiss_kappa,
    krippendorff_alpha,
    select_irr_metric,
)

# ---------------------------------------------------------------------------
# Oracle fixtures (vendored, attributed)
# ---------------------------------------------------------------------------

# Klaus Krippendorff, "Computing Krippendorff's Alpha-Reliability" (2011),
# University of Pennsylvania, Annenberg School for Communication departmental
# paper. The canonical 4-observer (A-D) x 12-unit worked example; '-' entries
# become None (missing). Stored transposed as unit rows [A, B, C, D].
# Authoring-time cross-check vs PyPI `krippendorff`: nominal 0.743421,
# interval 0.849107, ordinal 0.815388 (this module agrees to 6 decimals).
_K2011_A = [1, 2, 3, 3, 2, 1, 4, 1, 2, None, None, None]
_K2011_B = [1, 2, 3, 3, 2, 2, 4, 1, 2, 5, None, 3]
_K2011_C = [None, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, None]
_K2011_D = [1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, None]
KRIPPENDORFF_2011 = [
    list(col) for col in zip(_K2011_A, _K2011_B, _K2011_C, _K2011_D)
]

# Reconstructed from the Apache-2.0 replication repository of "Measurement
# Without Validity" (arXiv 2608.00794):
#   github.com/williamcaban/experiment-measurement-without-validity
# (author_codes.csv + results.csv; PARSE_ERR -> missing). 20 units (Q4),
# raters per row: [author, qwen, gemma, nemotron]; categories OK/MM/INC/ABS;
# None = missing. Reproduces all 7 published alphas (4-way 0.886 + six
# pairwise), cross-checked vs PyPI `krippendorff` to 6 decimals.
# License of the source data: Apache-2.0.
OK, MM, INC, ABS = "OK", "MM", "INC", "ABS"
REPLICATION_REPO_Q4 = [
    [ABS, ABS, ABS, ABS],     # A1
    [ABS, ABS, ABS, ABS],     # A2
    [OK, OK, OK, INC],        # B1
    [OK, OK, OK, OK],         # C1
    [OK, OK, OK, None],       # C4
    [OK, MM, OK, None],       # C5
    [MM, MM, INC, None],      # D1
    [MM, MM, MM, MM],         # D2
    [INC, INC, INC, INC],     # E1
    [INC, INC, INC, INC],     # E3
    [ABS, ABS, ABS, ABS],     # F3
    [ABS, ABS, ABS, ABS],     # F4
    [ABS, ABS, ABS, ABS],     # G1
    [ABS, ABS, ABS, ABS],     # G2
    [MM, MM, MM, MM],         # H1
    [INC, INC, INC, INC],     # I0
    [OK, OK, OK, None],       # I1
    [ABS, ABS, ABS, ABS],     # I2
    [ABS, ABS, ABS, ABS],     # I3
    [ABS, ABS, ABS, ABS],     # I4
]

_RELIABILITY_PATH = (
    Path(__file__).parent.parent / "agent_eval" / "reliability.py"
)


def _q4_pair(i: int, j: int) -> list:
    return [[row[i], row[j]] for row in REPLICATION_REPO_Q4]


# ---------------------------------------------------------------------------
# Krippendorff alpha — known-value oracles
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "level,expected",
    [(NOMINAL, 0.743), (INTERVAL, 0.849), (ORDINAL, 0.815)],
)
def test_krippendorff_2011_oracle(level, expected):
    result = krippendorff_alpha(KRIPPENDORFF_2011, level)
    assert result.metric == "krippendorff_alpha"
    assert result.level == level
    assert round(result.value, 3) == expected
    # Unit 12 has a single rating -> dropped: 11 pairable units, 40 ratings.
    assert result.n_units == 11
    assert result.n_ratings == 40
    assert result.reason_code is None


def test_replication_repo_four_way_nominal():
    result = krippendorff_alpha(REPLICATION_REPO_Q4, NOMINAL)
    assert round(result.value, 3) == 0.886
    assert result.n_units == 20
    assert result.n_ratings == 76  # 3 nemotron ratings missing + 1 more None


@pytest.mark.parametrize(
    "i,j,expected",
    [
        (0, 1, 0.930),  # author-qwen
        (0, 2, 0.929),  # author-gemma
        (0, 3, 0.901),  # author-nemotron
        (1, 2, 0.859),  # qwen-gemma
        (1, 3, 0.901),  # qwen-nemotron
        (2, 3, 0.901),  # gemma-nemotron
    ],
)
def test_replication_repo_pairwise(i, j, expected):
    result = krippendorff_alpha(_q4_pair(i, j), NOMINAL)
    assert round(result.value, 3) == expected


def test_alpha_systematic_disagreement_exact():
    # Two units, ratings swapped: D_o = 1, D_e = 2/3 -> alpha = 1 - 3/2 = -0.5
    result = krippendorff_alpha([["a", "b"], ["b", "a"]], NOMINAL)
    assert result.value == -0.5


def test_alpha_missing_row_equals_none_free_equivalent():
    with_none = krippendorff_alpha([[3, 3, None], [3, 4]], NOMINAL)
    without = krippendorff_alpha([[3, 3], [3, 4]], NOMINAL)
    assert with_none.value == without.value
    assert with_none.n_ratings == without.n_ratings == 4


def test_alpha_unpairable_unit_dropped_from_counts():
    base = [[1, 2], [2, 2], [1, 1]]
    padded = base + [[5], [None, None], []]
    r_base = krippendorff_alpha(base, NOMINAL)
    r_padded = krippendorff_alpha(padded, NOMINAL)
    assert r_padded.n_units == r_base.n_units == 3
    assert r_padded.value == r_base.value


# ---------------------------------------------------------------------------
# Kappas — known-value oracles
# ---------------------------------------------------------------------------

def test_fleiss_wikipedia_oracle():
    # Wikipedia "Fleiss' kappa" worked example: 10 subjects x 14 raters x 5
    # categories, expanded from the n_ij count table -> kappa = 0.210.
    counts = [
        [0, 0, 0, 0, 14], [0, 2, 6, 4, 2], [0, 0, 3, 5, 6], [0, 3, 9, 2, 0],
        [2, 2, 8, 1, 1], [7, 7, 0, 0, 0], [3, 2, 6, 3, 0], [2, 5, 3, 2, 2],
        [6, 5, 2, 1, 0], [0, 2, 2, 3, 7],
    ]
    units = [
        [cat for cat, c in enumerate(row) for _ in range(c)] for row in counts
    ]
    result = fleiss_kappa(units)
    assert result.metric == "fleiss_kappa"
    assert round(result.value, 3) == 0.210
    assert result.n_units == 10
    assert result.n_raters == 14
    assert result.level is None


def test_cohen_known_value():
    # Confusion matrix [[20, 5], [10, 15]]: p_o = 0.7, p_e = 0.5 -> 0.4
    a = ["x"] * 25 + ["y"] * 25
    b = ["x"] * 20 + ["y"] * 5 + ["x"] * 10 + ["y"] * 15
    result = cohen_kappa(a, b)
    assert result.value == pytest.approx(0.4)
    assert result.n_units == 50
    assert result.n_ratings == 100
    assert result.n_raters == 2
    assert result.level is None


def test_cohen_chance_agreement_zero():
    result = cohen_kappa(["x", "x", "y", "y"], ["x", "y", "x", "y"])
    assert result.value == 0.0


# ---------------------------------------------------------------------------
# Degenerate handling — the reason_code vocabulary
# ---------------------------------------------------------------------------

def test_reason_code_vocabulary_is_snake_case():
    for code in (REASON_PERFECT_AGREEMENT, REASON_INSUFFICIENT_DATA,
                 REASON_BELOW_FLOOR, REASON_UNDEFINED):
        assert "-" not in code
        assert code == code.lower()
    assert REASON_PERFECT_AGREEMENT == "perfect_agreement"
    assert REASON_INSUFFICIENT_DATA == "insufficient_data"
    assert REASON_BELOW_FLOOR == "below_floor"
    assert REASON_UNDEFINED == "undefined"


@pytest.mark.parametrize(
    "result",
    [
        krippendorff_alpha([[1, 1], [1, 1, 1]], NOMINAL),
        krippendorff_alpha([[2, 2], [2, 2]], INTERVAL),
        krippendorff_alpha([[3, 3, 3], [3, 3]], ORDINAL),
        fleiss_kappa([["a", "a", "a"], ["a", "a", "a"]]),
        cohen_kappa(["a", "a", "a"], ["a", "a", "a"]),
    ],
    ids=["alpha-nominal", "alpha-interval", "alpha-ordinal", "fleiss",
         "cohen"],
)
def test_perfect_agreement_degenerate(result):
    assert result.value is None
    assert result.reason_code == REASON_PERFECT_AGREEMENT
    assert result.reason  # human prose present


def test_all_singleton_units_insufficient_data():
    result = krippendorff_alpha([[1], [2], [3], [None, 4]], NOMINAL)
    assert result.value is None
    assert result.reason_code == REASON_INSUFFICIENT_DATA
    assert result.n_units == 0


def test_empty_input_insufficient_data():
    assert krippendorff_alpha([]).reason_code == REASON_INSUFFICIENT_DATA
    assert fleiss_kappa([]).reason_code == REASON_INSUFFICIENT_DATA
    assert cohen_kappa([], []).reason_code == REASON_INSUFFICIENT_DATA


def test_single_pairable_unit_insufficient_data():
    result = krippendorff_alpha([[1, 2], [3], [None, None]], NOMINAL)
    assert result.reason_code == REASON_INSUFFICIENT_DATA
    assert result.n_units == 1


@pytest.mark.parametrize(
    "result",
    [
        krippendorff_alpha([[1, 2]] * 5, NOMINAL, min_units=10),
        fleiss_kappa([["a", "b"]] * 5, min_units=10),
        cohen_kappa(["a"] * 5, ["a", "b", "a", "b", "a"], min_units=10),
    ],
    ids=["alpha", "fleiss", "cohen"],
)
def test_policy_floor_below_floor(result):
    assert result.value is None
    assert result.reason_code == REASON_BELOW_FLOOR
    assert result.n_units == 5


def test_structural_floor_beats_policy_floor():
    # 1 pairable unit under min_units=10: structural insufficiency wins.
    result = krippendorff_alpha([[1, 2]], NOMINAL, min_units=10)
    assert result.reason_code == REASON_INSUFFICIENT_DATA


def test_default_min_units_allows_two_argument_calls():
    assert DEFAULT_MIN_UNITS == 2
    # Two-argument (and positional-free kappa) calls must work.
    assert krippendorff_alpha([[1, 2], [2, 2]], NOMINAL).value is not None
    assert fleiss_kappa([["a", "b"], ["a", "a"]]).value is not None
    assert cohen_kappa(["a", "b"], ["a", "a"]).value is not None


# ---------------------------------------------------------------------------
# Misuse — loud ValueErrors
# ---------------------------------------------------------------------------

def test_fleiss_rejects_missing_pointing_at_alpha():
    with pytest.raises(ValueError, match="krippendorff_alpha"):
        fleiss_kappa([["a", "b", None], ["a", "b", "a"]])


def test_fleiss_rejects_ragged_pointing_at_alpha():
    with pytest.raises(ValueError, match="krippendorff_alpha"):
        fleiss_kappa([["a", "b"], ["a", "b", "a"]])


def test_fleiss_rejects_single_rater():
    with pytest.raises(ValueError, match="at least 2 raters"):
        fleiss_kappa([["a"], ["b"]])


def test_cohen_rejects_none_entries():
    with pytest.raises(ValueError, match="None"):
        cohen_kappa(["a", None], ["a", "b"])
    with pytest.raises(ValueError, match="None"):
        cohen_kappa(["a", "b"], [None, "b"])


def test_cohen_rejects_length_mismatch():
    with pytest.raises(ValueError, match="equal-length"):
        cohen_kappa(["a"], ["a", "b"])


def test_alpha_rejects_unknown_level():
    with pytest.raises(ValueError, match="unknown level"):
        krippendorff_alpha([[1, 2], [2, 2]], "ratio")


@pytest.mark.parametrize("level", [ORDINAL, INTERVAL])
def test_alpha_rejects_bool_and_non_numeric_on_metric_levels(level):
    # bool is a subclass of int (the bool-is-int trap): reject loudly.
    with pytest.raises(ValueError, match="numeric"):
        krippendorff_alpha([[True, 1], [0, 1]], level)
    with pytest.raises(ValueError, match="numeric"):
        krippendorff_alpha([["a", "b"], ["a", "a"]], level)


def test_alpha_nominal_tolerates_any_hashable():
    result = krippendorff_alpha([[True, "x"], ["x", True]], NOMINAL)
    assert result.value is not None


def test_alpha_nominal_true_one_hash_merge_quirk():
    # Documented quirk: True == 1 and hash(True) == hash(1), so True and 1
    # merge into one nominal category -> perfect agreement here.
    result = krippendorff_alpha([[True, 1], [1, True]], NOMINAL)
    assert result.value is None
    assert result.reason_code == REASON_PERFECT_AGREEMENT


# ---------------------------------------------------------------------------
# select_irr_metric — Figure-1 decision table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "n_raters,varying,complete,scale,expected",
    [
        # Non-nominal scale always -> alpha (checked before panel design).
        (4, True, False, ORDINAL, "krippendorff_alpha"),
        (2, False, True, INTERVAL, "krippendorff_alpha"),
        (5, False, True, ORDINAL, "krippendorff_alpha"),  # ordinal beats panel
        # Incomplete matrix -> alpha.
        (3, False, False, NOMINAL, "krippendorff_alpha"),
        (2, False, False, NOMINAL, "krippendorff_alpha"),
        # Varying rater identity -> alpha.
        (2, True, True, NOMINAL, "krippendorff_alpha"),
        (5, True, True, NOMINAL, "krippendorff_alpha"),
        # Two fixed raters, complete nominal -> Cohen.
        (2, False, True, NOMINAL, "cohen_kappa"),
        # Fixed panel, complete nominal -> Fleiss.
        (3, False, True, NOMINAL, "fleiss_kappa"),
        (5, False, True, NOMINAL, "fleiss_kappa"),
    ],
)
def test_select_irr_metric_decision_table(
    n_raters, varying, complete, scale, expected
):
    metric, rationale = select_irr_metric(n_raters, varying, complete, scale)
    assert metric == expected
    assert rationale


def test_select_irr_metric_rejects_single_rater():
    with pytest.raises(ValueError, match="at least 2 raters"):
        select_irr_metric(1, True, True, NOMINAL)


def test_select_irr_metric_rejects_unknown_scale():
    with pytest.raises(ValueError, match="unknown scale"):
        select_irr_metric(2, False, True, "ratio")


def test_rationale_hygiene():
    # No Landis-Koch adjectives, no "Sec 6.4"; the paper citation is
    # Sec 5.3 or Appendix A.1.
    banned = ["landis", "slight", "fair", "moderate", "substantial",
              "almost perfect", "6.4"]
    for n_raters in (2, 3, 5):
        for varying in (True, False):
            for complete in (True, False):
                for scale in sorted(LEVELS):
                    _, rationale = select_irr_metric(
                        n_raters, varying, complete, scale
                    )
                    lowered = rationale.lower()
                    assert rationale.strip()
                    for word in banned:
                        assert word not in lowered, (word, rationale)
                    assert "sec 5.3" in lowered or "a.1" in lowered


# ---------------------------------------------------------------------------
# Ordinal-vs-nominal divergence
# ---------------------------------------------------------------------------

def test_ordinal_rewards_near_misses_over_nominal():
    adjacent = [[1, 1], [2, 2], [3, 3], [4, 4], [5, 5],
                [1, 2], [2, 3], [3, 4], [4, 5], [2, 2]]
    nominal = krippendorff_alpha(adjacent, NOMINAL).value
    ordinal = krippendorff_alpha(adjacent, ORDINAL).value
    assert ordinal > nominal


def test_ordinal_penalizes_extreme_disagreement():
    adjacent = [[1, 1], [2, 2], [3, 3], [4, 4], [5, 5],
                [1, 2], [2, 3], [3, 4], [4, 5], [2, 2]]
    extreme = [[1, 1], [2, 2], [3, 3], [4, 4], [5, 5],
               [1, 5], [2, 5], [1, 4], [1, 5], [2, 2]]
    assert (
        krippendorff_alpha(extreme, ORDINAL).value
        < krippendorff_alpha(adjacent, ORDINAL).value
    )


# ---------------------------------------------------------------------------
# Bootstrap CI
# ---------------------------------------------------------------------------

def _alpha_stat(rows):
    return krippendorff_alpha(rows, NOMINAL).value


def test_bootstrap_deterministic_under_seed():
    ci1 = bootstrap_ci(REPLICATION_REPO_Q4, _alpha_stat, n_boot=300, seed=42)
    ci2 = bootstrap_ci(REPLICATION_REPO_Q4, _alpha_stat, n_boot=300, seed=42)
    assert (ci1.low, ci1.high, ci1.n_effective) == (
        ci2.low, ci2.high, ci2.n_effective
    )
    assert ci1.seed == 42


def test_bootstrap_brackets_point_estimate():
    point = krippendorff_alpha(REPLICATION_REPO_Q4, NOMINAL).value
    ci = bootstrap_ci(REPLICATION_REPO_Q4, _alpha_stat, n_boot=500, seed=0)
    assert ci.low <= point <= ci.high
    assert -1.0 <= ci.low <= ci.high <= 1.0
    assert ci.method == "bootstrap-percentile"


def test_bootstrap_skips_degenerate_resamples():
    # Resamples that miss the sole disagreeing unit are perfect-agreement
    # degenerates (statistic returns None) and must be skipped, not counted.
    units = [[1, 1], [1, 1], [1, 2]]
    ci = bootstrap_ci(units, _alpha_stat, n_boot=200, seed=0)
    assert ci is not None
    assert 0 < ci.n_effective < ci.n_boot == 200


def test_bootstrap_all_degenerate_returns_none():
    assert bootstrap_ci([[1, 1], [1, 1], [1, 1]], _alpha_stat,
                        n_boot=100, seed=0) is None


def test_bootstrap_too_few_units_returns_none():
    assert bootstrap_ci([[1, 2]], _alpha_stat, n_boot=100, seed=0) is None
    assert bootstrap_ci([], _alpha_stat, n_boot=100, seed=0) is None


# ---------------------------------------------------------------------------
# IRRResult serialization
# ---------------------------------------------------------------------------

def test_to_dict_rounds_display_value_keeps_attribute_precision():
    result = krippendorff_alpha(KRIPPENDORFF_2011, NOMINAL)
    d = result.to_dict()
    assert d["value"] == 0.743
    assert result.value != d["value"]  # attribute keeps full precision
    assert round(result.value, 6) == 0.743421


def test_to_dict_omits_none_optional_keys():
    computable = krippendorff_alpha(KRIPPENDORFF_2011, NOMINAL).to_dict()
    for absent in ("n_raters", "reason_code", "reason", "rationale", "ci",
                   "ci_reason"):
        assert absent not in computable
    assert computable["level"] == NOMINAL

    degenerate = krippendorff_alpha([[1], [2]], NOMINAL).to_dict()
    assert degenerate["value"] is None  # value key always present
    assert degenerate["reason_code"] == REASON_INSUFFICIENT_DATA
    assert "ci" not in degenerate

    kappa = cohen_kappa(["a", "b"], ["a", "a"]).to_dict()
    assert kappa["n_raters"] == 2
    assert "level" not in kappa


def test_to_dict_yaml_safe_round_trip():
    ci = bootstrap_ci(REPLICATION_REPO_Q4, _alpha_stat, n_boot=200, seed=1)
    result = krippendorff_alpha(REPLICATION_REPO_Q4, NOMINAL)
    result.ci = ci
    result.rationale = "test rationale"
    d = result.to_dict()
    round_tripped = yaml.safe_load(yaml.safe_dump(d))
    assert round_tripped == d
    assert round_tripped["ci"]["low"] == round(ci.low, 3)
    assert round_tripped["ci"]["high"] == round(ci.high, 3)
    assert round_tripped["ci"]["n_effective"] == ci.n_effective


def test_irr_result_and_ci_are_the_documented_contracts():
    result = IRRResult(metric="krippendorff_alpha", value=0.5, n_units=3,
                       n_ratings=6)
    assert result.reason_code is None
    assert isinstance(
        ConfidenceInterval(low=0.1, high=0.9, confidence=0.95).to_dict(), dict
    )


# ---------------------------------------------------------------------------
# Purity guards — reliability.py must stay pure stdlib
# ---------------------------------------------------------------------------

_STDLIB_ALLOWLIST = {
    "__future__", "math", "random", "statistics", "dataclasses",
    "collections", "itertools", "typing",
}


def test_reliability_imports_are_stdlib_only():
    tree = ast.parse(_RELIABILITY_PATH.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports on the scoring path"
            imported.add(node.module.split(".")[0])
    assert imported <= _STDLIB_ALLOWLIST, imported - _STDLIB_ALLOWLIST
    assert _STDLIB_ALLOWLIST <= (set(sys.stdlib_module_names) | {"__future__"})


def test_reliability_imports_on_bare_interpreter(repo_root):
    # -I (isolated) drops PYTHONPATH/user-site: the module must import with
    # nothing but the stdlib and the repo root on sys.path.
    code = (
        f"import sys; sys.path.insert(0, {str(repo_root)!r}); "
        "import agent_eval.reliability"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
