"""Codex CLI runner."""

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


class CodexRunner(AgentRunner):
    """Runner for the Codex CLI JSON stream."""

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
            "codex session start: identifier=%s state=%s model=%s prompt=%s",
            config.get("identifier", "-"),
            config.get("workflow_state", "-"),
            self._extract_arg(cmd, "-m") or "-",
            config.get("prompt_path", "-"),
        )

        start = time.monotonic()
        events: list[AgentEvent] = []
        captured_session_id: str | None = None
        token_usage: dict[str, int] = {}

        env = None
        extra_env = config.get("env_vars")
        if extra_env:
            env = {**os.environ, **extra_env}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
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
                error="'codex' CLI not found - is Codex installed?",
            )

        if proc.stdin:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()

        try:
            async for line in self._read_lines(proc.stdout):
                if on_raw_line:
                    on_raw_line(line)

                event = self.parse_event(line)
                if not event:
                    continue

                events.append(event)
                captured_session_id = captured_session_id or self._session_id_from(event.data)

                usage = event.data.get("usage", {})
                if usage:
                    token_usage.update(self._normalize_usage(usage))

                if on_event:
                    on_event(event)
        except asyncio.CancelledError:
            await self.kill(proc.pid)
            raise

        await proc.wait()
        stderr_out = await proc.stderr.read() if proc.stderr else b""
        duration = time.monotonic() - start
        exit_code = proc.returncode or 0

        error_events = [e for e in events if e.type == EventType.ERROR]
        stream_error = None
        if error_events:
            last = error_events[-1].data
            stream_error = last.get("message") or last.get("error") or last.get("reason")

        success = exit_code == 0 and not stream_error
        if not success and stderr_out:
            logger.warning("Codex stderr: %s", stderr_out.decode(errors="replace")[:500])

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
        if session_id:
            cmd = ["codex", "exec", "resume", "--json", session_id]
        else:
            cmd = [
                "codex",
                "exec",
                "--sandbox",
                str(config.get("sandbox") or "workspace-write"),
                "--json",
            ]

        model = config.get("model")
        if model:
            cmd.extend(["-m", model])
        # Prompt is supplied over stdin so it is not exposed in argv/process listings.
        cmd.append("-")
        return cmd

    @staticmethod
    def _extract_arg(cmd: list[str], flag: str) -> str | None:
        try:
            idx = cmd.index(flag)
        except ValueError:
            return None
        return cmd[idx + 1] if idx + 1 < len(cmd) else None

    def parse_event(self, line: str) -> AgentEvent | None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        now = datetime.now(timezone.utc)
        event_type = str(data.get("type") or data.get("event") or "")

        if event_type == "thread.started":
            return AgentEvent(type=EventType.SYSTEM, timestamp=now, data=data)
        if event_type == "turn.completed":
            return AgentEvent(type=EventType.COMPLETION, timestamp=now, data=data)
        if event_type == "turn.failed":
            return AgentEvent(
                type=EventType.ERROR,
                timestamp=now,
                data={**data, "message": self._error_message(data)},
            )
        if event_type == "error":
            return AgentEvent(
                type=EventType.ERROR,
                timestamp=now,
                data={**data, "message": self._error_message(data)},
            )

        item = data.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution":
            tool_id = self._tool_id(data)
            command = item.get("command", "")
            if event_type == "item.started":
                return AgentEvent(
                    type=EventType.TOOL_CALL,
                    timestamp=now,
                    data={
                        **data,
                        "tool_name": "Bash",
                        "tool_id": tool_id,
                        "input": {"command": command},
                    },
                )
            if event_type == "item.completed":
                exit_code = item.get("exit_code")
                status = str(item.get("status") or "").lower()
                is_error = status in {"failed", "error", "failure"}
                is_error = is_error or (isinstance(exit_code, int) and exit_code != 0)
                return AgentEvent(
                    type=EventType.TOOL_RESULT,
                    timestamp=now,
                    data={
                        **data,
                        "tool_id": tool_id,
                        "content": item.get("aggregated_output", ""),
                        "is_error": is_error,
                    },
                )

        if event_type in {"item.updated", "item.completed", "message.delta"}:
            text = self._extract_text(data)
            if text:
                return AgentEvent(
                    type=EventType.ASSISTANT_MESSAGE,
                    timestamp=now,
                    data={**data, "text": text},
                )

        if event_type in {
            "exec_command.started",
            "exec_command.start",
            "tool_call.started",
            "toolcall_start",
        }:
            return AgentEvent(
                type=EventType.TOOL_CALL,
                timestamp=now,
                data={**data, "tool_name": self._tool_name(data), "tool_id": self._tool_id(data)},
            )

        if event_type in {
            "exec_command.completed",
            "exec_command.finished",
            "tool_call.completed",
            "toolcall_end",
        }:
            return AgentEvent(type=EventType.TOOL_RESULT, timestamp=now, data=data)

        if data.get("usage"):
            return AgentEvent(type=EventType.TOKEN_USAGE, timestamp=now, data=data)

        return None

    @staticmethod
    def _session_id_from(data: dict[str, Any]) -> str | None:
        for key in ("session_id", "conversation_id", "thread_id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _normalize_usage(usage: dict[str, Any]) -> dict[str, int]:
        """Normalize current and legacy Codex token counters."""
        aliases = {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cached_input_tokens": "cache_read_input_tokens",
            "cache_read_input_tokens": "cache_read_input_tokens",
            "cache_write_input_tokens": "cache_creation_input_tokens",
            "cache_creation_input_tokens": "cache_creation_input_tokens",
            "reasoning_output_tokens": "reasoning_output_tokens",
        }
        normalized: dict[str, int] = {}
        for source_key, target_key in aliases.items():
            value = usage.get(source_key)
            if isinstance(value, int):
                normalized[target_key] = value
        return normalized

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        for key in ("text", "delta", "message"):
            value = data.get(key)
            if isinstance(value, str):
                return value

        item = data.get("item")
        if isinstance(item, dict):
            value = item.get("text") or item.get("delta")
            if isinstance(value, str):
                return value
            content = item.get("content")
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
                ]
                return "".join(parts)

        return ""

    @staticmethod
    def _tool_name(data: dict[str, Any]) -> str:
        for key in ("tool_name", "name", "command"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        item = data.get("item")
        if isinstance(item, dict):
            value = item.get("name") or item.get("command")
            if isinstance(value, str):
                return value
        return ""

    @staticmethod
    def _tool_id(data: dict[str, Any]) -> str:
        for key in ("tool_id", "call_id", "id"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        item = data.get("item")
        if isinstance(item, dict):
            value = item.get("id") or item.get("call_id")
            if isinstance(value, str):
                return value
        return ""

    @staticmethod
    def _error_message(data: dict[str, Any]) -> str:
        for key in ("message", "error", "reason"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return "codex run failed"

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
            logger.info("Sent SIGTERM to codex pid=%d", pid)
        except ProcessLookupError:
            pass
