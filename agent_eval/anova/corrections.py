"""Multiple-comparison correction names shared by config parsing and stats.

Lives outside ``stats/`` so ``matrix.py`` can validate
``matrix.analysis.correction`` at config-parse time without importing the
optional anova extra (scipy/statsmodels/pandas/pingouin).
"""

from __future__ import annotations

DEFAULT_CORRECTION = "holm"

# Canonical name per accepted spelling. "fdr_bh" is the statsmodels method
# name; the canonical output name stays the short "bh".
_ALIASES = {
    "holm": "holm",
    "bh": "bh",
    "fdr_bh": "bh",
    "none": "none",
}


def normalize_correction(value: object) -> str:
    """Canonical correction name (``holm`` | ``bh`` | ``none``).

    Raises ValueError on anything else so a typo fails at config load, not
    after the (expensive) runs have already executed.
    """
    try:
        return _ALIASES[str(value).strip().lower()]
    except KeyError:
        raise ValueError(
            f"Unknown multiple-comparison correction {value!r}: expected one of "
            f"{', '.join(sorted(_ALIASES))}."
        ) from None
