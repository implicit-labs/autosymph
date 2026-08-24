from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest
import yaml

from autosymph.config import RunnerDefinition, RunnersConfig, TrackerConfig
from autosymph.factory import FactoryRepository
from autosymph.state_machine import Signal, StateMachine


@pytest.fixture
def factory_repository(tmp_path: Path) -> FactoryRepository:
    repository = FactoryRepository(tmp_path / "factory")
    repository.initialize("sample-factory", initial_state="implement")
    repository.create_state(
        "done", kind="terminal", description="Record acceptance.", runner=None
    )
    repository.create_state(
        "blocked", kind="terminal", description="Record rejection.", runner=None
    )
    repository.create_state(
        "verify",
        kind="gate",
        description="Verify deterministic evidence.",
        runner=None,
        transitions=[
            {"signal": "approve", "target": "done"},
            {"signal": "fail", "target": "blocked"},
        ],
    )
    repository.create_state(
        "implement",
        kind="agent",
        description="Perform bounded work.",
        runner="codex",
        transitions=[
            {"signal": "complete", "target": "verify"},
            {"signal": "fail", "target": "blocked"},
        ],
    )
    return repository


def _runners() -> RunnersConfig:
    return RunnersConfig(
        default="codex",
        available={"codex": RunnerDefinition(type="codex", sandbox="workspace-write")},
    )


def test_valid_factory_scaffolds_and_audits(factory_repository: FactoryRepository):
    state = factory_repository.state_path("implement")

    assert factory_repository.audit() == []
    assert (state / "SKILL.md").is_file()
    assert (state / "schemas" / "input.json").is_file()
    assert (state / "schemas" / "output.json").is_file()
    assert (state / "scripts" / "validate.mjs").is_file()


def test_update_is_revision_checked_and_archived(factory_repository: FactoryRepository):
    updated = factory_repository.update_state(
        "implement", {"description": "Updated bounded work."}, expected_revision=1
    )

    assert updated["revision"] == 2
    history = factory_repository.root / ".factory" / "definitions" / "implement"
    assert (history / "v1.yaml").is_file()
    assert (history / "v1" / "SKILL.md").is_file()
    with pytest.raises(RuntimeError, match="stale state revision"):
        factory_repository.update_state(
            "implement", {"description": "Stale update."}, expected_revision=1
        )


def test_transition_audit_rejects_missing_target(factory_repository: FactoryRepository):
    manifest = factory_repository.state_path("implement") / "state.yaml"
    value = yaml.safe_load(manifest.read_text())
    value["transitions"] = [{"signal": "complete", "target": "missing"}]
    manifest.write_text(yaml.safe_dump(value, sort_keys=False))

    codes = {finding.code for finding in factory_repository.audit_transitions()}

    assert "MISSING_TARGET" in codes
    assert "NO_PATH_TO_TERMINAL" in codes


def test_transition_audit_rejects_unknown_signal(factory_repository: FactoryRepository):
    manifest = factory_repository.state_path("implement") / "state.yaml"
    value = yaml.safe_load(manifest.read_text())
    value["transitions"][0]["signal"] = "model-says-trust-me"
    manifest.write_text(yaml.safe_dump(value, sort_keys=False))

    codes = {finding.code for finding in factory_repository.audit_transitions()}

    assert "UNKNOWN_SIGNAL" in codes


def test_state_audit_rejects_symlinked_definition_parent(
    factory_repository: FactoryRepository,
):
    state = factory_repository.state_path("implement")
    (state / "script-alias").symlink_to("scripts", target_is_directory=True)
    manifest = state / "state.yaml"
    value = yaml.safe_load(manifest.read_text())
    value["scripts"].append(
        {
            "phase": "recover",
            "path": "script-alias/validate.mjs",
            "sha256": value["scripts"][0]["sha256"],
            "timeout_seconds": 10,
        }
    )
    manifest.write_text(yaml.safe_dump(value, sort_keys=False))

    codes = {finding.code for finding in factory_repository.audit_states()}

    assert "SYMLINKED_DEFINITION" in codes


def test_cycle_requires_exit_and_retry_budget(factory_repository: FactoryRepository):
    verify_manifest = factory_repository.state_path("verify") / "state.yaml"
    verify = yaml.safe_load(verify_manifest.read_text())
    verify["transitions"] = [
        {"signal": "approve", "target": "done"},
        {"signal": "fail", "target": "implement"},
    ]
    verify["retry"]["max_attempts"] = 0
    verify_manifest.write_text(yaml.safe_dump(verify, sort_keys=False))

    codes = {finding.code for finding in factory_repository.audit_transitions()}

    assert "UNBOUNDED_CYCLE" in codes


def test_compile_drives_existing_state_machine(factory_repository: FactoryRepository):
    workflow = factory_repository.compile_workflow(
        tracker=TrackerConfig(project="local", api_key="unused"), runners=_runners()
    )
    machine = StateMachine(workflow)
    machine.track_issue("issue-1", "LOCAL-1", "implement")

    assert machine.next_state("implement", Signal.COMPLETE, "issue-1") == "verify"
    assert machine.next_state("verify", Signal.APPROVE, "issue-1") == "done"
    assert workflow.states["implement"].prompt == str(
        (factory_repository.state_path("implement") / "SKILL.md").resolve()
    )


def test_compile_rejects_unavailable_profile(factory_repository: FactoryRepository):
    with pytest.raises(ValueError, match="unavailable runner profiles: codex"):
        factory_repository.compile_workflow(
            tracker=TrackerConfig(project="local"),
            runners=RunnersConfig(
                default="claude",
                available={"claude": RunnerDefinition(type="claude")},
            ),
        )


def test_pinned_state_script_executes(factory_repository: FactoryRepository, tmp_path: Path):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps({"input": True}))
    output_path.write_text(json.dumps({"output": True}))

    results = factory_repository.run_scripts(
        "implement", "validate", input_path, output_path
    )

    assert len(results) == 1
    assert results[0].success is True
    assert json.loads(results[0].stdout) == {"ok": True}


def test_self_heal_only_promotes_hash_pinned_permission_repair(
    factory_repository: FactoryRepository,
):
    state = factory_repository.state_path("implement")
    script = state / "scripts" / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    manifest = state / "state.yaml"
    value = yaml.safe_load(manifest.read_text())
    value["scripts"].append(
        {
            "phase": "validate",
            "path": "scripts/check.sh",
            "sha256": sha256(script.read_bytes()).hexdigest(),
            "timeout_seconds": 10,
        }
    )
    manifest.write_text(yaml.safe_dump(value, sort_keys=False))

    dry_run = factory_repository.self_heal()
    promoted = factory_repository.self_heal(apply=True)

    assert dry_run.promoted is False
    assert any(action.automatic for action in dry_run.actions)
    assert promoted.promoted is True
    assert promoted.after_errors == 0
    assert script.stat().st_mode & 0o100


def test_self_heal_rejects_unpinned_script(factory_repository: FactoryRepository):
    state = factory_repository.state_path("implement")
    script = state / "scripts" / "untrusted.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    manifest = state / "state.yaml"
    value = yaml.safe_load(manifest.read_text())
    value["scripts"].append(
        {
            "phase": "recover",
            "path": "scripts/untrusted.sh",
            "sha256": "0" * 64,
            "timeout_seconds": 10,
        }
    )
    manifest.write_text(yaml.safe_dump(value, sort_keys=False))

    result = factory_repository.self_heal(apply=True)

    assert result.promoted is False
    assert "SCRIPT_HASH_MISMATCH" in {finding.code for finding in result.findings}
