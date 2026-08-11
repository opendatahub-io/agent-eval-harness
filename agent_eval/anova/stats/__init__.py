"""Statistical analysis for agent evaluations.

Optional dependency: the ``anova`` extra (scipy, statsmodels, pandas, pingouin).
Use ``missing_deps_message()`` to explain how to install it — the extra has to
land in ``.eval-venv``, which is rarely the interpreter a bare ``pip install``
would target.
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
    _IMPORT_ERROR = None
except ImportError as exc:
    ANOVA_AVAILABLE = False
    # Kept so callers reaching the ANOVA_AVAILABLE gate can still name the module
    # that was actually missing instead of guessing.
    _IMPORT_ERROR = exc


def _plugin_root():
    return Path(__file__).resolve().parents[3]


def _installer_python():
    """The interpreter the extra has to be installed into.

    Not always ``sys.executable``. On an ABI *mismatch* ``_bootstrap`` re-execs, so
    ``sys.executable`` is already the venv python — but on the common ABI-*match*
    path it only patches ``sys.path``, leaving ``sys.executable`` as the launcher.
    Naming the launcher there installs the extra outside ``.eval-venv``, where a
    later re-exec into the venv interpreter cannot see it. Resolve the venv the
    same way bootstrap does, and fall back to the running interpreter only when
    there is no venv (in-container harbor verifier, evalhub pod).
    """
    try:
        from agent_eval._bootstrap import _venv_python_and_site
        venv_python, _ = _venv_python_and_site(str(_plugin_root()))
        if venv_python:
            return venv_python
    except Exception:
        pass
    return sys.executable


def missing_deps_message(exc=None):
    """Actionable install hint naming the interpreter that actually needs the extra.

    ``pip install -e ".[anova]"`` installs into whatever environment is active,
    which is usually not where eval-anova imports from: skill scripts activate
    ``<plugin_root>/.eval-venv``, and ``ensure_deps.py`` only provisions
    pyyaml/mlflow/anthropic/jinja2 there — never the anova extra.
    """
    missing = getattr(exc or _IMPORT_ERROR, "name", None)
    target = _installer_python()
    lines = [
        "eval-anova needs the 'anova' extra: scipy, statsmodels, pandas, pingouin.",
        "",
        f"  missing:     {missing or 'one or more of them'}",
        f"  running:     {sys.executable}",
    ]
    if target != sys.executable:
        lines.append(f"  imports from: {target}   <- the extra has to go here")
    lines += [
        "",
        'A plain `pip install -e ".[anova]"` targets whichever environment is',
        "active, which is not the one the harness imports from. Use:",
        "",
        f'  {target} -m pip install -e "{_plugin_root()}[anova]"',
    ]
    return "\n".join(lines)
