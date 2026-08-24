"""Oh My Pi (OMP) CLI runner.

OMP subscription and OMP+provider API usage are named profiles of this one
adapter. Prompts are passed by a private workspace-local file reference so raw
prompt content and API keys never appear in argv or receipts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import time
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable
from uuid import uuid4

from autosymph.runners.base import AgentEvent, AgentRunner, EventType, RunResult

logger = logging.getLogger(__name__)


class OmpRunner(AgentRunner):
    """Runner for OMP's non-interactive JSON event stream."""

    async def run(
        self,
        prompt: str,
        workspace_path: str,
        config: dict[str, Any],
        session_id: str | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        on_raw_line: Callable[[str], None] | None = None,
    ) -> RunResult:
        workspace = Path(workspace_path).resolve()
        prompt_dir = workspace / ".autosymph" / "omp-prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f"{uuid4().hex}.md"
        prompt_path.write_text(prompt)
        prompt_path.chmod(0o600)

        auth_env = config.get("auth_env")
        if config.get("auth_mode") == "environment" and (
            not auth_env or not os.environ.get(str(auth_env))
        ):
            prompt_path.unlink(missing_ok=True)
            return RunResult(
                success=False,
                exit_code=-1,
                error=f"OMP environment auth requires {auth_env or 'a configured auth_env'}",
            )

        cmd = self._build_command(prompt_path, config, session_id, workspace)
        logger.info(
            "omp session start: identifier=%s state=%s model=%s profile=%s auth=%s prompt_sha256=%s",
            config.get("identifier", "-"),
            config.get("workflow_state", "-"),
            config.get("model") or "-",
            config.get("profile") or "-",
            config.get("auth_mode") or "-",
            sha256(prompt.encode()).hexdigest(),
        )

        start = time.monotonic()
        events: list[AgentEvent] = []
        captured_session_id: str | None = session_id
        token_usage: dict[str, int] = {}
        env = {**os.environ, **config.get("env_vars", {})}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
                env=env,
                limit=10 * 1024 * 1024,
                start_new_session=True,
            )
        except FileNotFoundError:
            prompt_path.unlink(missing_ok=True)
            return RunResult(
                success=False,
                exit_code=-1,
                error="'omp' CLI not found - is Oh My Pi installed?",
            )

        try:
            async for line in self._read_lines(proc.stdout):
                if on_raw_line:
                    on_raw_line(line)
                event = self.parse_event(line)
                if event is None:
                    continue
                events.append(event)
                captured_session_id = captured_session_id or self._session_id_from(event.data)
                usage = self._usage_from(event.data)
                for key, value in usage.items():
                    if isinstance(value, int):
                        token_usage[key] = value
                if on_event:
                    on_event(event)
        except asyncio.CancelledError:
            await self.kill(proc.pid)
            raise
        finally:
            prompt_path.unlink(missing_ok=True)

        await proc.wait()
        stderr_out = await proc.stderr.read() if proc.stderr else b""
        duration = time.monotonic() - start
        exit_code = proc.returncode or 0
        error_events = [event for event in events if event.type == EventType.ERROR]
        stream_error = None
        if error_events:
            last = error_events[-1].data
            stream_error = self._error_message(last)
        success = exit_code == 0 and not stream_error
        if not success and stderr_out:
            logger.warning("OMP stderr: %s", stderr_out.decode(errors="replace")[:500])
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
        prompt_path: Path,
        config: dict[str, Any],
        session_id: str | None,
        workspace: Path,
    ) -> list[str]:
        if prompt_path.resolve().parent.parent.parent != workspace.resolve():
            raise ValueError("OMP prompt file must live under the workspace .autosymph directory")
        cmd = [
            "omp",
            "--print",
            "--mode",
            "json",
            "--cwd",
            str(workspace),
            "--approval-mode",
            str(config.get("permission_mode") or "write"),
        ]
        model = config.get("model")
        if model:
            cmd.extend(["--model", str(model)])
        profile = config.get("profile")
        if profile:
            cmd.extend(["--profile", str(profile)])
        max_time = config.get("max_time_seconds")
        if max_time:
            cmd.extend(["--max-time", str(max_time)])
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(f"@{prompt_path.resolve()}")
        return cmd

    def parse_event(self, line: str) -> AgentEvent | None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        now = datetime.now(timezone.utc)
        event_type = str(data.get("type") or data.get("event") or "")

        if event_type in {"error", "agent_error", "auto_retry_end"}:
            if event_type != "auto_retry_end" or data.get("success") is False or data.get("error"):
                return AgentEvent(
                    type=EventType.ERROR,
                    timestamp=now,
                    data={**data, "message": self._error_message(data)},
                )
        if event_type in {"tool_execution_start", "toolcall_start", "tool_call_start"}:
            return AgentEvent(
                type=EventType.TOOL_CALL,
                timestamp=now,
                data={
                    **data,
                    "tool_name": data.get("toolName") or data.get("name") or data.get("tool_name", ""),
                    "tool_id": data.get("toolCallId") or data.get("id") or data.get("tool_id", ""),
                },
            )
        if event_type in {"tool_execution_end", "toolcall_end", "tool_call_end"}:
            return AgentEvent(type=EventType.TOOL_RESULT, timestamp=now, data=data)
        if event_type in {"agent_end", "done", "session_end"}:
            return AgentEvent(type=EventType.COMPLETION, timestamp=now, data=data)
        if event_type in {"message_update", "text_delta", "assistant_message"}:
            text = self._text_from(data)
            if text:
                return AgentEvent(
                    type=EventType.ASSISTANT_MESSAGE,
                    timestamp=now,
                    data={**data, "text": text},
                )
        if event_type == "message_end":
            return AgentEvent(type=EventType.ASSISTANT_TURN, timestamp=now, data=data)
        if self._usage_from(data):
            return AgentEvent(type=EventType.TOKEN_USAGE, timestamp=now, data=data)
        if event_type in {"session", "agent_start"}:
            return AgentEvent(type=EventType.SYSTEM, timestamp=now, data=data)
        return None

    @staticmethod
    def _text_from(data: dict[str, Any]) -> str:
        for key in ("text", "delta"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        nested = data.get("assistantMessageEvent") or data.get("message")
        if isinstance(nested, dict):
            for key in ("text", "delta"):
                value = nested.get(key)
                if isinstance(value, str):
                    return value
        return ""

    @staticmethod
    def _usage_from(data: dict[str, Any]) -> dict[str, int]:
        usage = data.get("usage")
        if isinstance(usage, dict):
            return usage
        message = data.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            return message["usage"]
        return {}

    @staticmethod
    def _session_id_from(data: dict[str, Any]) -> str | None:
        for key in ("session_id", "sessionId", "sessionPath", "session_path"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _error_message(data: dict[str, Any]) -> str:
        for key in ("message", "error", "reason"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return "OMP run failed"

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
            os.killpg(pid, signal.SIGTERM)
            logger.info("Sent SIGTERM to OMP process group pid=%d", pid)
        except ProcessLookupError:
            pass
