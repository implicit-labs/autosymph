from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autosymph.config import RunnerDefinition, RunnersConfig, TrackerConfig
from autosymph.factory import FactoryRepository
from autosymph.linear_client import LinearIssue
from autosymph.orchestrator import Orchestrator
from autosymph.state_machine import ClaimState, StateMachine


def test_dispatch_rejects_state_package_drift_before_marking_run(tmp_path: Path):
    repository = FactoryRepository(tmp_path / "factory")
    repository.initialize("dispatch-sample", initial_state="implement")
    repository.create_state(
        "done", kind="terminal", description="Record acceptance.", runner=None
    )
    repository.create_state(
        "implement",
        kind="agent",
        description="Perform bounded work.",
        runner="codex",
        transitions=[{"signal": "complete", "target": "done"}],
    )
    config = repository.compile_workflow(
        tracker=TrackerConfig(project="local", api_key="unused"),
        runners=RunnersConfig(
            default="codex",
            available={"codex": RunnerDefinition(type="codex")},
        ),
    )
    machine = StateMachine(config)
    tracked = machine.track_issue("issue-1", "LOCAL-1", "implement")
    assert machine.claim("issue-1") is True
    linear = MagicMock()
    workspace = MagicMock()
    workspace.repo = tmp_path
    orchestrator = Orchestrator(
        config=config,
        config_path=tmp_path / "compiled.yaml",
        linear=linear,
        state_machine=machine,
        workspace_mgr=workspace,
    )
    skill = repository.state_path("implement") / "SKILL.md"
    skill.write_text(skill.read_text() + "\nDrift after compile.\n")
    issue = LinearIssue("issue-1", "LOCAL-1", "Test", "Ready")

    with pytest.raises(ValueError, match="definition drift"):
        asyncio.run(
            orchestrator._spawn_agent(
                issue,
                "implement",
                config.states["implement"],
                "codex",
            )
        )

    assert tracked.claim == ClaimState.CLAIMED
