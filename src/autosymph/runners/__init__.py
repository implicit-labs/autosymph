"""Agent runners — subprocess-based agent execution."""

from autosymph.runners.base import AgentEvent, AgentRunner, RunResult
from autosymph.runners.claude import ClaudeRunner
from autosymph.runners.codex import CodexRunner
from autosymph.runners.omp import OmpRunner
from autosymph.runners.pi import PiRunner

RunnerRegistry = dict[str, AgentRunner]


def default_runner_registry() -> RunnerRegistry:
    return {
        "claude": ClaudeRunner(),
        "codex": CodexRunner(),
        "omp": OmpRunner(),
        "pi": PiRunner(),
    }


__all__ = [
    "AgentEvent",
    "AgentRunner",
    "ClaudeRunner",
    "CodexRunner",
    "OmpRunner",
    "PiRunner",
    "RunResult",
    "RunnerRegistry",
    "default_runner_registry",
]
