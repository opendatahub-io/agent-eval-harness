"""Chance-corrected inter-rater reliability (IRR) — pure stdlib.

Implements the reliability toolkit prescribed by "Measurement Without
Validity" (arXiv 2608.00794): Krippendorff's alpha (nominal / ordinal /
interval, missing-data tolerant via the coincidence-matrix formulation),
Fleiss' kappa (complete nominal matrices only), Cohen's kappa (two fixed
raters), the paper's Figure-1 metric-selection tree, and unit-level bootstrap
percentile confidence intervals. No third-party imports — this module sits on
the scoring path, which must stay stdlib-only.

Unit rows
    Every coefficient consumes *units*: an iterable of per-unit rating rows.
    ``None`` marks a missing rating; ragged rows are allowed for
    ``krippendorff_alpha``. Consumers building rows from judge sampling data
    use the recipe ``stability.values + [None] * error_count`` — an errored
    rating is *missing*, never a nominal category of its own. The same rule
    applies to pairwise verdicts: an errored verdict is a missing rating,
    not a category.

Labeling (mandatory for consumers)
    When the "raters" are k samples of one judge model, the resulting alpha
    MUST be labeled "single-judge self-consistency alpha (upper bound on
    inter-rater reliability)" — it measures the stability of a single
    instrument, not agreement between independent raters (paper Sec 5.3,
    Appendix A.1). Never attach qualitative strength-of-agreement adjectives
    to coefficient values.

Degenerate results
    Coefficient functions raise ``ValueError`` only on *invalid arguments*
    (unknown level, non-numeric ordinal/interval ratings, incomplete kappa
    matrices). Non-computable *data* returns an :class:`IRRResult` with
    ``value=None`` and a machine-checkable ``reason_code`` from the module
    vocabulary (``REASON_*`` constants). Downstream gates branch on
    ``reason_code`` directly — ``REASON_PERFECT_AGREEMENT`` passes a gate
    (zero observed and zero expected disagreement), every other code means
    the coefficient is unavailable. There is no one-call wrapper: consumers
    call ``select_irr_metric`` plus the coefficient primitives and check
    ``reason_code`` themselves.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Callable, Iterable, Optional, Sequence

# --- Measurement levels -----------------------------------------------------

NOMINAL = "nominal"
ORDINAL = "ordinal"
INTERVAL = "interval"
LEVELS = frozenset({NOMINAL, ORDINAL, INTERVAL})

# --- Degenerate-result vocabulary (canonical, consumed downstream) ----------

#: Every included rating is identical: expected disagreement is zero, so the
#: coefficient is 0/0. Downstream gates treat this as a PASS.
REASON_PERFECT_AGREEMENT = "perfect_agreement"
#: No unit has >= 2 non-missing ratings, or fewer pairable units than the
#: structural floor of 2.
REASON_INSUFFICIENT_DATA = "insufficient_data"
#: Fewer pairable units than a caller-supplied policy floor (min_units > 2).
REASON_BELOW_FLOOR = "below_floor"
#: Any other non-computable case.
REASON_UNDEFINED = "undefined"

#: Structural floor: below 2 pairable units no coefficient is defined. Policy
#: floors (e.g. "need ~10 units before trusting alpha") are caller-supplied
#: via the ``min_units`` keyword.
DEFAULT_MIN_UNITS = 2


@dataclass
class ConfidenceInterval:
    """Bootstrap percentile confidence interval for a coefficient."""

    low: float
    high: float
    confidence: float
    method: str = "bootstrap-percentile"
    n_boot: int = 0
    n_effective: int = 0
    seed: int = 0

    def to_dict(self) -> dict[str, Any]:
        """YAML-safe dict with display rounding (3 decimals) on the bounds."""
        return {
            "low": round(self.low, 3),
            "high": round(self.high, 3),
            "confidence": self.confidence,
            "method": self.method,
            "n_boot": self.n_boot,
            "n_effective": self.n_effective,
            "seed": self.seed,
        }


@dataclass
class IRRResult:
    """The one return contract of every coefficient function in this module.

    ``value`` is ``None`` when the coefficient is not computable; then
    ``reason_code`` holds one of the module ``REASON_*`` constants and
    ``reason`` a human-prose explanation. ``level`` is set for Krippendorff's
    alpha only (``None`` for the kappas); ``n_raters`` is ``None`` when rater
    count varies per unit (alpha's coincidence formulation never tracks rater
    identity).
    """

    metric: str
    value: Optional[float]
    n_units: int
    n_ratings: int
    level: Optional[str] = None
    n_raters: Optional[int] = None
    reason_code: Optional[str] = None
    reason: Optional[str] = None
    rationale: Optional[str] = None
    ci: Optional[ConfidenceInterval] = None
    ci_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """YAML-safe dict for summary blocks.

        Display values are rounded to 3 decimals; the ``.value`` attribute
        itself keeps full precision for gating. ``None``-valued optional keys
        are omitted (``value`` stays present even when ``None``).
        """
        out: dict[str, Any] = {
            "metric": self.metric,
            "value": round(self.value, 3) if self.value is not None else None,
            "n_units": self.n_units,
            "n_ratings": self.n_ratings,
        }
        for key in ("level", "n_raters", "reason_code", "reason", "rationale"):
            val = getattr(self, key)
            if val is not None:
                out[key] = val
        if self.ci is not None:
            out["ci"] = self.ci.to_dict()
        if self.ci_reason is not None:
            out["ci_reason"] = self.ci_reason
        return out


# --- Internal helpers --------------------------------------------------------


def _floor_degenerate(
    metric: str,
    n_units: int,
    n_ratings: int,
    min_units: int,
    *,
    level: Optional[str] = None,
    n_raters: Optional[int] = None,
) -> Optional[IRRResult]:
    """Degenerate result when too few pairable units, else ``None``.

    The structural floor (2) maps to ``insufficient_data``; a caller-supplied
    policy floor above it maps to ``below_floor``.
    """
    if n_units < DEFAULT_MIN_UNITS:
        if n_units == 0:
            reason = "no unit has at least 2 non-missing ratings"
        else:
            reason = (
                f"only {n_units} pairable unit; at least "
                f"{DEFAULT_MIN_UNITS} required"
            )
        return IRRResult(
            metric=metric, value=None, n_units=n_units, n_ratings=n_ratings,
            level=level, n_raters=n_raters,
            reason_code=REASON_INSUFFICIENT_DATA, reason=reason,
        )
    if n_units < min_units:
        return IRRResult(
            metric=metric, value=None, n_units=n_units, n_ratings=n_ratings,
            level=level, n_raters=n_raters,
            reason_code=REASON_BELOW_FLOOR,
            reason=(
                f"{n_units} pairable units, below the caller-supplied "
                f"floor of {min_units}"
            ),
        )
    return None


def _validate_metric_values(values: Iterable[Any], level: str) -> None:
    """Reject bool or non-numeric ratings for ordinal/interval levels.

    Python ``bool`` is a subclass of ``int`` (the bool-is-int trap): a
    ``True`` rating would silently compute as 1.0 on a distance metric, so it
    is rejected loudly. Nominal tolerates any hashable — with the documented
    quirk that ``True`` and ``1`` hash-merge into one category.
    """
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(
                f"{level} level requires numeric ratings; got {v!r} "
                "(bool and non-numeric values are only valid at the "
                "nominal level)"
            )
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError(
                f"{level} level requires finite ratings; got {v!r}"
            )


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Percentile of pre-sorted values with linear interpolation."""
    h = (len(sorted_values) - 1) * q
    lo = math.floor(h)
    hi = math.ceil(h)
    if lo == hi:
        return float(sorted_values[lo])
    return sorted_values[lo] + (h - lo) * (sorted_values[hi] - sorted_values[lo])


# --- Coefficients ------------------------------------------------------------


def krippendorff_alpha(
    units: Iterable[Sequence[Any]],
    level: str = NOMINAL,
    *,
    min_units: int = DEFAULT_MIN_UNITS,
) -> IRRResult:
    """Krippendorff's alpha via the coincidence-matrix formulation.

    ``units`` is an iterable of per-unit rating rows; ``None`` marks a
    missing rating and ragged rows are allowed. Units with fewer than 2
    non-missing ratings are dropped (they contribute no pairable
    information). Each remaining unit with ``m`` ratings adds weight
    ``1/(m-1)`` to the coincidence matrix per unordered rating pair;
    ``alpha = 1 - D_o/D_e`` with the nominal (0/1), interval (squared
    difference), or ordinal (squared cumulative-margin distance over the
    sorted distinct observed values) delta.

    Raises ``ValueError`` on an unknown ``level``, or on bool / non-numeric
    ratings under ordinal/interval. Degenerate data never raises — see the
    module ``REASON_*`` vocabulary.
    """
    if level not in LEVELS:
        raise ValueError(
            f"unknown level {level!r}; expected one of {sorted(LEVELS)}"
        )

    rows = [[v for v in row if v is not None] for row in units]
    if level != NOMINAL:
        _validate_metric_values((v for row in rows for v in row), level)

    included = [row for row in rows if len(row) >= 2]
    n_units = len(included)
    n_ratings = sum(len(row) for row in included)

    degenerate = _floor_degenerate(
        "krippendorff_alpha", n_units, n_ratings, min_units, level=level,
    )
    if degenerate is not None:
        return degenerate

    # Coincidence matrix: each unordered pair within a unit contributes
    # 1/(m-1) to o[a][b] and to o[b][a] (identical pairs land twice on the
    # diagonal, matching the ordered-pair count).
    o: dict[Any, Counter] = defaultdict(Counter)
    for row in included:
        weight = 1.0 / (len(row) - 1)
        for a, b in combinations(row, 2):
            o[a][b] += weight
            o[b][a] += weight

    margins = {c: sum(row.values()) for c, row in o.items()}
    n = sum(margins.values())  # == n_ratings up to float error

    delta = _make_delta(level, margins)

    d_obs = 0.0
    for c, row in o.items():
        for k, weight in row.items():
            d_obs += weight * delta(c, k)
    d_obs /= n

    d_exp = 0.0
    for c, n_c in margins.items():
        for k, n_k in margins.items():
            d_exp += n_c * n_k * delta(c, k)
    d_exp /= n * (n - 1)

    if d_exp == 0.0:
        distinct = {v for row in included for v in row}
        if len(distinct) == 1:
            return IRRResult(
                metric="krippendorff_alpha", value=None,
                n_units=n_units, n_ratings=n_ratings, level=level,
                reason_code=REASON_PERFECT_AGREEMENT,
                reason=(
                    f"all {n_ratings} included ratings are identical; "
                    "expected disagreement is zero (alpha is 0/0)"
                ),
            )
        return IRRResult(
            metric="krippendorff_alpha", value=None,
            n_units=n_units, n_ratings=n_ratings, level=level,
            reason_code=REASON_UNDEFINED,
            reason="expected disagreement is zero despite differing ratings",
        )

    return IRRResult(
        metric="krippendorff_alpha", value=1.0 - d_obs / d_exp,
        n_units=n_units, n_ratings=n_ratings, level=level,
    )


def _make_delta(
    level: str, margins: dict[Any, float]
) -> Callable[[Any, Any], float]:
    """Difference function for one alpha computation.

    Ordinal distances are computed over the sorted distinct *observed*
    values, using the coincidence-matrix margins as the cumulative weights.
    """
    if level == NOMINAL:
        return lambda c, k: 0.0 if c == k else 1.0
    if level == INTERVAL:
        return lambda c, k: float(c - k) ** 2

    # Ordinal: delta = (sum_{c<=g<=k} n_g - (n_c + n_k)/2)^2 over the sorted
    # distinct observed values g.
    cats = sorted(margins)
    index = {c: i for i, c in enumerate(cats)}
    prefix = [0.0]
    for c in cats:
        prefix.append(prefix[-1] + margins[c])

    def ordinal_delta(c: Any, k: Any) -> float:
        i, j = sorted((index[c], index[k]))
        between = prefix[j + 1] - prefix[i]
        return (between - (margins[c] + margins[k]) / 2.0) ** 2

    return ordinal_delta


def fleiss_kappa(
    units: Iterable[Sequence[Any]],
    *,
    min_units: int = DEFAULT_MIN_UNITS,
) -> IRRResult:
    """Fleiss' kappa (Fleiss 1971) for complete nominal matrices.

    ``units`` must be a complete matrix: equal-length rows, at least 2
    raters, no ``None`` entries — otherwise ``ValueError`` (missing ratings
    force Krippendorff's alpha per the Figure-1 decision tree). Ratings are
    nominal categories (any hashable).
    """
    rows = [list(row) for row in units]

    for row in rows:
        if any(v is None for v in row):
            raise ValueError(
                "fleiss_kappa requires a complete matrix (no None entries); "
                "missing ratings force krippendorff_alpha per the Figure-1 "
                "decision tree"
            )
    widths = {len(row) for row in rows}
    if len(widths) > 1:
        raise ValueError(
            "fleiss_kappa requires equal-length rating rows; ragged "
            "(incomplete) matrices force krippendorff_alpha per the "
            "Figure-1 decision tree"
        )
    n_raters = widths.pop() if widths else None
    if n_raters is not None and n_raters < 2:
        raise ValueError(
            f"fleiss_kappa requires at least 2 raters per unit; got {n_raters}"
        )

    n_units = len(rows)
    n_ratings = n_units * (n_raters or 0)
    degenerate = _floor_degenerate(
        "fleiss_kappa", n_units, n_ratings, min_units, n_raters=n_raters,
    )
    if degenerate is not None:
        return degenerate
    assert n_raters is not None  # n_units >= 2 implies rows exist

    totals: Counter = Counter()
    p_i_sum = 0.0
    for row in rows:
        counts = Counter(row)
        totals.update(counts)
        p_i_sum += (sum(c * c for c in counts.values()) - n_raters) / (
            n_raters * (n_raters - 1)
        )
    p_bar = p_i_sum / n_units
    p_exp = sum((c / n_ratings) ** 2 for c in totals.values())

    if p_exp == 1.0:
        return IRRResult(
            metric="fleiss_kappa", value=None,
            n_units=n_units, n_ratings=n_ratings, n_raters=n_raters,
            reason_code=REASON_PERFECT_AGREEMENT,
            reason=(
                f"all {n_ratings} ratings fall in one category; expected "
                "agreement is 1 (kappa is 0/0)"
            ),
        )

    return IRRResult(
        metric="fleiss_kappa", value=(p_bar - p_exp) / (1.0 - p_exp),
        n_units=n_units, n_ratings=n_ratings, n_raters=n_raters,
    )


def cohen_kappa(
    ratings_a: Sequence[Any],
    ratings_b: Sequence[Any],
    *,
    min_units: int = DEFAULT_MIN_UNITS,
) -> IRRResult:
    """Cohen's kappa (unweighted, nominal) for two fixed raters.

    ``ratings_a`` and ``ratings_b`` must be equal-length and contain no
    ``None`` — callers exclude missing/null-join pairs first (or use
    ``krippendorff_alpha``, which tolerates missing data).
    """
    col_a = list(ratings_a)
    col_b = list(ratings_b)
    if len(col_a) != len(col_b):
        raise ValueError(
            f"cohen_kappa requires equal-length rating columns; got "
            f"{len(col_a)} vs {len(col_b)}"
        )
    if any(v is None for v in col_a) or any(v is None for v in col_b):
        raise ValueError(
            "cohen_kappa does not accept None ratings; exclude missing "
            "pairs first, or use krippendorff_alpha which tolerates "
            "missing data"
        )

    n_units = len(col_a)
    n_ratings = 2 * n_units
    degenerate = _floor_degenerate(
        "cohen_kappa", n_units, n_ratings, min_units, n_raters=2,
    )
    if degenerate is not None:
        return degenerate

    p_obs = sum(1 for a, b in zip(col_a, col_b) if a == b) / n_units
    freq_a = Counter(col_a)
    freq_b = Counter(col_b)
    p_exp = sum(
        (freq_a[c] / n_units) * (freq_b[c] / n_units) for c in freq_a
    )

    if p_exp == 1.0:
        # Both raters point-mass on the same category, which implies p_obs==1.
        return IRRResult(
            metric="cohen_kappa", value=None,
            n_units=n_units, n_ratings=n_ratings, n_raters=2,
            reason_code=REASON_PERFECT_AGREEMENT,
            reason=(
                f"both raters used one identical category across all "
                f"{n_units} units; expected agreement is 1 (kappa is 0/0)"
            ),
        )

    return IRRResult(
        metric="cohen_kappa", value=(p_obs - p_exp) / (1.0 - p_exp),
        n_units=n_units, n_ratings=n_ratings, n_raters=2,
    )


# --- Metric selection (paper Figure 1) ----------------------------------------


def select_irr_metric(
    n_raters: int,
    varying_identity: bool,
    complete_matrix: bool,
    scale: str,
) -> tuple[str, str]:
    """Select the structurally valid IRR coefficient (paper Figure 1).

    Returns ``(metric_name, rationale)`` where ``metric_name`` is one of
    ``"krippendorff_alpha"``, ``"cohen_kappa"``, ``"fleiss_kappa"`` and the
    rationale is a report-ready sentence (P8). Decision order: validate
    arguments; non-nominal scale -> alpha; incomplete matrix -> alpha;
    varying rater identity -> alpha; exactly 2 fixed raters -> Cohen's
    kappa; otherwise Fleiss' kappa.
    """
    if scale not in LEVELS:
        raise ValueError(
            f"unknown scale {scale!r}; expected one of {sorted(LEVELS)}"
        )
    if n_raters < 2:
        raise ValueError(
            f"inter-rater reliability requires at least 2 raters; got "
            f"{n_raters} (a single rating per unit is a measurement, not "
            "an agreement design)"
        )
    if scale != NOMINAL:
        return (
            "krippendorff_alpha",
            f"Krippendorff's alpha selected: the {scale} scale needs a "
            f"distance-weighted disagreement function (near-misses count "
            "less), which the kappa family lacks (paper Sec 5.3).",
        )
    if not complete_matrix:
        return (
            "krippendorff_alpha",
            "Krippendorff's alpha selected: the rating matrix is incomplete "
            "and alpha's coincidence-matrix formulation tolerates missing "
            "ratings, while the kappa family requires every rater to rate "
            "every unit (paper Sec 5.3).",
        )
    if varying_identity:
        return (
            "krippendorff_alpha",
            "Krippendorff's alpha selected: rater identity varies across "
            "units, and only alpha is defined for a varied-identity rater "
            "pool (paper Appendix A.1).",
        )
    if n_raters == 2:
        return (
            "cohen_kappa",
            "Cohen's kappa selected: two fixed raters on a complete nominal "
            "matrix is the only design where Cohen's kappa is structurally "
            "valid (paper Sec 5.3).",
        )
    return (
        "fleiss_kappa",
        f"Fleiss' kappa selected: {n_raters} fixed raters on a complete "
        "nominal matrix (paper Sec 5.3).",
    )


# --- Bootstrap CI --------------------------------------------------------------


def bootstrap_ci(
    units: Sequence[Any],
    statistic: Callable[[list], Optional[float]],
    *,
    n_boot: int = 1000,
    confidence: float = 0.95,
    seed: int = 0,
    min_effective_fraction: float = 0.5,
) -> Optional[ConfidenceInterval]:
    """Unit-level (cluster) bootstrap percentile CI for a coefficient.

    Resamples whole units with replacement ``n_boot`` times using
    ``random.Random(seed)`` (deterministic under a fixed seed) and applies
    ``statistic`` to each resample. ``statistic`` returns ``None`` on a
    degenerate resample (e.g. an all-identical draw); those are skipped and
    ``n_effective`` counts the rest. Returns ``None`` when fewer than 2
    units exist or ``n_effective`` falls below
    ``max(2, ceil(n_boot * min_effective_fraction))``; otherwise the
    percentile interval with linear interpolation.
    """
    rows = list(units)
    if len(rows) < 2:
        return None

    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(n_boot):
        resample = [rows[rng.randrange(len(rows))] for _ in range(len(rows))]
        value = statistic(resample)
        if value is None:
            continue
        values.append(value)

    floor = max(2, math.ceil(n_boot * min_effective_fraction))
    if len(values) < floor:
        return None

    values.sort()
    tail = (1.0 - confidence) / 2.0
    return ConfidenceInterval(
        low=_percentile(values, tail),
        high=_percentile(values, 1.0 - tail),
        confidence=confidence,
        n_boot=n_boot,
        n_effective=len(values),
        seed=seed,
    )
