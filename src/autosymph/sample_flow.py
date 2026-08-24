"""A real, local autosymph sample flow with model execution and proof gating."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from autosymph.config import (
    RunnerDefinition,
    RunnersConfig,
    StateConfig,
    StateTransitions,
    TrackerConfig,
    WorkflowConfig,
)
from autosymph.receipts import AttemptReceipt, evaluate_proof, write_json_atomic
from autosymph.runners import default_runner_registry
from autosymph.runners.base import AgentRunner
from autosymph.state_machine import Signal, StateMachine


SAMPLE_PROFILES: dict[str, RunnerDefinition] = {
    "claude-code": RunnerDefinition(
        type="claude",
        model="sonnet",
        auth_mode="subscription",
        permission_mode="acceptEdits",
    ),
    "codex": RunnerDefinition(
        type="codex",
        auth_mode="subscription",
        sandbox="workspace-write",
    ),
    "omp-subscription": RunnerDefinition(
        type="omp",
        model="anthropic/claude-sonnet",
        auth_mode="stored_profile",
        profile="autosymph-subscription",
        permission_mode="write",
        max_time_seconds=600,
    ),
    "omp-claude-api": RunnerDefinition(
        type="omp",
        model="anthropic/claude-sonnet",
        auth_mode="environment",
        auth_env="ANTHROPIC_API_KEY",
        profile="autosymph-claude-api",
        permission_mode="write",
        max_time_seconds=600,
    ),
}


@dataclass(frozen=True)
class SampleFlowResult:
    success: bool
    workspace: str
    runner_profile: str
    states: tuple[str, ...]
    receipt_path: str
    proof_path: str
    raw_log_path: str
    gate_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "workspace": self.workspace,
            "runner_profile": self.runner_profile,
            "states": list(self.states),
            "receipt_path": self.receipt_path,
            "proof_path": self.proof_path,
            "raw_log_path": self.raw_log_path,
            "gate_reasons": list(self.gate_reasons),
        }


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _initialize_workspace(workspace: Path) -> str:
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.email", "autosymph-sample@localhost")
    _git(workspace, "config", "user.name", "Autosymph Sample")
    (workspace / "README.md").write_text("# Autosymph sample\n")
    (workspace / "sample.txt").write_text("before\n")
    _git(workspace, "add", "README.md", "sample.txt")
    _git(workspace, "commit", "-m", "sample baseline")
    return _git(workspace, "rev-parse", "HEAD")


def _workflow(profile_name: str, profile: RunnerDefinition) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(project="local-sample", api_key="unused"),
        runners=RunnersConfig(default=profile_name, available={profile_name: profile}),
        states={
            "implement": StateConfig(
                type="agent",
                prompt="inline",
                runner=profile_name,
                transitions=StateTransitions.model_validate(
                    {"complete": "verify", "fail": "blocked"}
                ),
            ),
            "verify": StateConfig(
                type="gate",
                transitions=StateTransitions.model_validate(
                    {"approve": "done", "fail": "blocked"}
                ),
            ),
            "done": StateConfig(type="terminal"),
            "blocked": StateConfig(type="terminal"),
        },
    )


async def run_sample_flow(
    runner_profile: str = "claude-code",
    workspace: Path | None = None,
    runner_registry: dict[str, AgentRunner] | None = None,
) -> SampleFlowResult:
    if runner_profile not in SAMPLE_PROFILES:
        raise ValueError(
            f"unknown sample runner {runner_profile!r}; choose from {sorted(SAMPLE_PROFILES)}"
        )
    target = (workspace or Path(tempfile.mkdtemp(prefix="autosymph-sample-"))).resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"sample workspace must be empty: {target}")
    base_sha = _initialize_workspace(target)
    profile = SAMPLE_PROFILES[runner_profile]
    workflow = _workflow(runner_profile, profile)
    state_machine = StateMachine(workflow)
    tracked = state_machine.track_issue("local-sample", "LOCAL-SAMPLE", "implement")
    states = [tracked.workflow_state]

    prompt = (
        "This is an autosymph sample implementation state. Edit sample.txt so its entire "
        "contents are exactly: autosymph sample complete followed by one newline. "
        "Do not modify any other file. Do not commit. Inspect the result before finishing."
    )
    evidence_dir = target / ".autosymph" / "evidence"
    raw_log = evidence_dir / "implement-run1.ndjson"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    adapter = (runner_registry or default_runner_registry())[profile.type]
    runner_config = {
        **profile.model_dump(),
        "identifier": "LOCAL-SAMPLE",
        "workflow_state": "implement",
        "prompt_path": "inline:sample-flow",
        "runner": runner_profile,
    }

    def on_raw_line(line: str) -> None:
        with raw_log.open("a") as stream:
            stream.write(line + "\n")

    run_result = await adapter.run(
        prompt=prompt,
        workspace_path=str(target),
        config=runner_config,
        on_raw_line=on_raw_line,
    )
    result_sha = _git(target, "rev-parse", "HEAD")
    subject = target / "sample.txt"
    receipt = AttemptReceipt.from_run(
        attempt_id="local-sample-implement-1",
        state="implement",
        runner_profile=runner_profile,
        adapter_type=profile.type,
        model=profile.model,
        auth_mode=profile.auth_mode,
        prompt=prompt,
        base_git_sha=base_sha,
        resulting_git_sha=result_sha,
        workspace_path=target,
        subject_path=subject,
        raw_log_path=raw_log,
        result=run_result,
    )
    receipt_path = evidence_dir / "attempt-receipt.json"
    write_json_atomic(receipt_path, receipt.as_dict())

    implement_target = state_machine.next_state(
        "implement", Signal.COMPLETE if run_result.success else Signal.FAIL, "local-sample"
    )
    if implement_target != "verify":
        states.append(implement_target or "blocked")
        return SampleFlowResult(
            False, str(target), runner_profile, tuple(states), str(receipt_path), "",
            str(raw_log), (run_result.error or "IMPLEMENTATION_FAILED",),
        )
    tracked.workflow_state = "verify"
    states.append("verify")

    content_ok = subject.is_file() and subject.read_text() == "autosymph sample complete\n"
    changed_files = set(_git(target, "diff", "--name-only").splitlines())
    scope_ok = changed_files == {"sample.txt"}
    proof_path = evidence_dir / "proof.json"
    proof = {
        "schema_version": 1,
        "attempt_id": receipt.attempt_id,
        "validator_id": "deterministic:sample-contract-v1",
        "subject_sha256": receipt.subject_sha256,
        "checks": [
            {
                "name": "exact-content",
                "passed": content_ok,
                "artifact_path": "sample.txt",
                "artifact_sha256": sha256(subject.read_bytes()).hexdigest() if subject.is_file() else "",
            },
            {
                "name": "change-scope",
                "passed": scope_ok,
                "artifact_path": "sample.txt",
                "artifact_sha256": sha256(subject.read_bytes()).hexdigest() if subject.is_file() else "",
            },
        ],
    }
    write_json_atomic(proof_path, proof)
    gate = evaluate_proof(
        receipt,
        proof_path,
        workspace=target,
        required_checks={"exact-content", "change-scope"},
    )
    final_target = state_machine.next_state(
        "verify", Signal.APPROVE if gate.allowed else Signal.FAIL, "local-sample"
    )
    states.append(final_target or "blocked")
    result = SampleFlowResult(
        success=gate.allowed and final_target == "done",
        workspace=str(target),
        runner_profile=runner_profile,
        states=tuple(states),
        receipt_path=str(receipt_path),
        proof_path=str(proof_path),
        raw_log_path=str(raw_log),
        gate_reasons=gate.reason_codes,
    )
    write_json_atomic(evidence_dir / "sample-flow-result.json", result.as_dict())
    return result


def run_sample_flow_sync(
    runner_profile: str = "claude-code",
    workspace: Path | None = None,
) -> SampleFlowResult:
    return asyncio.run(run_sample_flow(runner_profile=runner_profile, workspace=workspace))
