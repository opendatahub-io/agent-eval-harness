"""Agent runner abstraction for eval harness."""

from .base import EvalRunner, RunResult
from .claude_code import ClaudeCodeRunner
from .openai_compatible import OpenAICompatibleRunner

RUNNERS = {
    "claude-code": ClaudeCodeRunner,
    "openai-compatible": OpenAICompatibleRunner,
}

__all__ = [
    "EvalRunner", "RunResult",
    "ClaudeCodeRunner", "OpenAICompatibleRunner",
    "RUNNERS",
]
