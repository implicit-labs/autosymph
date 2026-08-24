from __future__ import annotations

import json
from pathlib import Path

from autosymph.config import RunnerDefinition, RunnersConfig, StateConfig, TrackerConfig
from autosymph.factory import FactoryRepository
from autosymph.state_scripts import run_compiled_state_phase, validate_compiled_state


def _compiled_state(tmp_path: Path) -> tuple[FactoryRepository, StateConfig]:
    repository = FactoryRepository(tmp_path / "factory")
    repository.initialize("script-sample", initial_state="implement")
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
    workflow = repository.compile_workflow(
        tracker=TrackerConfig(project="local", api_key="unused"),
        runners=RunnersConfig(
            default="codex",
            available={"codex": RunnerDefinition(type="codex")},
        ),
    )
    return repository, workflow.states["implement"]


def test_compiled_state_binds_revision_hash_and_scripts(tmp_path: Path):
    repository, state = _compiled_state(tmp_path)

    resolved = validate_compiled_state("implement", state)

    assert resolved is not None
    assert resolved.root == repository.root
    assert state.state_revision == 1
    assert state.definition_sha256 == repository.definition_hash("implement")
    assert state.scripts[0].phase == "validate"


def test_runtime_executes_pinned_compiled_script(tmp_path: Path):
    _, state = _compiled_state(tmp_path)
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps({"input": True}))
    output_path.write_text(json.dumps({"output": True}))

    outcome = run_compiled_state_phase(
        "implement", state, "validate", input_path, output_path
    )

    assert outcome.success is True
    assert len(outcome.results) == 1
    assert json.loads(outcome.results[0].stdout) == {"ok": True}


def test_runtime_rejects_definition_drift_before_script_execution(tmp_path: Path):
    repository, state = _compiled_state(tmp_path)
    skill = repository.state_path("implement") / "SKILL.md"
    skill.write_text(skill.read_text() + "\nUncompiled mutation.\n")
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text("{}")
    output_path.write_text("{}")

    outcome = run_compiled_state_phase(
        "implement", state, "validate", input_path, output_path
    )

    assert outcome.success is False
    assert outcome.results == ()
    assert "definition drift" in str(outcome.error)


def test_state_without_compiled_scripts_is_a_noop(tmp_path: Path):
    outcome = run_compiled_state_phase(
        "implement",
        StateConfig(type="agent"),
        "validate",
        tmp_path / "input.json",
        tmp_path / "output.json",
    )

    assert outcome.success is True
    assert outcome.results == ()
