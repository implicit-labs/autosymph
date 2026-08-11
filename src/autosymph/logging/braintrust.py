"""Braintrust tracing — run span with child tool spans for timeline visualization.

Opt-in and BYOK: only activates when BRAINTRUST_API_KEY is set and braintrust
is installed. Use your own Braintrust project/API key to run tracing and eval
loops; autosymph does not provide or proxy Braintrust credentials.
Install: pip install 'autosymph[tracing]'

Trace structure:
    ISSUE-123-implement-run1            (run span — scores, metrics, summary)
      ├── Bash: git log --oneline -5    (tool span — input, output, intent)
      ├── Skill: github-pr              (tool span — input, output, intent)
      └── Bash: gh pr create            (tool span — input, output, intent)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from autosymph.runners.base import AgentEvent, EventType, RunResult

logger = logging.getLogger(__name__)

_MAX_INTENT_CHARS = 300
_MAX_OUTPUT_CHARS = 8000
_DENIAL_PATTERNS = ("requires approval", "permission", "not allowed", "denied", "command_substitution")

try:
    import braintrust

    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    logger.warning(
        "braintrust package not installed — tracing disabled "
        "(pip install 'autosymph[tracing]')"
    )


def is_enabled(api_key_ref: str = "$BRAINTRUST_API_KEY") -> bool:
    """Check if Braintrust tracing is available and configured."""
    if not _AVAILABLE:
        return False
    key = _resolve_env(api_key_ref)
    if not key:
        env_name = api_key_ref[1:] if api_key_ref.startswith("$") else api_key_ref
        logger.warning("%s not set — Braintrust tracing disabled", env_name)
        return False
    return True


def _resolve_env(value: str) -> str:
    """Resolve $ENV_VAR references to their values."""
    if value.startswith("$"):
        return os.environ.get(value[1:], "")
    return value


@dataclass
class _ToolEntry:
    """Pending tool call waiting for its result."""

    span: Any  # braintrust child Span
    tool_name: str
    tool_input: dict


@dataclass
class _RunState:
    """Accumulator for a single agent run's trace data."""

    span: Any  # braintrust run Span
    pending_intent: list[str] = field(default_factory=list)
    pending_tools: dict[str, _ToolEntry] = field(default_factory=dict)  # tool_use_id → entry
    # Summary counters
    total_tools: int = 0
    errors: int = 0
    denials: int = 0
    tools_used: dict[str, int] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)


class BraintrustTracer:
    """Logs Braintrust spans with child tool spans for timeline visualization.

    Each tool call becomes a child span under the run span, so the Braintrust
    timeline shows the full sequence of actions with input/output/intent.
    """

    def __init__(self, project: str = "autosymph", api_key: str = "$BRAINTRUST_API_KEY") -> None:
        if not _AVAILABLE:
            raise RuntimeError("braintrust package not installed — pip install 'autosymph[tracing]'")
        resolved_key = _resolve_env(api_key)
        self._logger = braintrust.init_logger(project=project, api_key=resolved_key)
        self._issue_spans: dict[str, Any] = {}  # issue_id → parent span
        self._runs: dict[str, _RunState] = {}
        logger.info("Braintrust tracer initialized (project=%s)", project)

    def start_run(
        self,
        issue_id: str,
        identifier: str,
        state: str,
        run_number: int,
        prompt: str,
        config: dict[str, Any],
    ) -> None:
        """Open a run span under the issue's parent span."""
        # Create or reuse a parent span per issue
        if issue_id not in self._issue_spans:
            self._issue_spans[issue_id] = self._logger.start_span(
                name=identifier,
                type=braintrust.SpanTypeAttribute.TASK,
                metadata={"issue_id": issue_id, "identifier": identifier},
            )
            logger.debug("[bt] started issue span: %s", identifier)

        parent = self._issue_spans[issue_id]
        name = f"{state}-run{run_number}"
        span = parent.start_span(
            name=name,
            type="task",
            input=prompt,
            metadata={
                "issue_id": issue_id,
                "identifier": identifier,
                "state": state,
                "run_number": run_number,
                "model": config.get("model"),
                "permission_mode": config.get("permission_mode"),
                "max_turns": config.get("max_turns"),
            },
        )
        self._runs[issue_id] = _RunState(span=span)
        logger.debug("[bt] started run span: %s/%s", identifier, name)

    def on_event(self, issue_id: str, event: AgentEvent) -> None:
        """Process an agent event — create/close tool child spans."""
        run = self._runs.get(issue_id)
        if not run:
            return

        if event.type == EventType.ASSISTANT_TURN:
            self._handle_turn(run, event)
        elif event.type == EventType.ASSISTANT_MESSAGE:
            text = event.data.get("text")
            if isinstance(text, str) and text.strip():
                run.pending_intent.append(text.strip())
        elif event.type == EventType.TOOL_CALL:
            self._handle_tool_call(run, event)
        elif event.type == EventType.TOOL_RESULT:
            self._handle_result(run, event)

    def end_run(self, issue_id: str, result: RunResult) -> None:
        """Close the run span with summary metrics and scores."""
        run = self._runs.pop(issue_id, None)
        if not run:
            return

        # Close any orphaned tool spans
        for entry in run.pending_tools.values():
            entry.span.log(output="[no result — span closed at run end]")
            entry.span.end()

        total = run.total_tools or 1  # avoid division by zero
        summary = {
            "total": run.total_tools,
            "errors": run.errors,
            "denials": run.denials,
            "error_rate": round(run.errors / total, 3),
            "denial_rate": round(run.denials / total, 3),
            "tools_used": run.tools_used,
            "skills": run.skills,
        }

        run.span.log(
            output={"success": result.success, "error": result.error},
            scores={
                "success": 1.0 if result.success else 0.0,
                "denial_rate": summary["denial_rate"],
                "error_rate": summary["error_rate"],
            },
            metrics={
                "duration_seconds": result.duration_seconds,
                "input_tokens": result.token_usage.get("input_tokens", 0),
                "output_tokens": result.token_usage.get("output_tokens", 0),
                "cache_read_tokens": result.token_usage.get("cache_read_input_tokens", 0),
                "tool_calls": run.total_tools,
            },
            metadata={
                "session_id": result.session_id,
                "tool_summary": summary,
            },
        )
        run.span.end()
        logger.debug(
            "[bt] ended run for %s (success=%s, tools=%d, errors=%d, denials=%d)",
            issue_id, result.success, run.total_tools, run.errors, run.denials,
        )

    def end_issue(self, issue_id: str, success: bool = True) -> None:
        """Close the parent issue span when the issue reaches a terminal state."""
        parent = self._issue_spans.pop(issue_id, None)
        if not parent:
            return
        parent.log(
            output={"completed": success},
            scores={"success": 1.0 if success else 0.0},
        )
        parent.end()
        logger.debug("[bt] closed issue span for %s (success=%s)", issue_id, success)

    # -- Internal --

    def _handle_turn(self, run: _RunState, event: AgentEvent) -> None:
        """Process an assistant turn — buffer intent, open child spans for tool calls."""
        thinking = event.data.get("thinking", [])
        texts = event.data.get("texts", [])
        tool_uses = event.data.get("tool_uses", [])

        for t in thinking:
            if t:
                run.pending_intent.append(t.strip())
        for t in texts:
            if t:
                run.pending_intent.append(t.strip())

        if tool_uses:
            intent = " | ".join(run.pending_intent) if run.pending_intent else None
            if intent and len(intent) > _MAX_INTENT_CHARS:
                intent = intent[:_MAX_INTENT_CHARS] + "..."
            run.pending_intent.clear()

            for block in tool_uses:
                self._open_tool(
                    run,
                    tool_id=block.get("id"),
                    tool_name=block.get("name", "unknown"),
                    tool_input=block.get("input", {}),
                    intent=intent,
                )

    def _handle_tool_call(self, run: _RunState, event: AgentEvent) -> None:
        """Open a child span for normalized TOOL_CALL events.

        Claude emits both full assistant turns and lower-level stream events, so
        ``_open_tool`` de-duplicates by tool id. Codex and Pi only expose this
        normalized lifecycle, which is why handling TOOL_CALL here is required
        for runner-neutral traces.
        """
        data = event.data
        tool_id = _first_string(data, "tool_id", "call_id", "id")
        item = data.get("item")
        if not tool_id and isinstance(item, dict):
            tool_id = _first_string(item, "tool_id", "call_id", "id")

        tool_name = _first_string(data, "tool_name", "name", "command") or "unknown"
        tool_input = _extract_tool_input(data)
        intent = " | ".join(run.pending_intent) if run.pending_intent else None
        if intent and len(intent) > _MAX_INTENT_CHARS:
            intent = intent[:_MAX_INTENT_CHARS] + "..."
        run.pending_intent.clear()
        self._open_tool(run, tool_id, tool_name, tool_input, intent)

    def _open_tool(
        self,
        run: _RunState,
        tool_id: str | None,
        tool_name: str,
        tool_input: dict[str, Any],
        intent: str | None,
    ) -> None:
        """Open one tool span, de-duplicating mixed high/low-level events."""
        if tool_id and tool_id in run.pending_tools:
            existing = run.pending_tools[tool_id]
            if tool_input and not existing.tool_input:
                existing.tool_input = tool_input
                existing.span.log(input=tool_input)
            return

        detail = _extract_detail(tool_name, tool_input)
        span_name = f"{tool_name}: {detail}" if detail else tool_name
        span_type = (
            braintrust.SpanTypeAttribute.FUNCTION
            if tool_name == "Skill"
            else braintrust.SpanTypeAttribute.TOOL
        )
        child = run.span.start_span(
            name=span_name,
            type=span_type,
            input=tool_input,
            metadata={"intent": intent} if intent else {},
        )

        run.total_tools += 1
        run.tools_used[tool_name] = run.tools_used.get(tool_name, 0) + 1
        if tool_name == "Skill":
            run.skills.append(str(tool_input.get("skill", "")))

        if tool_id:
            run.pending_tools[tool_id] = _ToolEntry(
                span=child,
                tool_name=tool_name,
                tool_input=tool_input,
            )
        else:
            # A span without a correlation id cannot receive a later result.
            child.log(output="[tool call has no correlation id]")
            child.end()

    def _handle_result(self, run: _RunState, event: AgentEvent) -> None:
        """Match tool results to pending child spans and close them."""
        for block in _extract_tool_results(event.data):
            tool_id = block.get("tool_use_id")
            if not isinstance(tool_id, str):
                continue
            entry = run.pending_tools.pop(tool_id, None)
            if not entry:
                continue

            is_error = block.get("is_error", False)
            content = block.get("content", "")
            is_denial = False

            searchable_content = content
            if not isinstance(searchable_content, str):
                try:
                    searchable_content = json.dumps(searchable_content, sort_keys=True)
                except (TypeError, ValueError):
                    searchable_content = str(searchable_content)
            if is_error:
                is_denial = any(p in searchable_content.lower() for p in _DENIAL_PATTERNS)

            if isinstance(content, str) and len(content) > _MAX_OUTPUT_CHARS:
                content = content[:_MAX_OUTPUT_CHARS] + f"...[truncated {len(content) - _MAX_OUTPUT_CHARS} chars]"

            entry.span.log(
                output=content,
                metadata={"is_error": is_error, "is_denial": is_denial},
            )
            if is_error:
                run.errors += 1
            if is_denial:
                run.denials += 1
                entry.span.log(scores={"denied": 1.0})

            entry.span.end()


def _first_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_tool_input(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("tool_input", "input", "arguments", "args"):
        value = data.get(key)
        if isinstance(value, dict):
            return value

    item = data.get("item")
    if isinstance(item, dict):
        nested = _extract_tool_input(item)
        if nested:
            return nested

    command = data.get("command")
    if isinstance(command, str):
        return {"command": command}
    return {}


def _extract_tool_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize Claude, Codex, and Pi result payloads."""
    blocks = data.get("tool_results")
    if isinstance(blocks, list):
        return [block for block in blocks if isinstance(block, dict)]

    raw_item = data.get("item")
    item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
    tool_id = (
        _first_string(data, "tool_use_id", "tool_id", "call_id", "id")
        or _first_string(item, "tool_use_id", "tool_id", "call_id", "id")
    )
    if not tool_id:
        return []

    content: Any = ""
    for source in (data, item):
        for key in ("content", "output", "result", "aggregated_output", "message"):
            value = source.get(key)
            if value not in (None, ""):
                content = value
                break
        if content not in (None, ""):
            break

    status = str(data.get("status") or item.get("status") or "").lower()
    exit_code = data.get("exit_code", item.get("exit_code"))
    is_error = bool(data.get("is_error") or item.get("is_error"))
    is_error = is_error or status in {"error", "failed", "failure"}
    is_error = is_error or (isinstance(exit_code, int) and exit_code != 0)
    is_error = is_error or bool(data.get("error") or item.get("error"))
    if content in (None, "") and (data.get("error") or item.get("error")):
        content = data.get("error") or item.get("error")

    return [
        {
            "tool_use_id": tool_id,
            "content": content,
            "is_error": is_error,
        }
    ]


def _extract_detail(tool_name: str, tool_input: dict) -> str:
    """Extract a short label for the span name."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:60] + ("..." if len(cmd) > 60 else "")
    if tool_name in ("Read", "Write", "Edit"):
        path = tool_input.get("file_path", "")
        # Show just the filename, not the full path
        return path.rsplit("/", 1)[-1] if "/" in path else path
    if tool_name == "Skill":
        return tool_input.get("skill", "")
    if tool_name == "Agent":
        return tool_input.get("description", "") or tool_input.get("subagent_type", "")
    if tool_name in ("Grep", "Glob"):
        return tool_input.get("pattern", "")
    if tool_name.startswith("mcp__"):
        # e.g. mcp__linear__save_issue → linear.save_issue
        parts = tool_name.split("__")
        return ".".join(parts[1:]) if len(parts) > 1 else tool_name
    return ""
