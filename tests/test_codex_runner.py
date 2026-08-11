from __future__ import annotations

import asyncio
import json

from autosymph.runners.base import EventType
from autosymph.runners.codex import CodexRunner


class TestCodexCommand:
    def test_new_session_command(self):
        runner = CodexRunner()
        cmd = runner._build_command("do it", {"model": "gpt-5.4-codex"}, session_id=None)

        assert cmd == [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--json",
            "-m",
            "gpt-5.4-codex",
            "do it",
        ]

    def test_resume_command(self):
        runner = CodexRunner()
        cmd = runner._build_command("continue", {"model": "gpt-5.4-codex"}, session_id="s-1")

        assert cmd == [
            "codex",
            "exec",
            "resume",
            "--json",
            "s-1",
            "-m",
            "gpt-5.4-codex",
            "continue",
        ]

    def test_missing_binary_returns_failed_result(self, monkeypatch):
        async def _fake_exec(*args, **kwargs):
            raise FileNotFoundError("codex missing")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        result = asyncio.run(
            CodexRunner().run("hi", "/tmp", {"identifier": "ISSUE-1", "workflow_state": "implement"})
        )

        assert result.success is False
        assert result.exit_code == -1
        assert result.error == "'codex' CLI not found - is Codex installed?"


class TestCodexParser:
    def test_current_thread_start_exposes_session_id(self):
        runner = CodexRunner()
        event = runner.parse_event(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "019fef96-c1e9-7161-90c3-fdbd4930d029",
                }
            )
        )

        assert event is not None
        assert event.type == EventType.SYSTEM
        assert runner._session_id_from(event.data) == "019fef96-c1e9-7161-90c3-fdbd4930d029"

    def test_assistant_text_from_item_updated(self):
        line = json.dumps({"type": "item.updated", "item": {"type": "message", "text": "hello"}})

        event = CodexRunner().parse_event(line)

        assert event is not None
        assert event.type == EventType.ASSISTANT_MESSAGE
        assert event.data["text"] == "hello"

    def test_tool_start_and_completion(self):
        runner = CodexRunner()

        tool_event = runner.parse_event(
            json.dumps({"type": "exec_command.started", "id": "call-1", "command": "pytest"})
        )
        done_event = runner.parse_event(
            json.dumps({"type": "turn.completed", "session_id": "s-1", "usage": {"input_tokens": 3}})
        )

        assert tool_event is not None
        assert tool_event.type == EventType.TOOL_CALL
        assert tool_event.data["tool_name"] == "pytest"
        assert tool_event.data["tool_id"] == "call-1"
        assert done_event is not None
        assert done_event.type == EventType.COMPLETION
        assert done_event.data["usage"]["input_tokens"] == 3

    def test_current_command_execution_lifecycle(self):
        runner = CodexRunner()
        start = runner.parse_event(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_1",
                        "type": "command_execution",
                        "command": "/bin/zsh -lc pwd",
                        "aggregated_output": "",
                        "exit_code": None,
                        "status": "in_progress",
                    },
                }
            )
        )
        result = runner.parse_event(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "command_execution",
                        "command": "/bin/zsh -lc pwd",
                        "aggregated_output": "/tmp/repo\n",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            )
        )

        assert start is not None
        assert start.type == EventType.TOOL_CALL
        assert start.data["tool_name"] == "Bash"
        assert start.data["tool_id"] == "item_1"
        assert start.data["input"] == {"command": "/bin/zsh -lc pwd"}
        assert result is not None
        assert result.type == EventType.TOOL_RESULT
        assert result.data["tool_id"] == "item_1"
        assert result.data["content"] == "/tmp/repo\n"
        assert result.data["is_error"] is False

    def test_current_usage_keys_are_normalized(self):
        usage = CodexRunner._normalize_usage(
            {
                "input_tokens": 120,
                "cached_input_tokens": 80,
                "cache_write_input_tokens": 5,
                "output_tokens": 14,
                "reasoning_output_tokens": 3,
            }
        )

        assert usage == {
            "input_tokens": 120,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 5,
            "output_tokens": 14,
            "reasoning_output_tokens": 3,
        }

    def test_failed_turn_is_error(self):
        event = CodexRunner().parse_event(
            json.dumps({"type": "turn.failed", "reason": "auth required"})
        )

        assert event is not None
        assert event.type == EventType.ERROR
        assert event.data["message"] == "auth required"
