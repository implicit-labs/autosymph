from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess

import pytest

from autosymph.config import RunnersConfig, TrackerConfig
from autosymph.contract_flow import ContractCheck, ValueContract, run_contract_flow
from autosymph.factory import FactoryRepository
from autosymph.runners.base import AgentRunner, RunResult
from autosymph.sample_flow import SAMPLE_PROFILES


class EditingRunner(AgentRunner):
    def __init__(self, path: str, content: str = "fixed\n"):
        self.path = path
        self.content = content

    def parse_event(self, line):
        del line
        return None

    async def run(self, prompt, workspace_path, config, session_id=None, on_event=None, on_raw_line=None):
        del prompt, config, session_id, on_event
        Path(workspace_path, self.path).write_text(self.content)
        if on_raw_line:
            on_raw_line('{"type":"result","success":true}')
        return RunResult(success=True, exit_code=0, session_id="editing-runner")

    async def kill(self, pid):
        del pid


def _git(workspace: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=workspace, check=True, capture_output=True)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.email", "contract-test@localhost")
    _git(workspace, "config", "user.name", "Contract Test")
    (workspace / "value.txt").write_text("before\n")
    _git(workspace, "add", "value.txt")
    _git(workspace, "commit", "-m", "baseline")
    return workspace


def _factory(tmp_path: Path) -> tuple[FactoryRepository, object]:
    repository = FactoryRepository(tmp_path / "factory")
    repository.initialize("value-factory", initial_state="implement")
    repository.create_state("done", kind="terminal", description="Accept.", runner=None)
    repository.create_state("blocked", kind="terminal", description="Reject.", runner=None)
    repository.create_state(
        "verify",
        kind="gate",
        description="Verify proof.",
        runner=None,
        transitions=[
            {"signal": "approve", "target": "done"},
            {"signal": "fail", "target": "blocked"},
        ],
    )
    repository.create_state(
        "implement",
        kind="agent",
        description="Make the contracted change.",
        runner="claude-code",
        transitions=[
            {"signal": "complete", "target": "verify"},
            {"signal": "fail", "target": "blocked"},
        ],
    )
    workflow = repository.compile_workflow(
        tracker=TrackerConfig(project="local", api_key="unused"),
        runners=RunnersConfig(
            default="claude-code",
            available={"claude-code": SAMPLE_PROFILES["claude-code"]},
        ),
    )
    return repository, workflow


def _contract() -> ValueContract:
    return ValueContract(
        name="real-value",
        objective="Change value.txt to the accepted content.",
        allowed_paths=("value.txt",),
        checks=(ContractCheck("content", ("grep", "-qx", "fixed", "value.txt"), 30),),
    )


def test_contract_flow_reaches_done_with_real_command(tmp_path: Path):
    workspace = _workspace(tmp_path)
    repository, workflow = _factory(tmp_path)

    result = asyncio.run(
        run_contract_flow(
            factory=repository,
            workflow=workflow,
            contract=_contract(),
            workspace=workspace,
            runner_profile="claude-code",
            runner_registry={"claude": EditingRunner("value.txt")},
        )
    )

    assert result.success is True
    assert result.states == ("implement", "verify", "done")
    assert result.changed_paths == ("value.txt",)
    assert Path(result.receipt_path).is_file()
    assert Path(result.proof_path).is_file()


def test_contract_flow_blocks_out_of_scope_change(tmp_path: Path):
    workspace = _workspace(tmp_path)
    repository, workflow = _factory(tmp_path)

    result = asyncio.run(
        run_contract_flow(
            factory=repository,
            workflow=workflow,
            contract=_contract(),
            workspace=workspace,
            runner_profile="claude-code",
            runner_registry={"claude": EditingRunner("outside.txt")},
        )
    )

    assert result.success is False
    assert result.states == ("implement", "verify", "blocked")
    assert "CHECK_FAILED:change-scope" in result.gate_reasons


def test_contract_flow_blocks_failed_acceptance_command(tmp_path: Path):
    workspace = _workspace(tmp_path)
    repository, workflow = _factory(tmp_path)
    contract = ValueContract(
        name="failed-check",
        objective="Change value.txt.",
        allowed_paths=("value.txt",),
        checks=(ContractCheck("impossible", ("grep", "-qx", "nope", "value.txt"), 30),),
    )

    result = asyncio.run(
        run_contract_flow(
            factory=repository,
            workflow=workflow,
            contract=contract,
            workspace=workspace,
            runner_profile="claude-code",
            runner_registry={"claude": EditingRunner("value.txt")},
        )
    )

    assert result.success is False
    assert result.states[-1] == "blocked"
    assert "CHECK_FAILED:impossible" in result.gate_reasons


@pytest.mark.parametrize("unsafe_name", ["../escape", "nested/name", "has space", ""])
def test_contract_rejects_unsafe_evidence_names(unsafe_name: str):
    value = _contract().as_dict()
    value["name"] = unsafe_name

    with pytest.raises(ValueError, match="safe identifier"):
        ValueContract.from_dict(value)


def test_contract_flow_rejects_non_git_workspace(tmp_path: Path):
    workspace = tmp_path / "not-a-repository"
    workspace.mkdir()
    repository, workflow = _factory(tmp_path)

    with pytest.raises(ValueError, match="not a Git repository"):
        asyncio.run(
            run_contract_flow(
                factory=repository,
                workflow=workflow,
                contract=_contract(),
                workspace=workspace,
                runner_profile="claude-code",
                runner_registry={"claude": EditingRunner("value.txt")},
            )
        )
