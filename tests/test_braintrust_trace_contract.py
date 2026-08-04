from __future__ import annotations

import json
from typing import Any

import pytest

from autosymph.logging.braintrust import BraintrustTracer
from autosymph.runners.base import RunResult
from autosymph.runners.claude import ClaudeRunner
from autosymph.runners.codex import CodexRunner
from autosymph.runners.pi import PiRunner


class _MemorySpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.children: list[_MemorySpan] = []
        self.logs: list[dict[str, Any]] = []
        self.ended = False

    def start_span(self, name: str, **_kwargs: Any) -> _MemorySpan:
        child = _MemorySpan(name)
        self.children.append(child)
        return child

    def log(self, **kwargs: Any) -> None:
        self.logs.append(kwargs)

    def end(self) -> None:
        self.ended = True


class _MemoryLogger:
    def __init__(self) -> None:
        self.roots: list[_MemorySpan] = []

    def start_span(self, name: str, **_kwargs: Any) -> _MemorySpan:
        span = _MemorySpan(name)
        self.roots.append(span)
        return span


def _tracer() -> tuple[BraintrustTracer, _MemoryLogger]:
    logger = _MemoryLogger()
    tracer = BraintrustTracer.__new__(BraintrustTracer)
    tracer._logger = logger
    tracer._issue_spans = {}
    tracer._runs = {}
    tracer.start_run(
        issue_id="issue-1",
        identifier="ISSUE-1",
        state="implement",
        run_number=1,
        prompt="test",
        config={"model": "test"},
    )
    return tracer, logger


@pytest.mark.parametrize(
    ("runner", "lines"),
    [
        (
            ClaudeRunner(),
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tool-1",
                                    "name": "Bash",
                                    "input": {"command": "pytest"},
                                }
                            ]
                        },
                    }
                ),
                # Claude can also emit the lower-level start event. It must not
                # create a duplicate child span for the same tool id.
                json.dumps(
                    {
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_start",
                            "content_block": {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "Bash",
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool-1",
                                    "content": "Permission denied by policy",
                                    "is_error": True,
                                }
                            ]
                        },
                    }
                ),
            ],
        ),
        (
            CodexRunner(),
            [
                json.dumps(
                    {
                        "type": "exec_command.started",
                        "id": "tool-1",
                        "command": "pytest",
                    }
                ),
                json.dumps(
                    {
                        "type": "exec_command.completed",
                        "id": "tool-1",
                        "exit_code": 1,
                        "output": "Permission denied by policy",
                    }
                ),
            ],
        ),
        (
            PiRunner(),
            [
                json.dumps(
                    {
                        "type": "toolcall_start",
                        "id": "tool-1",
                        "name": "shell",
                        "input": {"command": "pytest"},
                    }
                ),
                json.dumps(
                    {
                        "type": "toolcall_end",
                        "id": "tool-1",
                        "error": "Permission denied by policy",
                    }
                ),
            ],
        ),
    ],
    ids=["claude", "codex", "pi"],
)
def test_tool_trace_contract_is_runner_neutral(runner: Any, lines: list[str]) -> None:
    tracer, logger = _tracer()

    for line in lines:
        event = runner.parse_event(line)
        assert event is not None
        tracer.on_event("issue-1", event)

    tracer.end_run("issue-1", RunResult(success=False, exit_code=1))

    run_span = logger.roots[0].children[0]
    summary = run_span.logs[-1]["metadata"]["tool_summary"]
    assert summary["total"] == 1
    assert summary["errors"] == 1
    assert summary["denials"] == 1
    assert summary["error_rate"] == 1.0
    assert summary["denial_rate"] == 1.0
    assert run_span.children[0].ended is True


def test_successful_generic_tool_result_closes_span_without_error() -> None:
    tracer, logger = _tracer()
    runner = CodexRunner()

    for payload in (
        {"type": "exec_command.started", "id": "tool-1", "command": "pytest"},
        {
            "type": "exec_command.completed",
            "id": "tool-1",
            "exit_code": 0,
            "output": "12 passed",
        },
    ):
        event = runner.parse_event(json.dumps(payload))
        assert event is not None
        tracer.on_event("issue-1", event)

    tracer.end_run("issue-1", RunResult(success=True, exit_code=0))

    run_span = logger.roots[0].children[0]
    summary = run_span.logs[-1]["metadata"]["tool_summary"]
    assert summary["total"] == 1
    assert summary["errors"] == 0
    assert summary["denials"] == 0
    assert run_span.children[0].logs[-1]["output"] == "12 passed"


def test_claude_stream_start_is_enriched_by_full_turn() -> None:
    tracer, logger = _tracer()
    runner = ClaudeRunner()
    lines = [
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Bash",
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "pytest"},
                    }
                ]
            },
        },
    ]

    for payload in lines:
        event = runner.parse_event(json.dumps(payload))
        assert event is not None
        tracer.on_event("issue-1", event)

    run_span = logger.roots[0].children[0]
    assert len(run_span.children) == 1
    assert tracer._runs["issue-1"].pending_tools["tool-1"].tool_input == {
        "command": "pytest"
    }
    assert run_span.children[0].logs[-1]["input"] == {"command": "pytest"}
