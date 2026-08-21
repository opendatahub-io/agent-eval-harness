"""Null (do-nothing) runner — the dataset solvability probe.

Diagnostic runner for the null-agent solvability probe (/eval-dataset
Step 6.5): ``execute()`` runs NOTHING and returns an immediate empty success,
so the unchanged workspace -> execute -> collect -> score pipeline measures
what the judges award to an agent that does no work. Any case a null run
passes is non-discriminative — a degenerate case or a vacuous judge — and
``audit_dataset.py --null-run`` flags it.

CLI-only by design: invoke via ``--agent null`` on execute.py. A config
permanently pinned to ``runner.type: "null"`` is always a mistake, so config
load rejects it (see ``_parse_runner_config`` in agent_eval/config.py). Not
for real evaluations.
"""

from pathlib import Path
from typing import Optional

from .base import EvalRunner, RunResult


class NullRunner(EvalRunner):
    """Do-nothing runner: exit 0, empty output, zero cost, ~0 duration."""

    def __init__(self, log_prefix: Optional[str] = None, **_ignored):
        self._log_prefix = log_prefix

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        """Ignore every config field and every override.

        execute.py passes subagent_model / mlflow_experiment /
        mlflow_tracking_uri / effort / permissions — the probe has no knobs,
        so all are accepted and dropped.
        """
        return cls(log_prefix=log_prefix)

    @property
    def name(self) -> str:
        return "null"

    @property
    def version(self) -> str:
        return "1"

    def execute(
        self,
        target: Optional[str],
        args: str,
        workspace: Path,
        model: str,
        settings_path: Optional[Path] = None,
        system_prompt: Optional[str] = None,
        max_budget_usd: float = 5.0,
        timeout_s: int = 600,
        extra_env: Optional[dict] = None,
    ) -> RunResult:
        """Return immediately without spawning anything.

        Never reads ``settings_path`` or touches the workspace, so
        interception hooks never fire (eval-run Step 3a is skippable for the
        probe). ``model`` is required by execute.py but ignored here —
        ``resolved_model`` reports the probe, not the ignored flag.
        """
        return RunResult(
            exit_code=0,
            stdout="",
            stderr="",
            duration_s=0.0,
            token_usage=None,
            cost_usd=0.0,
            num_turns=0,
            resolved_model="null",
        )
