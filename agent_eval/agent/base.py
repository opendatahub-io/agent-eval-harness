"""Abstract runner interface for agent evaluation.

Each runner implementation translates the generic execute() call into
a platform-specific invocation (Claude Code CLI, Agent SDK, OpenCode, etc.).
The eval harness only interacts with runners through this interface.
"""

import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class RunResult:
    """Result of a single skill invocation."""
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    token_usage: Optional[dict] = None  # {"input": N, "output": N}
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    resolved_model: Optional[str] = None  # Full model ID from runtime
    models_used: Optional[list] = None   # All distinct models observed
    per_model_usage: Optional[dict] = None  # Per-model token/cost breakdown
    per_model_turns: Optional[dict] = None  # Per-model assistant turn count
    permission_denials: Optional[list] = None  # [{tool_name, tool_use_id, tool_input}]
    raw_output: Optional[dict] = None  # Runner-specific parsed output


class EvalRunner(ABC):
    """Abstract runner -- one implementation per agent platform."""

    @classmethod
    @abstractmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        """Construct a runner from an EvalConfig.

        Each runner subclass extracts the config fields it needs.
        Overrides (e.g. resolved models, experiments) take precedence.

        Args:
            config: EvalConfig instance.
            log_prefix: Progress logging prefix (e.g. "eval", "eval:case-01").
            **overrides: Runner-specific overrides from CLI flags.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this runner (e.g. 'claude-code', 'agent-sdk')."""

    @abstractmethod
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
        """Execute a skill or prompt in an isolated workspace.

        Args:
            target: Skill name (e.g. "rfe.review") for case/batch mode,
                or None for prompt mode (direct execution without skill wrapper).
            args: Skill arguments (e.g. "RHAIRFE-1109") for case/batch mode,
                or user prompt text for prompt mode.
            workspace: Pre-staged workspace directory.
            model: Model identifier (e.g. "opus", "sonnet").
            settings_path: Path to eval-specific settings file.
            system_prompt: Optional system prompt (appended).
                Each runner translates this to its platform's API
                (e.g. --append-system-prompt for Claude Code).
            max_budget_usd: Maximum API spend for this invocation.
            timeout_s: Timeout in seconds.
            extra_env: Additional env vars to inject (e.g. from hook outputs).
                Merged after execution.env, so hook env overrides static config.

        Returns:
            RunResult with exit code, output, timing, and optional usage stats.
        """

    def run_skill(
        self,
        skill_name: str,
        args: str,
        workspace: Path,
        model: str,
        settings_path: Optional[Path] = None,
        system_prompt: Optional[str] = None,
        max_budget_usd: float = 5.0,
        timeout_s: int = 600,
    ) -> RunResult:
        """Deprecated: Use execute() instead.

        This method is provided for backward compatibility with existing code.
        It calls execute(target=skill_name, ...).
        """
        warnings.warn(
            "run_skill() is deprecated, use execute() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.execute(
            target=skill_name,
            args=args,
            workspace=workspace,
            model=model,
            settings_path=settings_path,
            system_prompt=system_prompt,
            max_budget_usd=max_budget_usd,
            timeout_s=timeout_s,
        )
