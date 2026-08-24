"""Contract-driven factory execution for real Git workspaces."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import fnmatch
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

import yaml  # type: ignore[import-untyped]

from autosymph.config import WorkflowConfig
from autosymph.factory import FactoryRepository
from autosymph.receipts import AttemptReceipt, evaluate_proof, write_json_atomic
from autosymph.runners import default_runner_registry
from autosymph.runners.base import AgentRunner
from autosymph.sample_flow import SAMPLE_PROFILES
from autosymph.state_machine import Signal, StateMachine
from autosymph.state_scripts import run_compiled_state_phase


_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class ContractCheck:
    name: str
    command: tuple[str, ...]
    timeout_seconds: int = 300

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ContractCheck:
        name = value.get("name")
        command = value.get("command")
        timeout = value.get("timeout_seconds", 300)
        if not isinstance(name, str) or not _SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError("contract check requires a safe identifier name")
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            raise ValueError(f"contract check {name!r} needs a non-empty command list")
        if not isinstance(timeout, int) or timeout < 1:
            raise ValueError(f"contract check {name!r} needs a positive timeout")
        return cls(name, tuple(command), timeout)


@dataclass(frozen=True)
class ValueContract:
    name: str
    objective: str
    allowed_paths: tuple[str, ...]
    checks: tuple[ContractCheck, ...]
    require_changes: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ValueContract:
        if value.get("schema_version") != 1:
            raise ValueError("contract schema_version must be 1")
        name = value.get("name")
        objective = value.get("objective")
        paths = value.get("allowed_paths")
        checks = value.get("checks")
        if not isinstance(name, str) or not _SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError("contract requires a safe identifier name")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("contract requires an objective")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(path, str) and path for path in paths
        ):
            raise ValueError("contract requires allowed_paths")
        if not isinstance(checks, list) or not checks:
            raise ValueError("contract requires at least one check")
        parsed_checks = tuple(ContractCheck.from_dict(check) for check in checks)
        if len({check.name for check in parsed_checks}) != len(parsed_checks):
            raise ValueError("contract check names must be unique")
        return cls(
            name=name,
            objective=objective.strip(),
            allowed_paths=tuple(paths),
            checks=parsed_checks,
            require_changes=bool(value.get("require_changes", True)),
        )

    @classmethod
    def load(cls, path: Path) -> ValueContract:
        value = yaml.safe_load(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("contract must contain a YAML mapping")
        return cls.from_dict(value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "objective": self.objective,
            "allowed_paths": list(self.allowed_paths),
            "require_changes": self.require_changes,
            "checks": [
                {
                    "name": check.name,
                    "command": list(check.command),
                    "timeout_seconds": check.timeout_seconds,
                }
                for check in self.checks
            ],
        }


@dataclass(frozen=True)
class ContractFlowResult:
    success: bool
    contract: str
    workspace: str
    runner_profile: str
    states: tuple[str, ...]
    changed_paths: tuple[str, ...]
    gate_reasons: tuple[str, ...]
    receipt_path: str
    proof_path: str
    raw_log_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "contract": self.contract,
            "workspace": self.workspace,
            "runner_profile": self.runner_profile,
            "states": list(self.states),
            "changed_paths": list(self.changed_paths),
            "gate_reasons": list(self.gate_reasons),
            "receipt_path": self.receipt_path,
            "proof_path": self.proof_path,
            "raw_log_path": self.raw_log_path,
        }


def _git(workspace: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _changed_paths(workspace: Path) -> tuple[str, ...]:
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "-z", "HEAD"],
        cwd=workspace,
        capture_output=True,
        check=True,
    ).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=workspace,
        capture_output=True,
        check=True,
    ).stdout
    paths = {
        value.decode(errors="surrogateescape")
        for value in (tracked + untracked).split(b"\0")
        if value
    }
    return tuple(sorted(path for path in paths if not path.startswith(".autosymph/")))


def _write_workspace_snapshot(workspace: Path, path: Path) -> tuple[str, ...]:
    changed = _changed_paths(workspace)
    files: list[dict[str, Any]] = []
    for relative in changed:
        candidate = workspace / relative
        if candidate.is_symlink():
            files.append({"path": relative, "kind": "symlink", "target": os.readlink(candidate)})
        elif candidate.is_file():
            files.append(
                {
                    "path": relative,
                    "kind": "file",
                    "sha256": sha256(candidate.read_bytes()).hexdigest(),
                    "size": candidate.stat().st_size,
                }
            )
        else:
            files.append({"path": relative, "kind": "deleted"})
    write_json_atomic(path, {"base_git_sha": _git(workspace, "rev-parse", "HEAD"), "files": files})
    return changed


def _path_allowed(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _run_check(workspace: Path, check: ContractCheck) -> dict[str, Any]:
    started = time.monotonic()
    environment = {**os.environ, "UV_CACHE_DIR": "/tmp/autosymph-contract-uv-cache"}
    try:
        completed = subprocess.run(
            list(check.command),
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=check.timeout_seconds,
            check=False,
        )
        return {
            "name": check.name,
            "command": list(check.command),
            "exit_code": completed.returncode,
            "passed": completed.returncode == 0,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": completed.stdout[-100_000:],
            "stderr": completed.stderr[-100_000:],
        }
    except subprocess.TimeoutExpired as error:
        return {
            "name": check.name,
            "command": list(check.command),
            "exit_code": 124,
            "passed": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": str(error.stdout or "")[-100_000:],
            "stderr": str(error.stderr or "check timed out")[-100_000:],
        }
    except OSError as error:
        return {
            "name": check.name,
            "command": list(check.command),
            "exit_code": -1,
            "passed": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": "",
            "stderr": str(error),
        }


def _prompt(contract: ValueContract, skill: str) -> str:
    rendered_checks = "\n".join(
        f"- {check.name}: {json.dumps(list(check.command))}" for check in contract.checks
    )
    rendered_paths = "\n".join(f"- {path}" for path in contract.allowed_paths)
    return (
        f"{skill.rstrip()}\n\n"
        f"## Objective\n\n{contract.objective}\n\n"
        f"## Allowed change scope\n\n{rendered_paths}\n\n"
        f"## Acceptance commands\n\n{rendered_checks}\n\n"
        "Work directly in the current repository. Do not commit. Do not modify files outside the "
        "allowed scope. Run the acceptance commands and inspect the final diff before finishing."
    )


async def run_contract_flow(
    *,
    factory: FactoryRepository,
    workflow: WorkflowConfig,
    contract: ValueContract,
    workspace: Path,
    runner_profile: str,
    runner_registry: dict[str, AgentRunner] | None = None,
) -> ContractFlowResult:
    workspace = workspace.resolve()
    try:
        is_worktree = _git(workspace, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"workspace is not a Git repository: {workspace}") from error
    if is_worktree != "true":
        raise ValueError(f"workspace is not a Git repository: {workspace}")
    existing = _changed_paths(workspace)
    if existing:
        raise ValueError(f"workspace must start clean; found changes: {', '.join(existing)}")
    if runner_profile not in SAMPLE_PROFILES:
        raise ValueError(f"unknown runner profile {runner_profile!r}")
    profile = SAMPLE_PROFILES[runner_profile]
    machine = StateMachine(workflow)
    tracked = machine.track_issue(contract.name, contract.name.upper(), "implement")
    states = [tracked.workflow_state]
    evidence = workspace / ".autosymph" / "evidence" / contract.name
    evidence.mkdir(parents=True, exist_ok=True)
    raw_log = evidence / "implement-run1.ndjson"
    prompt = _prompt(
        contract,
        (factory.state_path("implement") / "SKILL.md").read_text(),
    )
    adapter = (runner_registry or default_runner_registry())[profile.type]
    runner_config = {
        **profile.model_dump(),
        "identifier": contract.name.upper(),
        "workflow_state": "implement",
        "prompt_path": str(factory.state_path("implement") / "SKILL.md"),
        "runner": runner_profile,
    }

    def on_raw_line(line: str) -> None:
        with raw_log.open("a") as stream:
            stream.write(line + "\n")

    base_sha = _git(workspace, "rev-parse", "HEAD")
    result = await adapter.run(
        prompt=prompt,
        workspace_path=str(workspace),
        config=runner_config,
        on_raw_line=on_raw_line,
    )
    snapshot = evidence / "workspace-subject.json"
    changed = _write_workspace_snapshot(workspace, snapshot)
    receipt = AttemptReceipt.from_run(
        attempt_id=f"{contract.name}-implement-1",
        state="implement",
        runner_profile=runner_profile,
        adapter_type=profile.type,
        model=profile.model,
        auth_mode=profile.auth_mode,
        prompt=prompt,
        base_git_sha=base_sha,
        resulting_git_sha=_git(workspace, "rev-parse", "HEAD"),
        workspace_path=workspace,
        subject_path=snapshot,
        raw_log_path=raw_log,
        result=result,
    )
    receipt_path = evidence / "attempt-receipt.json"
    write_json_atomic(receipt_path, receipt.as_dict())
    target = machine.next_state(
        "implement", Signal.COMPLETE if result.success else Signal.FAIL, contract.name
    )
    if target != "verify":
        states.append(target or "blocked")
        return ContractFlowResult(
            False,
            contract.name,
            str(workspace),
            runner_profile,
            tuple(states),
            changed,
            (result.error or "IMPLEMENTATION_FAILED",),
            str(receipt_path),
            "",
            str(raw_log),
        )
    tracked.workflow_state = "verify"
    states.append("verify")

    state_input = evidence / "state-input.json"
    state_output = evidence / "state-output.json"
    write_json_atomic(state_input, contract.as_dict())
    write_json_atomic(
        state_output,
        {"runner_success": result.success, "changed_paths": list(changed)},
    )
    state_validation = run_compiled_state_phase(
        "implement",
        workflow.states["implement"],
        "validate",
        state_input,
        state_output,
    )
    script_artifact = evidence / "state-script-validation.json"
    write_json_atomic(script_artifact, state_validation.as_dict())

    scope_passed = (not contract.require_changes or bool(changed)) and all(
        _path_allowed(path, contract.allowed_paths) for path in changed
    )
    scope_artifact = evidence / "scope-check.json"
    write_json_atomic(
        scope_artifact,
        {
            "passed": scope_passed,
            "changed_paths": list(changed),
            "allowed_paths": list(contract.allowed_paths),
        },
    )
    proof_checks: list[dict[str, Any]] = [
        {
            "name": "change-scope",
            "passed": scope_passed,
            "artifact_path": str(scope_artifact.relative_to(workspace)),
            "artifact_sha256": sha256(scope_artifact.read_bytes()).hexdigest(),
        },
        {
            "name": "state-script-validation",
            "passed": state_validation.success and bool(state_validation.results),
            "artifact_path": str(script_artifact.relative_to(workspace)),
            "artifact_sha256": sha256(script_artifact.read_bytes()).hexdigest(),
        },
    ]
    for check in contract.checks:
        check_result = await asyncio.to_thread(_run_check, workspace, check)
        artifact = evidence / f"check-{check.name}.json"
        write_json_atomic(artifact, check_result)
        proof_checks.append(
            {
                "name": check.name,
                "passed": check_result["passed"],
                "artifact_path": str(artifact.relative_to(workspace)),
                "artifact_sha256": sha256(artifact.read_bytes()).hexdigest(),
            }
        )

    final_snapshot = evidence / "workspace-final.json"
    _write_workspace_snapshot(workspace, final_snapshot)
    stable = json.loads(snapshot.read_text()) == json.loads(final_snapshot.read_text())
    proof_checks.append(
        {
            "name": "workspace-stable",
            "passed": stable,
            "artifact_path": str(final_snapshot.relative_to(workspace)),
            "artifact_sha256": sha256(final_snapshot.read_bytes()).hexdigest(),
        }
    )
    proof_path = evidence / "proof.json"
    write_json_atomic(
        proof_path,
        {
            "schema_version": 1,
            "attempt_id": receipt.attempt_id,
            "validator_id": f"deterministic:contract:{contract.name}:v1",
            "subject_sha256": receipt.subject_sha256,
            "checks": proof_checks,
        },
    )
    gate = evaluate_proof(
        receipt,
        proof_path,
        workspace=workspace,
        required_checks={str(check["name"]) for check in proof_checks},
    )
    final = machine.next_state(
        "verify", Signal.APPROVE if gate.allowed else Signal.FAIL, contract.name
    )
    states.append(final or "blocked")
    flow_result = ContractFlowResult(
        gate.allowed and final == "done",
        contract.name,
        str(workspace),
        runner_profile,
        tuple(states),
        changed,
        gate.reason_codes,
        str(receipt_path),
        str(proof_path),
        str(raw_log),
    )
    write_json_atomic(evidence / "contract-flow-result.json", flow_result.as_dict())
    return flow_result


def run_contract_flow_sync(**kwargs: Any) -> ContractFlowResult:
    return asyncio.run(run_contract_flow(**kwargs))
