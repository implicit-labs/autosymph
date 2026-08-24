"""Core orchestration loop — poll Linear, dispatch agents, reconcile state.

Hot-reloads workflow.yaml on every poll tick. Prompts read at dispatch time (not cached).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autosymph.concurrency import ConcurrencyManager
from autosymph.config import (
    RUNNER_NAME_RE,
    StateConfig,
    WorkflowConfig,
    load_config,
    resolve_activity_summary,
    validate_config,
    ConfigError,
)
from autosymph.linear_client import LinearClient, LinearIssue
from autosymph.ledger import Ledger, LedgerError, RunAllocation, SQLiteLedger
from autosymph.logging.braintrust import (
    AsyncBraintrustProjector,
    BraintrustTracer,
    is_enabled as bt_enabled,
)
from autosymph.logging.events import EventLog
from autosymph.logging.stream import LogStream
from autosymph.logging.summarizer import ActivitySummarizer
from autosymph.logging.timeline import TimelineExtractor
from autosymph.resources import AcquiredResources, ResourcePool
from autosymph.runners.base import AgentRunner, EventType, RunResult
from autosymph.runners import RunnerRegistry, default_runner_registry
from autosymph.state_machine import ClaimState, Signal, StateMachine
from autosymph.workspace import Workspace, WorkspaceManager

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return UTC ISO timestamp in Linear-compatible Z form."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse Linear/local ISO timestamps for ordering and stale-comment filters."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Orchestrator:
    """Main poll-dispatch-reconcile loop.

    Lifecycle:
    1. Poll Linear for issues in actionable statuses
    2. Reconcile: detect stalls, kill timed-out agents, handle terminal tickets
    3. Dispatch: select eligible issues, respect concurrency limits, spawn agents
    4. Sleep until next tick
    5. Hot-reload workflow.yaml before each tick
    """

    def __init__(
        self,
        config: WorkflowConfig,
        config_path: Path,
        linear: LinearClient,
        state_machine: StateMachine,
        workspace_mgr: WorkspaceManager,
        runner: AgentRunner | None = None,
        runner_registry: RunnerRegistry | None = None,
        shutdown_event: asyncio.Event | None = None,
        resource_pool: ResourcePool | None = None,
        concurrency_mgr: ConcurrencyManager | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.linear = linear
        self.sm = state_machine
        self.workspace_mgr = workspace_mgr
        self.runners = runner_registry or default_runner_registry()
        if runner is not None:
            self.runners["claude"] = runner
        self.runner = self.runners["claude"]  # backward-compatible test hook
        self.resource_pool = resource_pool or ResourcePool(config.resources)
        self._concurrency_mgr = concurrency_mgr
        self._external_shutdown = shutdown_event
        log_root = config.logging.resolved_log_root()
        self.log_stream = LogStream(log_root, project_slug=config.project_slug)
        self.event_log = EventLog(log_root)
        self._warnings: list[str] = []  # startup warnings surfaced in TUI
        self._owns_ledger = ledger is None
        if ledger is not None:
            self.ledger = ledger
        else:
            ledger_path = config.reliability.resolved_database_path(log_root)
            try:
                self.ledger = SQLiteLedger(
                    ledger_path,
                    backup_before_migrate=config.reliability.backup_before_migrate,
                )
            except LedgerError:
                if not config.reliability.recover_read_only or not ledger_path.exists():
                    raise
                logger.exception(
                    "Ledger startup validation failed; attempting read-only recovery"
                )
                self.ledger = SQLiteLedger(ledger_path, read_only=True)
        if not self.ledger.read_only:
            reconciled = self.ledger.reconcile_open_runs(project_slug=config.project_slug)
            if reconciled:
                logger.warning(
                    "Reconciled %d interrupted run(s) from a previous process",
                    len(reconciled),
                )
        if self.ledger.read_only:
            self._warnings.append(
                "reliability ledger opened read-only — new dispatches are blocked"
            )
        self.tracer: AsyncBraintrustProjector | BraintrustTracer | None = None
        tracing_cfg = config.logging.tracing
        if bt_enabled(tracing_cfg.api_key):
            try:
                self.tracer = AsyncBraintrustProjector(
                    BraintrustTracer(
                        project=tracing_cfg.project,
                        api_key=tracing_cfg.api_key,
                    )
                )
            except Exception:
                logger.warning("Braintrust tracer init failed — tracing disabled", exc_info=True)
                self._warnings.append("Braintrust tracer init failed — tracing disabled")
        else:
            from autosymph.logging.braintrust import _AVAILABLE as _bt_available
            if not _bt_available:
                self._warnings.append("braintrust not installed — tracing disabled")
            else:
                self._warnings.append("BRAINTRUST_API_KEY not set — tracing disabled")
        self._poll_lock = asyncio.Lock()
        self._running: dict[str, _RunningAgent] = {}  # issue_id -> running agent info
        self._workspaces: dict[str, Workspace] = {}  # issue_id -> active workspace
        self._shutdown = asyncio.Event()
        self._tick_count = 0
        self._session_ids: dict[tuple[str, str], str] = {}  # (issue_id, runner) -> session_id
        self._last_failed_state: dict[str, str] = {}  # issue_id -> state that failed (for investigating)
        self._completed_today: list[str] = []  # identifiers completed today
        self._failed_today: list[str] = []  # identifiers failed today
        self._last_poll_time: float = 0
        self._on_status_change: Callable[[], None] | None = None  # TUI refresh callback
        self._resolved_assignee: str | None = None  # resolved on first tick

    @property
    def project_slug(self) -> str:
        """URL-safe project identifier from config."""
        return self.config.project_slug

    def _trace_call(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Project to Braintrust without letting it affect factory state."""
        tracer = self.tracer
        if tracer is None:
            return
        if isinstance(tracer, AsyncBraintrustProjector):
            tracer.submit(method, *args, **kwargs)
            return
        try:
            getattr(tracer, method)(*args, **kwargs)
        except Exception as exc:
            logger.warning("Braintrust %s failed — tracing disabled", method, exc_info=True)
            self._replace_warning(
                "Braintrust",
                f"Braintrust projection failed ({exc.__class__.__name__}) — tracing disabled",
            )
            self.tracer = None

    def _resolve_prompt_path(self, prompt_ref: str) -> Path:
        """Resolve a prompt path from config.

        If `prompts.root` is set, all relative prompt refs are resolved from
        that directory. Otherwise they remain relative to the config file.
        Absolute paths are used as-is.
        """
        prompt_path = Path(prompt_ref).expanduser()
        if prompt_path.is_absolute():
            return prompt_path.resolve()

        base_dir = self.config_path.parent
        if self.config.prompts.root:
            root_path = Path(self.config.prompts.root).expanduser()
            base_dir = root_path if root_path.is_absolute() else (self.config_path.parent / root_path)

        return (base_dir / prompt_path).resolve()

    # -- Main loop --

    async def run(self) -> None:
        """Start the poll loop. Blocks until shutdown signal."""
        logger.info(
            "Orchestrator started — polling every %dms",
            self.config.polling.interval_ms,
        )

        # Collect permission denials from previous runs on startup
        try:
            import subprocess as _sp
            script = self.config_path.parent.parent / "autosymph" / "scripts" / "collect-permission-denials.py"
            if script.exists():
                _sp.run(["python3", str(script)], capture_output=True, timeout=10)
                logger.info("Permission denial report updated")
        except Exception:
            logger.debug("Permission denial collection skipped", exc_info=True)

        # Signal handlers: only install if no external shutdown event (standalone mode).
        # In multi-instance mode, Supervisor owns signal handlers.
        if self._external_shutdown:
            self._shutdown = self._external_shutdown
        else:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        while not self._shutdown.is_set():
            try:
                self._hot_reload()
                await self.poll_tick()
            except Exception:
                logger.exception("Error in poll tick %d", self._tick_count)

            interval_s = self.config.polling.interval_ms / 1000
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass  # Normal — just means the interval elapsed

        logger.info("Orchestrator stopped after %d ticks", self._tick_count)
        if isinstance(self.tracer, AsyncBraintrustProjector):
            self.tracer.close()
        if self._owns_ledger:
            self.ledger.close()

    async def poll_tick(self) -> None:
        """Single poll iteration — fetch, reconcile, dispatch. Lock prevents concurrent polls."""
        if self._poll_lock.locked():
            logger.debug("Poll already in progress — skipping")
            return
        async with self._poll_lock:
            self._tick_count += 1
            self._last_poll_time = time.monotonic()
            self._notify_status_change()
            logger.debug("=== Tick %d ===", self._tick_count)

            # 1. Fetch issues from Linear
            if not self.linear.is_configured:
                logger.warning("Skipping tick — LINEAR_API_KEY not set")
                return

            # Resolve assignee filter on first tick (lazy — needs async)
            if self._resolved_assignee is None and self.linear._assignee_filter:
                try:
                    self._resolved_assignee = await self.linear.resolve_assignee_filter()
                    logger.info("Assignee filter resolved: %s", self._resolved_assignee)
                except Exception:
                    logger.warning("Failed to resolve assignee filter — polling all issues")

            statuses = self.sm.actionable_linear_statuses()
            try:
                issues = await self.linear.fetch_actionable_issues(
                    statuses,
                    assignee_id=self._resolved_assignee,
                    terminal_statuses=self.config.linear_states.terminal,
                )
            except Exception:
                logger.exception("Failed to fetch issues from Linear — skipping tick")
                return

            # 2. Reconcile — update tracked state, detect completions/stalls
            await self._reconcile(issues)

            # 3. Dispatch — spawn agents for eligible issues
            await self._dispatch(issues)

    # -- Reconciliation --

    async def _reconcile(self, issues: list[LinearIssue]) -> None:
        """Reconcile Linear state with internal tracking."""
        live_ids = {i.id for i in issues}

        # Detect issues that left actionable states (completed/canceled externally)
        for issue_id in list(self.sm.tracked_issues.keys()):
            if issue_id not in live_ids:
                tracked = self.sm.get_issue(issue_id)
                if tracked:
                    logger.info(
                        "%s no longer in actionable state — untracking",
                        tracked.identifier,
                    )
                    # Cancel running agent if any
                    await self._cancel_agent(issue_id)
                    self.sm.untrack_issue(issue_id)

        # Detect stalled agents
        stall_timeout_s = self.config.claude.stall_timeout_ms / 1000
        for issue_id, agent in list(self._running.items()):
            idle_s = time.monotonic() - agent.last_activity
            if idle_s > stall_timeout_s:
                tracked = self.sm.get_issue(issue_id)
                ident = tracked.identifier if tracked else issue_id
                logger.warning(
                    "%s agent stalled (%.0fs idle > %.0fs threshold) — killing",
                    ident, idle_s, stall_timeout_s,
                )
                self.event_log.timeout(ident, agent.workflow_state)
                await self._cancel_agent(issue_id, terminal_status="timed_out")
                self.sm.release(issue_id)

    # -- Dispatch --

    async def _dispatch(self, issues: list[LinearIssue]) -> None:
        """Select and dispatch eligible issues."""
        max_total = self.config.agent.max_concurrent_agents

        for issue in self._prioritize(issues):
            # Global concurrency check (local only — ConcurrencyManager checked before spawn)
            if not self._concurrency_mgr and self.sm.running_count() >= max_total:
                logger.debug("At max concurrent agents (%d) — skipping remaining", max_total)
                break

            # Already running?
            if issue.id in self._running:
                continue

            # Determine workflow state for this issue.
            # If already tracked (e.g. after internal transition like verify → verify_review),
            # use the tracked state — UNLESS the issue is tracked in a TERMINAL
            # state and the user has externally moved Linear back to an actionable
            # status (e.g. Blocked → Verifying to re-test after a structify fix).
            # IMPORTANT: do NOT re-resolve solely because tracked workflow_state
            # name differs from the resolved one — verify and verify_review share
            # Linear status "Verifying" by design, and re-resolving would kick
            # verify_review back into verify forever.
            tracked = self.sm.get_issue(issue.id)
            workflow_state: str | None
            if tracked and tracked.claim == ClaimState.UNCLAIMED:
                tracked_cfg = self.config.states.get(tracked.workflow_state)
                if tracked_cfg and tracked_cfg.type == "terminal":
                    # Terminally-tracked but Linear is showing actionable — user
                    # re-queued. Re-resolve.
                    resolved = self._resolve_workflow_state(issue)
                    if resolved and resolved != tracked.workflow_state:
                        logger.info(
                            "%s tracked terminally as '%s' but Linear shows actionable → re-resolving to '%s'",
                            tracked.identifier, tracked.workflow_state, resolved,
                        )
                        workflow_state = resolved
                        self.sm.track_issue(issue.id, issue.identifier, workflow_state)
                    else:
                        workflow_state = tracked.workflow_state
                else:
                    workflow_state = tracked.workflow_state
            else:
                workflow_state = self._resolve_workflow_state(issue)
            if not workflow_state:
                continue

            state_cfg = self.config.states.get(workflow_state)
            if not state_cfg:
                continue

            # Only dispatch agent states (not gates or terminals)
            if state_cfg.type != "agent":
                # Track gate issues so we see them in status
                self.sm.track_issue(issue.id, issue.identifier, workflow_state)
                continue

            # Per-state concurrency check
            state_limit = self.config.agent.max_concurrent_agents_by_state.get(workflow_state)
            if state_limit and self.sm.running_count_by_state(workflow_state) >= state_limit:
                logger.debug(
                    "At per-state limit for '%s' (%d) — skipping %s",
                    workflow_state, state_limit, issue.identifier,
                )
                continue

            runner_name = self._resolve_runner_name(issue, workflow_state, state_cfg)

            # Track and check rework cap
            self.sm.track_issue(issue.id, issue.identifier, workflow_state)

            if workflow_state == "rework":
                exhausted_target = self.sm.record_rework(issue.id)
                if exhausted_target:
                    # Rework cap exceeded — move back to todo for re-planning
                    logger.warning(
                        "%s rework cap exceeded — moving to '%s'",
                        issue.identifier, exhausted_target,
                    )
                    linear_status = self._workflow_to_linear_status(exhausted_target)
                    if linear_status:
                        try:
                            await self.linear.transition_issue(issue.id, linear_status)
                        except Exception:
                            logger.exception("Failed to transition %s to %s", issue.identifier, exhausted_target)
                    self.sm.untrack_issue(issue.id)
                    continue

            # Claim and dispatch
            if not self.sm.claim(issue.id):
                continue

            # ConcurrencyManager check (atomic acquire right before spawn)
            if self._concurrency_mgr:
                if not await self._concurrency_mgr.acquire(self.project_slug):
                    logger.debug(
                        "ConcurrencyManager denied dispatch for %s/%s",
                        self.project_slug, issue.identifier,
                    )
                    self.sm.release(issue.id)
                    continue

            await self._spawn_agent(issue, workflow_state, state_cfg, runner_name)

    def _prioritize(self, issues: list[LinearIssue]) -> list[LinearIssue]:
        """Sort issues by priority (lower number = higher priority), then by identifier."""
        return sorted(issues, key=lambda i: (i.priority, i.identifier))

    def _resolve_workflow_state(self, issue: LinearIssue) -> str | None:
        """Determine which workflow state an issue should be in based on its Linear status."""
        ls = self.config.linear_states
        status = issue.status

        # Direct status mappings
        # Autoplan — checked before todo so explicit `Autoplan` state takes priority.
        if ls.autoplan and status == ls.autoplan:
            return "autoplan"
        if status == ls.todo:
            return "implement"
        if status == ls.verifying:
            return "verify"
        if hasattr(ls, 'verify_review') and status == ls.verify_review:
            return "verify_review"
        if status == ls.rework:
            return "rework"
        if status == ls.review:
            return "review"
        if status == ls.investigating:
            return "investigating"
        if status == ls.blocked:
            return "blocked"
        if status == ls.gate_approved:
            return "finalize"
        if status in ls.terminal:
            return "done"

        # Active status — check if we're already tracking this issue
        if status == ls.active:
            tracked = self.sm.get_issue(issue.id)
            if tracked:
                return tracked.workflow_state
            # New active issue — default to implement
            return "implement"

        return None

    def _resolve_runner_name(
        self,
        issue: LinearIssue,
        workflow_state: str,
        state_cfg: StateConfig,
    ) -> str:
        """Resolve the runner for an issue/state.

        Precedence: explicit Linear `runner:<name>` label, first matching
        auto_match rule, per-state runner, config default, then Claude.
        """
        del workflow_state  # Kept in the signature for tests and future audit logging.

        for label in issue.labels:
            if not label.startswith("runner:"):
                continue
            runner_name = label.removeprefix("runner:")
            if not RUNNER_NAME_RE.fullmatch(runner_name):
                raise ConfigError(
                    f"{issue.identifier} has malformed runner label '{label}'"
                )
            return self._require_registered_runner(runner_name)

        issue_labels = set(issue.labels)
        for rule in self.config.runners.auto_match:
            if rule.title and not re.search(rule.title, issue.title, re.IGNORECASE):
                continue
            if rule.labels and not set(rule.labels).issubset(issue_labels):
                continue
            return self._require_registered_runner(rule.runner)

        if state_cfg.runner:
            return self._require_registered_runner(state_cfg.runner)

        return self._require_registered_runner(self.config.runners.default or "claude")

    def _require_registered_runner(self, runner_name: str) -> str:
        if runner_name not in self.config.runners.available:
            raise ConfigError(f"Runner '{runner_name}' is not enabled in runners.available")
        if runner_name not in self.runners:
            raise ConfigError(f"Runner '{runner_name}' is not registered")
        return runner_name

    # -- Agent lifecycle --

    async def _spawn_agent(
        self,
        issue: LinearIssue,
        workflow_state: str,
        state_cfg: Any,
        runner_name: str,
    ) -> None:
        """Spawn an agent for an issue."""
        try:
            allocation = self.ledger.allocate_run(
                project_slug=self.project_slug,
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                state=workflow_state,
                runner=runner_name,
                metadata={"model": state_cfg.model or self.config.claude.model},
            )
        except Exception as exc:
            logger.exception("Durable dispatch allocation failed for %s", issue.identifier)
            self._replace_warning(
                issue.identifier,
                f"{issue.identifier}: durable dispatch blocked — {str(exc)[:80]}",
            )
            self.sm.release(issue.id)
            if self._concurrency_mgr:
                await self._concurrency_mgr.release(self.project_slug)
            return

        try:
            self.sm.mark_running(issue.id)
            prompt, runner_config, session_id, issue_slug = self._prepare_agent_dispatch(
                issue, workflow_state, state_cfg, runner_name
            )
            # Acquire resources before creating the task. The source repo is
            # used for project-type detection because all worktrees share it.
            resources = await self.resource_pool.acquire_for_issue(
                issue.id, workflow_state, issue.labels,
                workspace_path=self.workspace_mgr.repo,
                project_slug=self.project_slug if self._concurrency_mgr else None,
            )
        except Exception as exc:
            self.ledger.record_terminal(
                run_id=allocation.run_id,
                event_type="preflight_blocked",
                status="preflight_blocked",
                payload={
                    "stage": "dispatch_preflight",
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                },
            )
            logger.exception("Dispatch preflight failed for %s", issue.identifier)
            self.sm.release(issue.id)
            if self._concurrency_mgr:
                await self._concurrency_mgr.release(self.project_slug)
            return

        # Spawn as async task
        task = asyncio.create_task(
            self._run_agent(
                issue,
                workflow_state,
                prompt,
                runner_config,
                session_id,
                issue_slug,
                runner_name,
                resources,
                allocation,
            ),
            name=f"agent-{issue.identifier}",
        )
        self._running[issue.id] = _RunningAgent(
            task=task,
            run_id=allocation.run_id,
            workflow_state=workflow_state,
            runner_name=runner_name,
            started_at=time.monotonic(),
            last_activity=time.monotonic(),
            dispatched_at_iso=_utc_now_iso(),
        )

    def _prepare_agent_dispatch(
        self,
        issue: LinearIssue,
        workflow_state: str,
        state_cfg: Any,
        runner_name: str,
    ) -> tuple[str, dict[str, Any], str | None, str]:
        """Build prompt and runner inputs before acquiring resources."""
        prompt = ""
        if state_cfg.prompt:
            prompt_path = self._resolve_prompt_path(state_cfg.prompt)
            if prompt_path.exists():
                prompt = prompt_path.read_text()
            else:
                logger.warning("Prompt file not found: %s", prompt_path)

        if self.config.prompts.global_prompt:
            global_path = self._resolve_prompt_path(self.config.prompts.global_prompt)
            if global_path.exists():
                prompt = global_path.read_text() + "\n\n" + prompt

        prompt = f"Issue: {issue.identifier} — {issue.title}\n\n{prompt}"
        if issue.description:
            prompt += f"\n\n## Issue Description\n\n{issue.description}"

        logger.info("Dispatching %s → '%s' runner=%s", issue.identifier, workflow_state, runner_name)
        runner_config: dict[str, Any] = {
            "model": state_cfg.model or self.config.claude.model,
            "permission_mode": state_cfg.permission_mode or self.config.claude.permission_mode,
            "max_turns": state_cfg.max_turns or self.config.claude.max_turns,
            "identifier": issue.identifier,
            "workflow_state": workflow_state,
            "prompt_path": state_cfg.prompt or "-",
            "runner": runner_name,
        }
        if state_cfg.allowed_tools:
            runner_config["allowed_tools"] = state_cfg.allowed_tools
        if state_cfg.mcp_config:
            runner_config["mcp_config"] = state_cfg.mcp_config

        session_id = None
        if state_cfg.session == "inherit":
            session_id = self._session_ids.get((issue.id, runner_name))
        return prompt, runner_config, session_id, issue.identifier.lower()

    @staticmethod
    def _session_name(state: str, identifier: str, run: int) -> str:
        """Session naming: {issue}-{state}-run{N} (e.g. ISSUE-123-implement-run1)."""
        return f"{identifier}-{state}-run{run}"

    async def _run_agent(
        self,
        issue: LinearIssue,
        workflow_state: str,
        prompt: str,
        runner_config: dict[str, Any],
        session_id: str | None,
        issue_slug: str,
        runner_name: str,
        resources: AcquiredResources | None = None,
        allocation: RunAllocation | None = None,
    ) -> None:
        """Create worktree, run agent, log to disk, post to Linear, clean up."""
        workspace = None
        failed = False
        log_path = None
        if allocation is None:
            allocation = self.ledger.allocate_run(
                project_slug=self.project_slug,
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                state=workflow_state,
                runner=runner_name,
                metadata={"model": runner_config.get("model")},
            )
        run_id = allocation.run_id
        run_number = allocation.attempt
        session_name = self._session_name(workflow_state, issue.identifier, run_number)
        timeline = TimelineExtractor()
        summarizer = ActivitySummarizer()

        try:
            # Tier 2: open log file
            log_path = self.log_stream.open(issue_slug, workflow_state, run_number)
            self.ledger.mark_run_started(run_id, raw_log_path=log_path)

            # Tier 3: log dispatch
            self.event_log.dispatch(
                issue.identifier, workflow_state, run_number, session_name, runner=runner_name,
            )
            self.ledger.append_event(
                run_id=run_id,
                project_slug=self.project_slug,
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                event_type="runner_started",
                idempotency_key=f"runner-started:{run_id}",
                payload={"session_name": session_name, "runner": runner_name},
            )

            # Create isolated worktree
            workspace = await self.workspace_mgr.create(issue_slug)
            self._workspaces[issue.id] = workspace
            logger.info("[%s] workspace ready at %s", session_name, workspace.path)

            # Reset infra crash counter — workspace created OK
            tracked_issue = self.sm.get_issue(issue.id)
            if tracked_issue and tracked_issue.infra_crash_count > 0:
                logger.info("[%s] infra crash counter reset (was %d)", session_name, tracked_issue.infra_crash_count)
                tracked_issue.infra_crash_count = 0
                tracked_issue.last_infra_error = None

            # Resolve resource placeholders now that we have the workspace path
            if resources and resources.items.pop("_NEEDS_DERIVED_DATA", None):
                resources.items["AUTOSYMPH_DERIVED_DATA"] = str(workspace.path / "DerivedData")

            # Template variables for investigating prompt
            if workflow_state == "investigating":
                log_root = self.config.logging.resolved_log_root()
                autosymph_root = str(self.config_path.parent)
                prompt = prompt.replace("{target_worktree}", str(workspace.path))
                prompt = prompt.replace("{autosymph_root}", autosymph_root)
                prompt = prompt.replace("{failed_state}", self._last_failed_state.get(issue.id, "unknown"))
                prompt = prompt.replace("{orchestrator_log_path}", str(log_root / "orchestrator.log"))
                prompt = prompt.replace("{result_file}", str(workspace.path / ".autosymph-result.json"))
                prompt = prompt.replace("{linear_issue_id}", issue.identifier)

            # Run before_run hook (fetch, rebase, etc.)
            await self.workspace_mgr.prepare(workspace)

            # Braintrust: open run span
            self._trace_call(
                "start_run",
                issue_id=issue.id,
                identifier=issue.identifier,
                state=workflow_state,
                run_number=run_number,
                prompt=prompt,
                config=runner_config,
            )

            def on_event(event):
                self.mark_activity(issue.id)
                timeline.ingest(event)
                # Activity summarizer — fail-soft. Any exception sets `errored`,
                # routing the comment renderer to the legacy timeline block.
                try:
                    summarizer.ingest(event)
                except Exception as exc:
                    if not summarizer.errored:
                        logger.warning(
                            "[%s] activity summary ingest failed: %s",
                            session_name,
                            exc.__class__.__name__,
                        )
                    summarizer.errored = True
                # Track tool calls and tokens for TUI
                agent = self._running.get(issue.id)
                if agent:
                    if event.type == EventType.TOOL_CALL:
                        agent.last_tool = event.data.get("tool_name", "")
                    # Track tokens from assistant turns or dedicated token_usage events.
                    # Per-turn `usage` is for THAT message only (not conversation-cumulative),
                    # so accumulate. Sum input + output + cache to reflect real spend — for
                    # tool-call turns `output_tokens` alone is often 1-3, which made the TUI
                    # display useless (an earlier version assigned `=` based on a wrong
                    # "cumulative per turn" assumption).
                    usage = event.data.get("usage", {})
                    if not usage:
                        usage = event.data.get("message", {}).get("usage", {})
                    if usage:
                        agent.tokens += (
                            usage.get("input_tokens", 0)
                            + usage.get("output_tokens", 0)
                            + usage.get("cache_creation_input_tokens", 0)
                            + usage.get("cache_read_input_tokens", 0)
                        )
                # Braintrust: forward events for tool span creation
                self._trace_call("on_event", issue.id, event)

            def on_raw_line(line):
                self.mark_activity(issue.id)  # any stdout = not stalled
                self.log_stream.write_line(log_path, line)
                # Count turns (each assistant message = 1 turn)
                agent = self._running.get(issue.id)
                if agent and '"type":"assistant"' in line and '"role":"assistant"' in line:
                    agent.turns += 1

            # Pass acquired resources as env vars
            if resources and resources.items:
                runner_config["env_vars"] = resources.as_env()
                logger.info("[%s] resources: %s", session_name, resources.items)

            # The model-selection audit log lives in the runner
            # (runners/claude.py) so it captures the actual argv passed to claude,
            # not just the runner_config dict here. See ClaudeRunner.run.
            logger.info("[%s] spawning agent runner=%s", session_name, runner_name)
            result = await self.runners[runner_name].run(
                prompt=prompt,
                workspace_path=str(workspace.path),
                config=runner_config,
                session_id=session_id,
                on_event=on_event,
                on_raw_line=on_raw_line,
            )

            # Store session_id for resume
            if result.session_id:
                self._session_ids[(issue.id, runner_name)] = result.session_id

            total_tokens = sum(result.token_usage.values())
            logger.info(
                "[%s] finished: runner=%s success=%s duration=%.1fs tokens=%s",
                session_name, runner_name, result.success, result.duration_seconds, result.token_usage,
            )

            # Tier 2: write meta file with timeline
            if log_path:
                self.log_stream.write_meta(
                    log_path,
                    issue_id=issue_slug,
                    state=workflow_state,
                    run_number=run_number,
                    success=result.success,
                    exit_code=result.exit_code,
                    duration_seconds=result.duration_seconds,
                    token_usage=result.token_usage,
                    session_id=result.session_id,
                    error=result.error,
                    runner=runner_name,
                    run_id=run_id,
                )

            # Tier 3: log outcome
            if result.success:
                self.event_log.complete(
                    issue.identifier, workflow_state, run_number,
                    result.duration_seconds, total_tokens,
                )
            else:
                self.event_log.fail(
                    issue.identifier, workflow_state, run_number, result.error,
                )

            self.ledger.record_terminal(
                run_id=run_id,
                event_type="run_completed" if result.success else "run_failed",
                status="completed" if result.success else "failed",
                payload={
                    "exit_code": result.exit_code,
                    "duration_seconds": result.duration_seconds,
                    "token_usage": result.token_usage,
                    "session_id": result.session_id,
                    "runner": runner_name,
                    "error": result.error,
                },
            )

            # Post summary comment to Linear (activity summary preferred, timeline fallback)
            await self._post_run_summary(
                issue,
                workflow_state,
                run_number,
                result,
                log_path,
                session_name,
                runner_name,
                timeline,
                summarizer,
            )

            failed = not result.success
            await self.on_agent_complete(issue.id, result.success, issue, run_id=run_id)

        except asyncio.CancelledError:
            logger.info("[%s] cancelled", session_name)
            self.event_log.fail(issue.identifier, workflow_state, run_number, "cancelled")
            self.ledger.record_terminal(
                run_id=run_id,
                event_type="run_cancelled",
                status="cancelled",
                payload={"reason": "cancelled"},
            )
            failed = True
            self.sm.release(issue.id)
        except Exception as exc:
            logger.exception("[%s] crashed", session_name)
            self.event_log.fail(issue.identifier, workflow_state, run_number, "crash")
            self.ledger.record_terminal(
                run_id=run_id,
                event_type="run_crashed",
                status="crashed",
                payload={"error": str(exc), "error_type": exc.__class__.__name__},
            )
            failed = True

            # Track consecutive infra/workspace crashes and bail after MAX attempts
            MAX_INFRA_CRASHES = 3
            tracked = self.sm.get_issue(issue.id)
            if tracked:
                tracked.infra_crash_count += 1
                tracked.last_infra_error = str(exc)[:200]
                if tracked.infra_crash_count >= MAX_INFRA_CRASHES:
                    logger.error(
                        "%s hit infra crash cap (%d/%d) — moving to blocked: %s",
                        tracked.identifier, tracked.infra_crash_count,
                        MAX_INFRA_CRASHES, tracked.last_infra_error,
                    )
                    self._replace_warning(
                        tracked.identifier,
                        f"{tracked.identifier}: infra crash — {tracked.last_infra_error[:80]}"
                    )
                    blocked_status = self._workflow_to_linear_status("blocked")
                    if blocked_status:
                        try:
                            await self.linear.transition_issue(issue.id, blocked_status)
                        except Exception:
                            logger.exception("Failed to transition %s to blocked", tracked.identifier)
                    try:
                        await self.linear.post_comment(
                            issue.id,
                            f"**Infra crash cap exceeded** ({MAX_INFRA_CRASHES} consecutive failures).\n\n"
                            f"```\n{tracked.last_infra_error}\n```\n\n"
                            f"Issue moved to Blocked. Fix the infra issue and move back to Ready.",
                        )
                    except Exception:
                        logger.exception("Failed to post blocked comment for %s", tracked.identifier)
                    self._failed_today.append(tracked.identifier)
                    self._notify_status_change()
                    self.sm.untrack_issue(issue.id)
                    return  # skip normal release — issue is untracked
                else:
                    self._replace_warning(
                        tracked.identifier,
                        f"{tracked.identifier}: infra crash {tracked.infra_crash_count}/{MAX_INFRA_CRASHES} — {tracked.last_infra_error[:60]}"
                    )

            self.sm.release(issue.id)
        finally:
            if log_path and log_path.exists():
                try:
                    self.ledger.attach_raw_log(run_id, log_path)
                except Exception:
                    logger.exception("Failed to attach raw log metadata for %s", session_name)
            # Braintrust: always close run span (crash-safe)
            self._trace_call(
                "end_run",
                issue.id,
                result if "result" in locals() else RunResult(
                    success=False, exit_code=-1, error="crash",
                ),
            )

            # Always release resources (crash-safe)
            if resources:
                self.resource_pool.release_all(
                    issue.id,
                    project_slug=self.project_slug if self._concurrency_mgr else None,
                )

            self._workspaces.pop(issue.id, None)
            if workspace and failed:
                try:
                    await self.workspace_mgr.cleanup(workspace, failed=True)
                except Exception:
                    logger.exception("Workspace cleanup failed for %s", issue.identifier)

    def _render_activity_block(
        self,
        state: str,
        session_name: str,
        summarizer: ActivitySummarizer | None,
        timeline: TimelineExtractor | None,
        failure_modes: list[str] | None = None,
    ) -> str:
        """Render the body block between header and `[Full log]`.

        Returns the high-level activity digest when enabled and successful,
        otherwise the legacy timeline code block. Always fail-soft: a raise from
        ``format_digest`` falls through to the legacy block. When the summarizer
        is disabled, errored, or empty, the legacy timeline block is rendered.
        """
        legacy = ""
        if timeline and timeline.entries:
            legacy = f"\n```\n{timeline.format_timeline()}\n```"

        state_cfg = self.config.states.get(state) if self.config else None
        if state_cfg is None or not isinstance(state_cfg, StateConfig):
            return legacy

        enabled = resolve_activity_summary(self.config, state_cfg)
        if not enabled or summarizer is None or summarizer.errored:
            return legacy

        try:
            rendered = summarizer.format_digest(extra_failure_modes=failure_modes)
        except Exception as exc:
            logger.warning(
                "[%s] activity summary failed: %s", session_name, exc.__class__.__name__,
            )
            return legacy

        return f"\n\n{rendered}" if rendered else legacy

    async def _post_run_summary(
        self,
        issue: LinearIssue,
        state: str,
        run_number: int,
        result: RunResult,
        log_path: Path | None,
        session_name: str = "",
        runner_name: str = "claude",
        timeline: TimelineExtractor | None = None,
        summarizer: ActivitySummarizer | None = None,
    ) -> None:
        """Post a summary comment to the Linear issue after each agent run."""
        outcome = "completed" if result.success else "failed"
        tokens = result.token_usage
        token_str = ""
        if tokens:
            parts = []
            if tokens.get("input_tokens"):
                parts.append(f"{tokens['input_tokens']:,} in")
            if tokens.get("output_tokens"):
                parts.append(f"{tokens['output_tokens']:,} out")
            token_str = f" | tokens: {', '.join(parts)}" if parts else ""

        duration = f"{result.duration_seconds:.0f}s"
        failure_modes: list[str] = []
        if result.error:
            failure_modes.append(f"Run exited with error: {result.error}")
        elif not result.success:
            failure_modes.append(f"Run exited unsuccessfully with code {result.exit_code}.")

        # Upload NDJSON log to Linear CDN
        log_line = ""
        if log_path and log_path.exists():
            try:
                asset_url = await self.linear.upload_file(
                    str(log_path),
                    filename=f"{session_name}.ndjson",
                )
                log_line = f"\n**Log:** [NDJSON]({asset_url})"
            except Exception:
                logger.warning(
                    "Failed to upload log for %s — falling back to path",
                    issue.identifier,
                )
                log_line = f"\n**Log:** `{log_path}`"

        activity_block = self._render_activity_block(
            state=state,
            session_name=session_name,
            summarizer=summarizer,
            timeline=timeline,
            failure_modes=failure_modes,
        )

        body = (
            f"**`{session_name}`** {outcome} "
            f"({duration}{token_str}) | runner: {runner_name}"
            f"{log_line}{activity_block}"
        )

        try:
            await self.linear.post_comment(issue.id, body)
        except Exception:
            logger.warning("Failed to post summary comment for %s", issue.identifier)

    async def _cancel_agent(
        self, issue_id: str, *, terminal_status: str | None = None
    ) -> None:
        """Cancel a running agent task."""
        agent = self._running.pop(issue_id, None)
        if agent:
            if terminal_status:
                self.ledger.record_terminal(
                    run_id=agent.run_id,
                    event_type=f"run_{terminal_status}",
                    status=terminal_status,
                    payload={"reason": terminal_status},
                )
            # Release concurrency slot
            if self._concurrency_mgr:
                await self._concurrency_mgr.release(self.project_slug)
            if not agent.task.done():
                agent.task.cancel()
                try:
                    await agent.task
                except (asyncio.CancelledError, Exception):
                    pass

    def mark_activity(self, issue_id: str) -> None:
        """Update last activity timestamp for stall detection."""
        if issue_id in self._running:
            self._running[issue_id].last_activity = time.monotonic()

    async def on_agent_complete(
        self,
        issue_id: str,
        success: bool,
        issue: LinearIssue | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        """Handle agent completion — transition state, clean up."""
        agent = self._running.pop(issue_id, None)
        if agent and self._concurrency_mgr:
            await self._concurrency_mgr.release(self.project_slug)
        tracked = self.sm.get_issue(issue_id)
        if not tracked:
            return

        if success:
            signal_to_use = self._determine_signal(tracked, issue, issue_id)

            # Verify review: override signal based on structured verdict in Linear comment
            if tracked.workflow_state == "verify_review":
                # Pass dispatch time so the parser ignores stale verdicts from prior runs.
                # Without this, a reject_structify from run 1 would re-fire on every later
                # run that fails to post a fresh verdict.
                dispatched_at = agent.dispatched_at_iso if agent else None
                signal_to_use = await self._extract_verify_review_verdict(
                    issue_id, tracked.identifier, signal_to_use,
                    min_created_at=dispatched_at,
                )

            # Investigating: route fixed/workaround back to the state that failed
            target: str | None
            if (
                tracked.workflow_state == "investigating"
                and signal_to_use in (Signal.COMPLETE, Signal.WORKAROUND)
                and issue_id in self._last_failed_state
            ):
                target = self._last_failed_state[issue_id]
            elif (
                tracked.workflow_state == "investigating"
                and signal_to_use == Signal.NOT_AUTOSYMPH
                and self._last_failed_state.get(issue_id) == "verify_review"
            ):
                # verify_review falsely rejected (e.g. structify suggested but
                # the code is actually fine). Investigator confirms not_autosymph.
                # Route to verify, not back to verify_review — re-running the
                # same reviewer on the same evidence would just re-reject.
                # A fresh verify run produces fresh evidence for review.
                target = "verify"
                logger.info(
                    "%s investigator confirmed verify_review false rejection — "
                    "routing to verify for fresh evidence",
                    tracked.identifier,
                )
            else:
                target = self.sm.next_state(tracked.workflow_state, signal_to_use, issue_id)

            # Fall back to COMPLETE if the specific signal has no transition
            if not target and signal_to_use not in (Signal.COMPLETE, Signal.COMPLETE_LOW_RISK):
                target = self.sm.next_state(tracked.workflow_state, Signal.COMPLETE, issue_id)

            if target:
                logger.info(
                    "%s completed '%s' → '%s' (signal=%s)",
                    tracked.identifier, tracked.workflow_state, target, signal_to_use.value,
                )
                self.event_log.state_change(tracked.identifier, tracked.workflow_state, target)
                if run_id:
                    self.ledger.record_transition(
                        run_id=run_id,
                        from_state=tracked.workflow_state,
                        to_state=target,
                        signal=signal_to_use.value,
                    )
                # Transition in Linear
                target_cfg = self.config.states.get(target)
                if target_cfg and target_cfg.linear_state:
                    linear_status = self._workflow_to_linear_status(target_cfg.linear_state)
                    if linear_status:
                        try:
                            await self.linear.transition_issue(issue_id, linear_status)
                        except Exception:
                            logger.exception("Failed to transition %s in Linear", tracked.identifier)

                if target == "done" or (target_cfg and target_cfg.type == "terminal"):
                    self._trace_call("end_issue", issue_id, success=True)
                    self.sm.untrack_issue(issue_id)
                    self._completed_today.append(tracked.identifier)
                    self._notify_status_change()
                else:
                    tracked.workflow_state = target
                    self.sm.release(issue_id)
            else:
                logger.error(
                    "%s completed but no valid transition from '%s' (signal=%s) — untracking",
                    tracked.identifier, tracked.workflow_state, signal_to_use.value,
                )
                self._trace_call("end_issue", issue_id, success=False)
                self.sm.untrack_issue(issue_id)
        else:
            # Agent failed — record state for investigating prompt context
            self._last_failed_state[issue_id] = tracked.workflow_state

            # Try the FAIL signal for a fallback transition
            target = self.sm.next_state(tracked.workflow_state, Signal.FAIL, issue_id)
            if target:
                # Verify retry cap: track verify ↔ verify_review cycles
                MAX_VERIFY_RETRIES = 15
                if tracked.workflow_state == "verify_review" and target == "verify":
                    tracked.verify_retry_count += 1
                    if tracked.verify_retry_count >= MAX_VERIFY_RETRIES:
                        logger.error(
                            "%s exceeded verify retry cap (%d/%d) — moving to blocked",
                            tracked.identifier, tracked.verify_retry_count, MAX_VERIFY_RETRIES,
                        )
                        self.event_log.state_change(
                            tracked.identifier, tracked.workflow_state, "blocked",
                        )
                        blocked_status = self._workflow_to_linear_status("blocked")
                        if blocked_status:
                            try:
                                await self.linear.transition_issue(issue_id, blocked_status)
                            except Exception:
                                logger.exception("Failed to transition %s to blocked", tracked.identifier)
                        await self.linear.post_comment(
                            issue_id,
                            f"Verify retry cap exceeded ({MAX_VERIFY_RETRIES} cycles). "
                            f"verify_review kept rejecting verification evidence. "
                            f"Issue moved to Blocked for human review.",
                        )
                        self._failed_today.append(tracked.identifier)
                        self._notify_status_change()
                        self.sm.untrack_issue(issue_id)
                        return

                logger.warning(
                    "%s failed in '%s' → falling back to '%s'",
                    tracked.identifier, tracked.workflow_state, target,
                )
                if run_id:
                    self.ledger.record_transition(
                        run_id=run_id,
                        from_state=tracked.workflow_state,
                        to_state=target,
                        signal=Signal.FAIL.value,
                    )
                tracked.workflow_state = target
                self.sm.release(issue_id)
            else:
                self._failed_today.append(tracked.identifier)
                self._notify_status_change()
                logger.error(
                    "%s failed in '%s' with no fallback — untracking",
                    tracked.identifier, tracked.workflow_state,
                )
                self._trace_call("end_issue", issue_id, success=False)
                self.sm.untrack_issue(issue_id)

    def _determine_signal(
        self, tracked: Any, issue: LinearIssue | None, issue_id: str,
    ) -> Signal:
        """Determine the routing signal for a successful agent completion.

        For the investigating state, reads .autosymph-result.json to determine
        the signal (fixed, workaround, not_autosymph, escalate).
        For other states, uses risk-based routing.
        """
        if tracked.workflow_state == "investigating":
            workspace = self._workspaces.get(issue_id)
            if workspace:
                result_file = workspace.path / ".autosymph-result.json"
                if result_file.exists():
                    try:
                        data = json.loads(result_file.read_text())
                        signal_name = data.get("signal", "")
                        signal_map = {
                            "fixed": Signal.COMPLETE,
                            "workaround_applied": Signal.WORKAROUND,
                            "not_autosymph": Signal.NOT_AUTOSYMPH,
                            "escalate": Signal.ESCALATE,
                        }
                        result_signal = signal_map.get(signal_name)
                        if result_signal:
                            # not_autosymph only makes sense from verification failures.
                            # Allow it from both verify and verify_review — a falsely
                            # rejected verify_review is a legitimate not_autosymph case
                            # (investigator confirms code is fine; on_agent_complete
                            # below routes verify_review → verify to refresh evidence).
                            failed_state = self._last_failed_state.get(issue_id)
                            if result_signal == Signal.NOT_AUTOSYMPH and failed_state not in ("verify", "verify_review"):
                                logger.warning(
                                    "%s used not_autosymph from '%s' — treating as escalate",
                                    tracked.identifier, failed_state,
                                )
                                return Signal.ESCALATE

                            logger.info(
                                "%s investigation result: %s — %s",
                                tracked.identifier, signal_name,
                                data.get("summary", "no summary"),
                            )
                            return result_signal
                        logger.warning(
                            "%s unknown investigation signal '%s' — defaulting to COMPLETE",
                            tracked.identifier, signal_name,
                        )
                    except (json.JSONDecodeError, OSError) as e:
                        logger.warning(
                            "%s failed to read investigation result: %s — defaulting to COMPLETE",
                            tracked.identifier, e,
                        )
            # No result file = agent completed without writing one, treat as fixed
            return Signal.COMPLETE

        # Default: risk-based routing
        if issue and issue.is_low_risk:
            return Signal.COMPLETE_LOW_RISK
        return Signal.COMPLETE

    async def _extract_verify_review_verdict(
        self, issue_id: str, identifier: str, fallback_signal: Signal,
        min_created_at: str | None = None,
    ) -> Signal:
        """Parse the verify_review agent's Linear comment for a structured verdict.

        The agent posts a comment ending with:
            <!-- autosymph:verify_review {"decision":"approve"} -->
        or:
            <!-- autosymph:verify_review {"decision":"reject",...} -->

        The orchestrator trusts the verdict block, NOT the agent's signal.
        If no block found or decision is reject, returns FAIL.
        Also validates: if comment heading says REJECTED but decision says approve,
        treat as reject (catch contradictions).

        If `min_created_at` (ISO 8601) is given, comments older than that timestamp
        are ignored — this prevents a stale verdict from a prior run (e.g.
        reject_structify from run 1) from being re-matched on a later run that
        failed to post its own verdict block. Without this filter, the parser
        walks back through history and re-fires the old verdict on every run,
        creating an infinite cycle.
        """
        import re

        try:
            comments = await self.linear.fetch_recent_comments(issue_id)
        except Exception:
            logger.warning("%s failed to fetch comments for verdict — defaulting to FAIL", identifier)
            return Signal.FAIL

        # Sort newest-first when timestamps are available so the most recent
        # verdict wins. Linear's GraphQL ordering is not guaranteed to be
        # descending by createdAt, so don't trust the response order.
        def _created_at(c: Any) -> str:
            return getattr(c, "created_at", "") or ""

        def _created_at_dt(c: Any) -> datetime:
            return (
                _parse_iso_datetime(_created_at(c))
                or datetime.min.replace(tzinfo=timezone.utc)
            )

        min_created_dt = _parse_iso_datetime(min_created_at)
        if comments and any(_created_at(c) for c in comments):
            comments = sorted(comments, key=_created_at_dt, reverse=True)

        skipped_stale = 0
        for comment in comments:
            body = comment.body
            created_at = _created_at(comment)
            created_at_dt = _parse_iso_datetime(created_at)
            if min_created_dt and created_at_dt and created_at_dt < min_created_dt:
                if re.search(r'<!--\s*autosymph:verify_review', body):
                    skipped_stale += 1
                continue

            # Look for structured verdict block
            match = re.search(
                r'<!--\s*autosymph:verify_review\s+({.*?})\s*-->',
                body, re.DOTALL,
            )
            if not match:
                continue

            try:
                verdict = json.loads(match.group(1))
            except json.JSONDecodeError:
                logger.warning("%s malformed verdict JSON — defaulting to FAIL", identifier)
                return Signal.FAIL

            decision = verdict.get("decision", "").lower()

            # Contradiction check: heading says REJECTED but decision says approve
            if "REJECTED" in body and decision == "approve":
                logger.warning(
                    "%s verdict contradiction: heading says REJECTED but decision says approve — treating as reject",
                    identifier,
                )
                return Signal.FAIL

            if decision == "approve":
                # Use the original signal's risk routing (preserves risk:low detection)
                if fallback_signal == Signal.COMPLETE_LOW_RISK:
                    logger.info("%s verify_review APPROVED (risk:low)", identifier)
                    return Signal.COMPLETE_LOW_RISK
                logger.info("%s verify_review APPROVED", identifier)
                return Signal.COMPLETE

            elif decision == "reject":
                reasons = verdict.get("reasons", [])
                logger.info(
                    "%s verify_review REJECTED — %s",
                    identifier, "; ".join(reasons) if reasons else "no reasons given",
                )
                return Signal.FAIL

            elif decision == "reject_structify":
                reasons = verdict.get("reasons", [])
                logger.info(
                    "%s verify_review REJECTED (structify) — %s",
                    identifier, "; ".join(reasons) if reasons else "no reasons given",
                )
                return Signal.STRUCTIFY

            else:
                logger.warning("%s unknown verdict decision '%s' — defaulting to FAIL", identifier, decision)
                return Signal.FAIL

        # No verdict block found in any recent comment
        if skipped_stale:
            logger.warning(
                "%s no fresh verdict block — skipped %d stale block(s) from prior run(s) — defaulting to FAIL",
                identifier, skipped_stale,
            )
        else:
            logger.warning("%s no verdict block found in comments — defaulting to FAIL", identifier)
        return Signal.FAIL

    def _workflow_to_linear_status(self, linear_state_key: str) -> str | None:
        """Map a workflow linear_state value to the actual Linear status name."""
        ls = self.config.linear_states
        mapping = {
            "todo": ls.todo,
            "autoplan": ls.autoplan,  # None when autoplan is not configured
            "active": ls.active,
            "verifying": ls.verifying,
            "investigating": ls.investigating,
            "review": ls.review,
            "gate_approved": ls.gate_approved,
            "rework": ls.rework,
            "blocked": ls.blocked,
            "terminal": ls.terminal[0] if ls.terminal else None,
        }
        return mapping.get(linear_state_key, linear_state_key)

    # -- Hot-reload --

    def _hot_reload(self) -> None:
        """Re-read workflow.yaml. Changes take effect on the current tick."""
        try:
            new_config = load_config(self.config_path)
            validate_config(new_config)
            self.config = new_config
            self.sm.reload(new_config)
        except ConfigError as e:
            logger.warning("Hot-reload failed (keeping previous config): %s", e)

    # -- Shutdown --

    async def shutdown(self) -> None:
        """Gracefully stop all runners and exit."""
        logger.info("Shutting down orchestrator...")
        self._shutdown.set()
        for issue_id in list(self._running.keys()):
            await self._cancel_agent(issue_id)
            self.sm.release(issue_id)

    # -- Status --

    def _replace_warning(self, prefix: str, message: str) -> None:
        """Replace any existing warning with the same prefix, or append new."""
        self._warnings = [w for w in self._warnings if not w.startswith(prefix)]
        self._warnings.append(message)

    def _notify_status_change(self) -> None:
        """Notify TUI that status changed."""
        if self._on_status_change:
            self._on_status_change()

    def status_summary(self) -> dict[str, Any]:
        """Return a status snapshot for the TUI."""
        if isinstance(self.tracer, AsyncBraintrustProjector) and self.tracer.failure:
            failure = self.tracer.failure
            self._replace_warning(
                "Braintrust",
                f"Braintrust projection failed ({failure.__class__.__name__}) — tracing disabled",
            )
        now = time.monotonic()
        interval_s = self.config.polling.interval_ms / 1000
        next_poll_s = max(0, interval_s - (now - self._last_poll_time))

        runners = {}
        for issue_id, agent in self._running.items():
            tracked = self.sm.get_issue(issue_id)
            if tracked:
                runners[tracked.identifier] = {
                    "run_id": agent.run_id,
                    "state": agent.workflow_state,
                    "runner": agent.runner_name,
                    "duration_s": now - agent.started_at,
                    "idle_s": now - agent.last_activity,
                    "turns": agent.turns,
                    "tokens": agent.tokens,
                    "last_tool": agent.last_tool,
                }

        return {
            "tick": self._tick_count,
            "polling_interval_s": interval_s,
            "next_poll_s": next_poll_s,
            "runners": runners,
            "runners_active": len(self._running),
            "runners_max": self.config.agent.max_concurrent_agents,
            "completed_today": list(self._completed_today),
            "failed_today": list(self._failed_today),
            "tracked_issues": {
                s.identifier: {
                    "state": s.workflow_state,
                    "claim": s.claim.value,
                }
                for s in self.sm.tracked_issues.values()
            },
            "warnings": list(self._warnings),
            "reliability": {
                "mode": self.config.reliability.mode,
                "ledger_path": str(getattr(self.ledger, "path", "injected")),
                "read_only": self.ledger.read_only,
                "schema_version": getattr(self.ledger, "schema_version", None),
            },
            # Multi-instance metadata
            "project_slug": self.project_slug,
            "config_path": str(self.config_path),
            "repo_path": str(self.workspace_mgr.repo),
        }


class _RunningAgent:
    """Internal tracking for a running agent task."""

    __slots__ = ("task", "run_id", "workflow_state", "started_at", "last_activity",
                 "runner_name", "turns", "tokens", "last_tool", "dispatched_at_iso")

    def __init__(
        self,
        task: asyncio.Task,
        run_id: str,
        workflow_state: str,
        runner_name: str,
        started_at: float,
        last_activity: float,
        dispatched_at_iso: str | None = None,
    ) -> None:
        self.task = task
        self.run_id = run_id
        self.workflow_state = workflow_state
        self.runner_name = runner_name
        self.started_at = started_at
        self.last_activity = last_activity
        self.dispatched_at_iso = dispatched_at_iso or _utc_now_iso()
        self.turns: int = 0
        self.tokens: int = 0
        self.last_tool: str = ""
