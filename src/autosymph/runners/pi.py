"""Pi CLI runner."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Any, Callable

from autosymph.runners.base import AgentEvent, AgentRunner, EventType, RunResult

logger = logging.getLogger(__name__)


class PiRunner(AgentRunner):
    """Runner for the Pi CLI JSON stream."""

    async def run(
        self,
        prompt: str,
        workspace_path: str,
        config: dict[str, Any],
        session_id: str | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        on_raw_line: Callable[[str], None] | None = None,
    ) -> RunResult:
        cmd = self._build_command(prompt, config, session_id)
        logger.info(
            "pi session start: identifier=%s state=%s prompt=%s",
            config.get("identifier", "-"),
            config.get("workflow_state", "-"),
            config.get("prompt_path", "-"),
        )

        start = time.monotonic()
        events: list[AgentEvent] = []
        captured_session_id: str | None = session_id
        token_usage: dict[str, int] = {}

        env = None
        extra_env = config.get("env_vars")
        if extra_env:
            env = {**os.environ, **extra_env}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_path,
                env=env,
                limit=10 * 1024 * 1024,
            )
        except FileNotFoundError:
            return RunResult(
                success=False,
                exit_code=-1,
                error="'pi' CLI not found - is Pi installed?",
            )

        try:
            async for line in self._read_lines(proc.stdout):
                if on_raw_line:
                    on_raw_line(line)

                event = self.parse_event(line)
                if not event:
                    continue

                events.append(event)
                if not captured_session_id:
                    session_path = event.data.get("session_path") or event.data.get("session")
                    if isinstance(session_path, str) and session_path:
                        captured_session_id = session_path

                usage = event.data.get("usage", {})
                if usage:
                    for source_key, target_key in (
                        ("input_tokens", "input_tokens"),
                        ("output_tokens", "output_tokens"),
                        ("input", "input_tokens"),
                        ("output", "output_tokens"),
                    ):
                        val = usage.get(source_key)
                        if isinstance(val, int):
                            token_usage[target_key] = val

                if on_event:
                    on_event(event)
        except asyncio.CancelledError:
            await self.kill(proc.pid)
            raise

        await proc.wait()
        stderr_out = await proc.stderr.read() if proc.stderr else b""
        duration = time.monotonic() - start
        exit_code = proc.returncode or 0

        stream_error = self._stream_error(events)

        success = exit_code == 0 and not stream_error
        if not success and stderr_out:
            logger.warning("Pi stderr: %s", stderr_out.decode(errors="replace")[:500])

        return RunResult(
            success=success,
            exit_code=exit_code,
            session_id=captured_session_id,
            events=events,
            duration_seconds=duration,
            token_usage=token_usage,
            error=stream_error
            or (stderr_out.decode(errors="replace")[:500] if not success and stderr_out else None),
        )

    def _build_command(
        self,
        prompt: str,
        config: dict[str, Any],
        session_id: str | None,
    ) -> list[str]:
        cmd = ["pi", "--mode", "json", "--print"]
        model = config.get("model")
        if model:
            cmd.extend(["--model", str(model)])
        if session_id:
            cmd.extend(["--session", session_id, prompt])
            return cmd
        cmd.extend(["--no-session", prompt])
        return cmd

    def parse_event(self, line: str) -> AgentEvent | None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        now = datetime.now(timezone.utc)
        event_type = str(data.get("type") or data.get("event") or "")

        if event_type == "session":
            return AgentEvent(
                type=EventType.SYSTEM,
                timestamp=now,
                data={**data, "session_path": data.get("id", "")},
            )

        # Pi v0.50+ wraps streaming provider events in message_update.
        if event_type == "message_update":
            update = data.get("assistantMessageEvent") or {}
            if update.get("type") == "text_delta":
                return AgentEvent(
                    type=EventType.ASSISTANT_MESSAGE,
                    timestamp=now,
                    data={**data, "text": update.get("delta", "")},
                )

        if event_type == "message_end":
            message = data.get("message") or {}
            if message.get("role") == "assistant" and message.get("stopReason") == "error":
                error = message.get("errorMessage") or "Pi provider error"
                return AgentEvent(
                    type=EventType.ERROR,
                    timestamp=now,
                    data={**data, "message": error, "usage": message.get("usage", {})},
                )

        if event_type == "turn_end":
            message = data.get("message") or {}
            usage = message.get("usage") or {}
            if usage:
                return AgentEvent(
                    type=EventType.TOKEN_USAGE,
                    timestamp=now,
                    data={**data, "usage": usage},
                )

        if event_type == "tool_execution_start":
            return AgentEvent(
                type=EventType.TOOL_CALL,
                timestamp=now,
                data={
                    **data,
                    "tool_name": data.get("toolName", ""),
                    "tool_id": data.get("toolCallId", ""),
                    "input": data.get("args", {}),
                },
            )

        if event_type == "tool_execution_end":
            return AgentEvent(
                type=EventType.TOOL_RESULT,
                timestamp=now,
                data={
                    **data,
                    "tool_id": data.get("toolCallId", ""),
                    "content": data.get("result"),
                    "is_error": bool(data.get("isError")),
                },
            )

        if event_type == "auto_retry_end":
            if data.get("success", False):
                return AgentEvent(type=EventType.COMPLETION, timestamp=now, data=data)
            return AgentEvent(
                type=EventType.ERROR,
                timestamp=now,
                data={**data, "message": data.get("finalError") or "Pi retries exhausted"},
            )

        if event_type == "text_delta":
            return AgentEvent(
                type=EventType.ASSISTANT_MESSAGE,
                timestamp=now,
                data={**data, "text": data.get("text", "")},
            )
        if event_type == "toolcall_start":
            return AgentEvent(
                type=EventType.TOOL_CALL,
                timestamp=now,
                data={
                    **data,
                    "tool_name": data.get("name") or data.get("tool_name", ""),
                    "tool_id": data.get("id") or data.get("tool_id", ""),
                },
            )
        if event_type == "toolcall_end":
            return AgentEvent(type=EventType.TOOL_RESULT, timestamp=now, data=data)
        if event_type == "done":
            return AgentEvent(type=EventType.COMPLETION, timestamp=now, data=data)
        if event_type == "error":
            message = data.get("message") or data.get("error") or "pi run failed"
            return AgentEvent(
                type=EventType.ERROR,
                timestamp=now,
                data={**data, "message": message},
            )
        if data.get("usage"):
            return AgentEvent(type=EventType.TOKEN_USAGE, timestamp=now, data=data)
        return None

    @staticmethod
    def _stream_error(events: list[AgentEvent]) -> str | None:
        """Return the final terminal stream error, allowing retries to recover."""
        stream_error: str | None = None
        for event in events:
            if event.type == EventType.ERROR:
                value = event.data.get("message") or event.data.get("error")
                stream_error = str(value) if value else "Pi run failed"
            elif event.type == EventType.COMPLETION:
                stream_error = None
        return stream_error

    async def _read_lines(self, stream: asyncio.StreamReader | None):
        if not stream:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            yield line.decode(errors="replace").rstrip("\n")

    async def kill(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("Sent SIGTERM to pi pid=%d", pid)
        except ProcessLookupError:
            pass
