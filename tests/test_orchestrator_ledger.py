"""Lifecycle integration tests proving dispatches cannot disappear."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from autosymph.config import (
    LoggingConfig,
    StateConfig,
    StateTransitions,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from autosymph.ledger import SQLiteLedger
from autosymph.linear_client import LinearIssue
from autosymph.logging.braintrust import AsyncBraintrustProjector
from autosymph.orchestrator import Orchestrator
from autosymph.resources import AcquiredResources
from autosymph.state_machine import StateMachine


class _ResourcePool:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def acquire_for_issue(self, issue_id, workflow_state, labels, **kwargs):
        if self.error:
            raise self.error
        return AcquiredResources(holder_id=issue_id)

    def release_all(self, issue_id, **kwargs) -> None:
        return None


def _config(tmp_path: Path) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(project="Autosymph", api_key="test"),
        workspace=WorkspaceConfig(root=str(tmp_path / "worktrees"), repo=str(tmp_path)),
        logging=LoggingConfig(log_root=str(tmp_path / "logs")),
        states={
            "implement": StateConfig(
                type="agent",
                transitions=StateTransitions(**{"complete": "done", "fail": "done"}),
            ),
            "done": StateConfig(type="terminal"),
        },
    )


def _orchestrator(tmp_path: Path, pool: _ResourcePool):
    config = _config(tmp_path)
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    workspace = MagicMock()
    workspace.repo = tmp_path
    orchestrator = Orchestrator(
        config=config,
        config_path=tmp_path / "workflow.yaml",
        linear=MagicMock(),
        state_machine=StateMachine(config),
        workspace_mgr=workspace,
        resource_pool=pool,
        ledger=ledger,
    )
    issue = LinearIssue(
        id="linear-1",
        identifier="IMP-552",
        title="Durable ledger",
        status="Ready",
    )
    orchestrator.sm.track_issue(issue.id, issue.identifier, "implement")
    assert orchestrator.sm.claim(issue.id)
    return orchestrator, workspace, ledger, issue


@pytest.mark.asyncio
async def test_resource_preflight_failure_is_terminal(tmp_path: Path) -> None:
    orchestrator, _workspace, ledger, issue = _orchestrator(
        tmp_path, _ResourcePool(RuntimeError("simulator unavailable"))
    )

    await orchestrator._spawn_agent(
        issue,
        "implement",
        orchestrator.config.states["implement"],
        "claude",
    )

    run = ledger._connection.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "failed"
    assert run["process_outcome"] == "failed"
    assert [row["event_type"] for row in ledger.list_events(run_id=run["run_id"])][-1] == (
        "preflight_blocked"
    )
    ledger.close()


@pytest.mark.asyncio
async def test_prompt_preflight_failure_is_terminal(tmp_path: Path) -> None:
    orchestrator, _workspace, ledger, issue = _orchestrator(tmp_path, _ResourcePool())
    orchestrator._prepare_agent_dispatch = MagicMock(
        side_effect=OSError("prompt became unreadable")
    )

    await orchestrator._spawn_agent(
        issue,
        "implement",
        orchestrator.config.states["implement"],
        "claude",
    )

    run = ledger._connection.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "failed"
    terminal = ledger._connection.execute(
        "SELECT event_type, payload_json FROM events WHERE event_id = ?",
        (run["terminal_event_id"],),
    ).fetchone()
    assert terminal["event_type"] == "preflight_blocked"
    assert json.loads(terminal["payload_json"])["stage"] == "dispatch_preflight"
    ledger.close()


@pytest.mark.asyncio
async def test_workspace_crash_is_terminal_and_keeps_raw_attempt(tmp_path: Path) -> None:
    orchestrator, workspace, ledger, issue = _orchestrator(tmp_path, _ResourcePool())
    workspace.create = AsyncMock(side_effect=RuntimeError("worktree creation failed"))

    await orchestrator._spawn_agent(
        issue,
        "implement",
        orchestrator.config.states["implement"],
        "claude",
    )
    await orchestrator._running[issue.id].task

    run = ledger._connection.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "failed"
    assert run["terminal_event_id"] is not None
    assert "run_crashed" in [
        row["event_type"] for row in ledger.list_events(run_id=run["run_id"])
    ]
    ledger.close()


@pytest.mark.asyncio
async def test_completion_commits_run_event_and_transition_together(tmp_path: Path) -> None:
    orchestrator, _workspace, ledger, issue = _orchestrator(tmp_path, _ResourcePool())
    run = ledger.allocate_run(
        project_slug="autosymph",
        issue_id=issue.id,
        issue_identifier=issue.identifier,
        state="implement",
        runner="claude",
    )

    await orchestrator.on_agent_complete(
        issue.id,
        True,
        issue,
        run_id=run.run_id,
        terminal_event_type="run_completed",
        terminal_status="completed",
        terminal_payload={"exit_code": 0},
    )

    stored = ledger.get_run(run.run_id)
    decision = ledger._connection.execute(
        "SELECT * FROM transition_decisions WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    assert stored["status"] == "completed"
    assert stored["terminal_event_id"] is not None
    assert decision["from_state"] == "implement"
    assert decision["to_state"] == "done"
    ledger.close()


def test_braintrust_failure_is_fail_soft(tmp_path: Path) -> None:
    orchestrator, _workspace, ledger, _issue = _orchestrator(tmp_path, _ResourcePool())
    tracer = MagicMock()
    tracer.on_event.side_effect = RuntimeError("provider offline")
    projector = AsyncBraintrustProjector(tracer)
    orchestrator.tracer = projector

    orchestrator._trace_call("on_event", "linear-1", object())
    projector.close()
    run = ledger.allocate_run(
        project_slug="autosymph",
        issue_id="linear-1",
        issue_identifier="IMP-552",
        state="implement",
        runner="claude",
    )

    orchestrator.status_summary()
    assert isinstance(projector.failure, RuntimeError)
    assert run.attempt_number == 1
    assert any("Braintrust projection failed" in warning for warning in orchestrator._warnings)
    ledger.close()


def test_status_exposes_mobile_audit_ledger_state(tmp_path: Path) -> None:
    orchestrator, _workspace, ledger, _issue = _orchestrator(tmp_path, _ResourcePool())

    status = orchestrator.status_summary()

    assert status["reliability"] == {
        "mode": "observe",
        "ledger_path": str((tmp_path / "ledger.db").resolve()),
        "read_only": False,
        "schema_version": 1,
    }
    ledger.close()
