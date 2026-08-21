"""Agent runner abstraction for eval harness."""

from .base import EvalRunner, RunResult
from .claude_code import ClaudeCodeRunner
from .cli_runner import CliRunner
from .codex import CodexRunner
from .null import NullRunner
from .responses_api import ResponsesAPIRunner

RUNNERS = {
    "claude-code": ClaudeCodeRunner,
    "cli": CliRunner,
    "codex": CodexRunner,
    "null": NullRunner,
    "responses-api": ResponsesAPIRunner,
}

__all__ = [
    "EvalRunner", "RunResult",
    "ClaudeCodeRunner", "CliRunner", "CodexRunner", "NullRunner",
    "ResponsesAPIRunner",
    "RUNNERS",
]
