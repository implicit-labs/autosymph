from __future__ import annotations

import asyncio
import json

from autosymph.runners.base import EventType
from autosymph.runners.pi import PiRunner


class TestPiCommand:
    def test_new_session_command(self):
        cmd = PiRunner()._build_command("do it", {}, session_id=None)

        assert cmd == [
            "pi",
            "--mode",
            "json",
            "--print",
            "--no-session",
            "do it",
        ]

    def test_resume_command(self):
        cmd = PiRunner()._build_command("continue", {}, session_id="/tmp/pi-session")

        assert cmd == [
            "pi",
            "--mode",
            "json",
            "--print",
            "--session",
            "/tmp/pi-session",
            "continue",
        ]

    def test_model_command(self):
        cmd = PiRunner()._build_command(
            "do it", {"model": "mlx/local-model"}, session_id=None
        )

        assert cmd == [
            "pi",
            "--mode",
            "json",
            "--print",
            "--model",
            "mlx/local-model",
            "--no-session",
            "do it",
        ]

    def test_missing_binary_returns_failed_result(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            raise FileNotFoundError("pi missing")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        result = asyncio.run(
            PiRunner().run("hi", "/tmp", {"identifier": "ISSUE-1", "workflow_state": "implement"})
        )

        assert result.success is False
        assert result.exit_code == -1
        assert result.error == "'pi' CLI not found - is Pi installed?"


class TestPiParser:
    def test_current_text_delta(self):
        event = PiRunner().parse_event(
            json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "hello"},
                }
            )
        )

        assert event is not None
        assert event.type == EventType.ASSISTANT_MESSAGE
        assert event.data["text"] == "hello"

    def test_text_delta(self):
        event = PiRunner().parse_event(json.dumps({"type": "text_delta", "text": "hello"}))

        assert event is not None
        assert event.type == EventType.ASSISTANT_MESSAGE
        assert event.data["text"] == "hello"

    def test_tool_lifecycle(self):
        runner = PiRunner()

        start = runner.parse_event(
            json.dumps({"type": "toolcall_start", "id": "t1", "name": "shell"})
        )
        end = runner.parse_event(json.dumps({"type": "toolcall_end", "id": "t1"}))

        assert start is not None
        assert start.type == EventType.TOOL_CALL
        assert start.data["tool_name"] == "shell"
        assert end is not None
        assert end.type == EventType.TOOL_RESULT

    def test_current_tool_lifecycle(self):
        runner = PiRunner()

        start = runner.parse_event(
            json.dumps(
                {
                    "type": "tool_execution_start",
                    "toolCallId": "t1",
                    "toolName": "bash",
                    "args": {"command": "pytest"},
                }
            )
        )
        end = runner.parse_event(
            json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "t1",
                    "toolName": "bash",
                    "result": "Permission denied",
                    "isError": True,
                }
            )
        )

        assert start is not None
        assert start.type == EventType.TOOL_CALL
        assert start.data["tool_name"] == "bash"
        assert start.data["input"] == {"command": "pytest"}
        assert end is not None
        assert end.type == EventType.TOOL_RESULT
        assert end.data["is_error"] is True

    def test_current_provider_error(self):
        runner = PiRunner()
        message_error = runner.parse_event(
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "error",
                        "errorMessage": "Connection error.",
                    },
                }
            )
        )
        retry_error = runner.parse_event(
            json.dumps(
                {
                    "type": "auto_retry_end",
                    "success": False,
                    "attempt": 3,
                    "finalError": "Connection error.",
                }
            )
        )

        assert message_error is not None
        assert message_error.type == EventType.ERROR
        assert message_error.data["message"] == "Connection error."
        assert retry_error is not None
        assert retry_error.type == EventType.ERROR

    def test_current_token_usage(self):
        event = PiRunner().parse_event(
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {"usage": {"input": 12, "output": 3}},
                    "toolResults": [],
                }
            )
        )

        assert event is not None
        assert event.type == EventType.TOKEN_USAGE
        assert event.data["usage"] == {"input": 12, "output": 3}

    def test_done_and_error(self):
        runner = PiRunner()

        done = runner.parse_event(json.dumps({"type": "done", "session_path": "/tmp/s"}))
        error = runner.parse_event(json.dumps({"type": "error", "error": "bad auth"}))

        assert done is not None
        assert done.type == EventType.COMPLETION
        assert error is not None
        assert error.type == EventType.ERROR
        assert error.data["message"] == "bad auth"
