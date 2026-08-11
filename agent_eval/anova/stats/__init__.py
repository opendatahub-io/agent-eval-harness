"""Statistical analysis for agent evaluations.

Optional dependency: the ``anova`` extra (scipy, statsmodels, pandas, pingouin).
Use ``missing_deps_message()`` to explain how to install it — the extra has to
land in the interpreter the harness actually runs under, which is rarely the one
a bare ``pip install`` would target.
"""

import sys
from pathlib import Path

from agent_eval.anova.stats.pareto import pareto_frontier

try:
    from agent_eval.anova.stats.anova import (
        mixed_effects_anova,
        one_way_anova,
        repeated_measures_anova,
    )

    ANOVA_AVAILABLE = True
except ImportError:
    ANOVA_AVAILABLE = False


def missing_deps_message(exc=None):
    """Actionable install hint that names the interpreter actually missing them.

    ``pip install -e ".[anova]"`` installs into whatever environment happens to be
    active, which is usually *not* where eval-anova runs: skill scripts activate
    ``<plugin_root>/.eval-venv`` (or re-exec into it), and ``ensure_deps.py`` only
    provisions pyyaml/mlflow/anthropic/jinja2 there — never the anova extra. So
    name ``sys.executable`` explicitly instead of leaving the user to guess which
    python is short.
    """
    plugin_root = Path(__file__).resolve().parents[3]
    missing = getattr(exc, "name", None)
    return "\n".join([
        "eval-anova needs the 'anova' extra: scipy, statsmodels, pandas, pingouin.",
        "",
        f"  missing:     {missing or 'one or more of them'}",
        f"  interpreter: {sys.executable}",
        "",
        "Install into that interpreter — a plain `pip install -e \".[anova]\"` targets",
        "whichever environment is active, which is not the one running this code:",
        "",
        f'  {sys.executable} -m pip install -e "{plugin_root}[anova]"',
    ])
