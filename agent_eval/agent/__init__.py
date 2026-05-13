"""Agent runner abstraction for eval harness."""

from .base import EvalRunner, RunResult
from .claude_code import ClaudeCodeRunner
from .cli_runner import CliRunner

RUNNERS = {
    "claude-code": ClaudeCodeRunner,
    "cli": CliRunner,
}

__all__ = ["EvalRunner", "RunResult", "ClaudeCodeRunner", "CliRunner", "RUNNERS"]
