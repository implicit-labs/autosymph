from __future__ import annotations

import asyncio
import json

from autosymph.runners.base import EventType
from autosymph.runners.omp import OmpRunner


def test_command_contains_profile_not_secret(tmp_path):
    prompt_dir = tmp_path / ".autosymph" / "omp-prompts"
    prompt_dir.mkdir(parents=True)
    prompt = prompt_dir / "prompt.md"
    prompt.write_text("secret prompt")

    command = OmpRunner()._build_command(
        prompt,
        {
            "model": "anthropic/claude-sonnet-5",
            "profile": "claude-api",
            "permission_mode": "write",
            "max_time_seconds": 60,
        },
        None,
        tmp_path,
    )

    assert "claude-api" in command
    assert "secret prompt" not in command
    assert command[-1] == f"@{prompt.resolve()}"


def test_environment_auth_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = asyncio.run(
        OmpRunner().run(
            "do it",
            str(tmp_path),
            {"auth_mode": "environment", "auth_env": "ANTHROPIC_API_KEY"},
        )
    )

    assert result.success is False
    assert result.exit_code == -1
    assert result.error == "OMP environment auth requires ANTHROPIC_API_KEY"
    assert list((tmp_path / ".autosymph" / "omp-prompts").iterdir()) == []


def test_parser_handles_tool_and_completion_events():
    runner = OmpRunner()
    tool = runner.parse_event(
        json.dumps(
            {"type": "tool_execution_start", "toolName": "bash", "toolCallId": "call-1"}
        )
    )
    done = runner.parse_event(json.dumps({"type": "agent_end", "sessionId": "session-1"}))

    assert tool is not None and tool.type == EventType.TOOL_CALL
    assert tool.data["tool_name"] == "bash"
    assert done is not None and done.type == EventType.COMPLETION


def test_parser_treats_nested_omp_model_error_as_failure_event():
    event = OmpRunner().parse_event(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorStatus": 404,
                    "errorMessage": "model not found",
                },
            }
        )
    )

    assert event is not None and event.type == EventType.ERROR
    assert event.data["message"] == "model not found"


def test_prompt_must_be_workspace_local(tmp_path):
    outside = tmp_path.parent / "outside-prompt.md"
    outside.write_text("prompt")

    try:
        OmpRunner()._build_command(outside, {}, None, tmp_path)
    except ValueError as error:
        assert "must live under the workspace" in str(error)
    else:
        raise AssertionError("outside prompt was accepted")
