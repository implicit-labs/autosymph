"""Execute scripts from a hash-bound, compiled factory state package."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autosymph.config import StateConfig
from autosymph.factory import FactoryRepository, ScriptResult


@dataclass(frozen=True)
class PhaseOutcome:
    phase: str
    success: bool
    results: tuple[ScriptResult, ...]
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "success": self.success,
            "error": self.error,
            "results": [result.as_dict() for result in self.results],
        }


def validate_compiled_state(state_id: str, state: StateConfig) -> FactoryRepository | None:
    """Fail if authored bytes drifted after the last successful compile."""
    if not state.factory_root:
        return None
    if not state.state_revision or not state.definition_sha256:
        raise ValueError(f"compiled state {state_id!r} is missing revision or definition hash")
    repository = FactoryRepository(state.factory_root)
    manifest = repository.load_state(state_id)
    if manifest.get("revision") != state.state_revision:
        raise ValueError(
            f"compiled state {state_id!r} revision drift: "
            f"expected {state.state_revision}, found {manifest.get('revision')}"
        )
    actual_hash = repository.definition_hash(state_id)
    if actual_hash != state.definition_sha256:
        raise ValueError(
            f"compiled state {state_id!r} definition drift: "
            f"expected {state.definition_sha256}, found {actual_hash}"
        )
    return repository


def run_compiled_state_phase(
    state_id: str,
    state: StateConfig,
    phase: str,
    input_path: Path,
    output_path: Path,
) -> PhaseOutcome:
    relevant = [script for script in state.scripts if script.phase == phase]
    if not relevant:
        return PhaseOutcome(phase, True, ())
    try:
        repository = validate_compiled_state(state_id, state)
        if repository is None:
            raise ValueError(f"state {state_id!r} has scripts but no compiled factory root")
        results = tuple(repository.run_scripts(state_id, phase, input_path, output_path))
    except Exception as caught:
        return PhaseOutcome(phase, False, (), f"{caught.__class__.__name__}: {caught}")
    failed = [result for result in results if not result.success]
    failure_message = None
    if failed:
        failure_message = "; ".join(
            f"{result.path} exited {result.exit_code}: {result.stderr.strip()}" for result in failed
        )
    return PhaseOutcome(
        phase,
        not failed and len(results) == len(relevant),
        results,
        failure_message,
    )
