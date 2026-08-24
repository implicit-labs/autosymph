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

import logging
import os
import queue
import threading
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
                type="task",  # type: ignore[arg-type]  # accepted by Braintrust at runtime
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
                tool_id = block.get("id")
                tool_name = block.get("name", "unknown")
                tool_input = block.get("input", {})

                # Build span name: "Bash: git log..." or "Skill: github-pr"
                detail = _extract_detail(tool_name, tool_input)
                span_name = f"{tool_name}: {detail}" if detail else tool_name

                span_type = "function" if tool_name == "Skill" else "tool"
                child = run.span.start_span(
                    name=span_name,
                    type=span_type,
                    input=tool_input,
                    metadata={"intent": intent} if intent else {},
                )

                # Track summary stats
                run.total_tools += 1
                run.tools_used[tool_name] = run.tools_used.get(tool_name, 0) + 1
                if tool_name == "Skill":
                    run.skills.append(tool_input.get("skill", ""))

                if tool_id:
                    run.pending_tools[tool_id] = _ToolEntry(
                        span=child, tool_name=tool_name, tool_input=tool_input,
                    )

    def _handle_result(self, run: _RunState, event: AgentEvent) -> None:
        """Match tool results to pending child spans and close them."""
        for block in event.data.get("tool_results", []):
            tool_id = block.get("tool_use_id")
            entry = run.pending_tools.pop(tool_id, None)
            if not entry:
                continue

            is_error = block.get("is_error", False)
            content = block.get("content", "")
            is_denial = False

            if is_error and isinstance(content, str):
                is_denial = any(p in content.lower() for p in _DENIAL_PATTERNS)

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


@dataclass(frozen=True)
class _ProjectionCall:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class AsyncBraintrustProjector:
    """Ordered, non-blocking, fail-soft projection to Braintrust.

    The local ledger commits first. Braintrust calls run sequentially on a
    daemon thread so provider latency and outages cannot delay or invalidate a
    factory decision.
    """

    def __init__(self, tracer: Any, *, max_queue: int = 10_000) -> None:
        self._tracer = tracer
        self._queue: queue.Queue[_ProjectionCall | None] = queue.Queue(maxsize=max_queue)
        self._failure: Exception | None = None
        self._closed = False
        self._worker = threading.Thread(
            target=self._run,
            name="autosymph-braintrust-projector",
            daemon=True,
        )
        self._worker.start()

    @property
    def failure(self) -> Exception | None:
        return self._failure

    def submit(self, method: str, *args: Any, **kwargs: Any) -> bool:
        if self._closed or self._failure is not None:
            return False
        try:
            self._queue.put_nowait(_ProjectionCall(method, args, kwargs))
            return True
        except queue.Full:
            logger.warning("Braintrust projection queue full — dropping %s", method)
            return False

    def _run(self) -> None:
        while True:
            call = self._queue.get()
            try:
                if call is None:
                    return
                if self._failure is None:
                    getattr(self._tracer, call.method)(*call.args, **call.kwargs)
            except Exception as exc:
                self._failure = exc
                logger.warning(
                    "Braintrust projection failed — remote tracing disabled",
                    exc_info=True,
                )
            finally:
                self._queue.task_done()

    def close(self, *, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # Do not block shutdown on an optional projection.
            return
        self._worker.join(timeout=timeout)


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
