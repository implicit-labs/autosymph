"""CLI commands for authoring and validating Autosymph factories."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import tempfile
from typing import Any

import click
import yaml  # type: ignore[import-untyped]

from autosymph.config import RunnersConfig, TrackerConfig
from autosymph.contract_flow import ValueContract, run_contract_flow_sync
from autosymph.factory import FactoryRepository, findings_as_json
from autosymph.receipts import write_json_atomic
from autosymph.sample_flow import SAMPLE_PROFILES, run_sample_flow_sync
from autosymph.state_scripts import run_compiled_state_phase


def _transitions(values: tuple[str, ...]) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for value in values:
        if "=" not in value:
            raise click.BadParameter("transitions use SIGNAL=TARGET")
        signal, target = value.split("=", 1)
        if not signal or not target:
            raise click.BadParameter("transitions use non-empty SIGNAL=TARGET")
        parsed.append({"signal": signal, "target": target})
    return parsed


def _runner_config(default: str) -> RunnersConfig:
    return RunnersConfig(default=default, available=dict(SAMPLE_PROFILES))


@click.group("factory")
def factory() -> None:
    """Create, audit, compile, heal, and test state-package factories."""


@factory.command("init")
@click.argument("root", type=click.Path(path_type=Path, file_okay=False))
@click.argument("name")
@click.option("--initial", default="implement", show_default=True)
def initialize(root: Path, name: str, initial: str) -> None:
    """Initialize FACTORY.YAML and the states directory."""
    FactoryRepository(root).initialize(name, initial_state=initial)
    click.echo(str(root.resolve()))


@factory.command("create-state")
@click.argument("root", type=click.Path(path_type=Path, file_okay=False))
@click.argument("state_id")
@click.option("--kind", type=click.Choice(["agent", "gate", "terminal"]), required=True)
@click.option("--description", required=True)
@click.option("--runner", default="codex", show_default=True)
@click.option("--transition", "transition_values", multiple=True, help="SIGNAL=TARGET")
def create_state(
    root: Path,
    state_id: str,
    kind: str,
    description: str,
    runner: str,
    transition_values: tuple[str, ...],
) -> None:
    """Scaffold a state package with a skill, schemas, and validation script."""
    path = FactoryRepository(root).create_state(
        state_id,
        kind=kind,
        description=description,
        runner=runner,
        transitions=_transitions(transition_values),
    )
    click.echo(str(path))


@factory.command("update-state")
@click.argument("root", type=click.Path(path_type=Path, file_okay=False))
@click.argument("state_id")
@click.option("--expected-revision", type=int, required=True)
@click.option(
    "--patch-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
    help="JSON or YAML mapping containing fields to replace.",
)
def update_state(root: Path, state_id: str, expected_revision: int, patch_file: Path) -> None:
    """Revision-check and archive a state update before promotion."""
    value: Any = yaml.safe_load(patch_file.read_text())
    if not isinstance(value, dict):
        raise click.BadParameter("patch file must contain a mapping", param_hint="--patch-file")
    updated = FactoryRepository(root).update_state(
        state_id,
        value,
        expected_revision=expected_revision,
    )
    click.echo(json.dumps(updated, indent=2, sort_keys=True))


@factory.command("audit")
@click.argument("root", type=click.Path(path_type=Path, file_okay=False, exists=True))
@click.option("--scope", type=click.Choice(["all", "states", "transitions"]), default="all")
@click.option("--json-output", is_flag=True)
def audit(root: Path, scope: str, json_output: bool) -> None:
    """Fail closed on invalid state packages or graph transitions."""
    repository = FactoryRepository(root)
    if scope == "states":
        findings = repository.audit_states()
    elif scope == "transitions":
        findings = repository.audit_transitions()
    else:
        findings = repository.audit()
    if json_output:
        click.echo(findings_as_json(findings))
    elif not findings:
        click.echo("OK - factory audit passed")
    else:
        for finding in findings:
            location = f" [{finding.state}]" if finding.state else ""
            click.echo(f"{finding.severity.upper()} {finding.code}{location}: {finding.message}")
    if any(finding.severity == "error" for finding in findings):
        raise click.exceptions.Exit(1)


@factory.command("heal")
@click.argument("root", type=click.Path(path_type=Path, file_okay=False, exists=True))
@click.option("--apply", "apply_changes", is_flag=True)
def heal(root: Path, apply_changes: bool) -> None:
    """Promote only bounded mechanical repairs that improve a full audit."""
    result = FactoryRepository(root).self_heal(apply=apply_changes)
    click.echo(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    if apply_changes and (not result.promoted or result.after_errors > 0):
        raise click.exceptions.Exit(1)


@factory.command("compile")
@click.argument("root", type=click.Path(path_type=Path, file_okay=False, exists=True))
@click.option("--output", type=click.Path(path_type=Path, dir_okay=False), required=True)
@click.option("--project", required=True, help="Linear project name for the runtime config.")
@click.option("--runner", default="codex", show_default=True)
def compile_factory(root: Path, output: Path, project: str, runner: str) -> None:
    """Compile an audited factory into Autosymph's runtime WorkflowConfig."""
    workflow = FactoryRepository(root).compile_workflow(
        tracker=TrackerConfig(project=project),
        runners=_runner_config(runner),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(workflow.model_dump(exclude_none=True), sort_keys=False))
    click.echo(str(output.resolve()))


@factory.command("sample")
@click.option(
    "--runner",
    "runner_profile",
    type=click.Choice(list(SAMPLE_PROFILES)),
    default="codex",
    show_default=True,
)
def sample_factory(runner_profile: str) -> None:
    """Author, audit, compile, and execute a real three-state factory."""
    root = Path(tempfile.mkdtemp(prefix="autosymph-compiled-factory-"))
    definition = root / "definition"
    workspace = root / "workspace"
    repository = FactoryRepository(definition)
    repository.initialize("compiled-sample", initial_state="implement")
    repository.create_state(
        "done",
        kind="terminal",
        description="Record the accepted sample result.",
        runner=None,
    )
    repository.create_state(
        "blocked",
        kind="terminal",
        description="Record a failed or rejected sample result.",
        runner=None,
    )
    repository.create_state(
        "verify",
        kind="gate",
        description="Accept only receipt-bound deterministic proof.",
        runner=None,
        transitions=[
            {"signal": "approve", "target": "done"},
            {"signal": "fail", "target": "blocked"},
        ],
    )
    repository.create_state(
        "implement",
        kind="agent",
        description="Make the bounded sample.txt edit and inspect the exact bytes.",
        runner=runner_profile,
        transitions=[
            {"signal": "complete", "target": "verify"},
            {"signal": "fail", "target": "blocked"},
        ],
    )
    findings = repository.audit()
    workflow = repository.compile_workflow(
        tracker=TrackerConfig(project="local-compiled-sample", api_key="unused"),
        runners=_runner_config(runner_profile),
    )

    def state_script_check(target: Path) -> list[dict[str, Any]]:
        evidence = target / ".autosymph" / "evidence"
        input_path = evidence / "state-input.json"
        output_path = evidence / "state-output.json"
        write_json_atomic(input_path, {"objective": "edit sample.txt"})
        write_json_atomic(output_path, {"subject": "sample.txt"})
        phase = run_compiled_state_phase(
            "implement",
            workflow.states["implement"],
            "validate",
            input_path,
            output_path,
        )
        artifact = evidence / "state-script-validation.json"
        write_json_atomic(
            artifact,
            {
                "state": "implement",
                "phase": "validate",
                "outcome": phase.as_dict(),
            },
        )
        return [
            {
                "name": "state-script-validation",
                "passed": phase.success and bool(phase.results),
                "artifact_path": str(artifact.relative_to(target)),
                "artifact_sha256": sha256(artifact.read_bytes()).hexdigest(),
            }
        ]

    skill = (repository.state_path("implement") / "SKILL.md").read_text()
    result = run_sample_flow_sync(
        runner_profile=runner_profile,
        workspace=workspace,
        workflow=workflow,
        prompt_prefix=skill,
        extra_checks=state_script_check,
    )
    click.echo(
        json.dumps(
            {
                "success": result.success,
                "factory": str(definition),
                "factory_revision": repository.load_workflow()["revision"],
                "audit_findings": [finding.as_dict() for finding in findings],
                "flow": result.as_dict(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    if not result.success:
        raise click.ClickException("compiled factory sample did not reach done")


@factory.command("run")
@click.argument("root", type=click.Path(path_type=Path, file_okay=False, exists=True))
@click.option(
    "--workspace",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    required=True,
)
@click.option(
    "--contract",
    "contract_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
)
@click.option(
    "--runner",
    "runner_profile",
    type=click.Choice(list(SAMPLE_PROFILES)),
    default="codex",
    show_default=True,
)
def run_factory(
    root: Path,
    workspace: Path,
    contract_path: Path,
    runner_profile: str,
) -> None:
    """Run an audited factory against a clean real Git workspace contract."""
    try:
        repository = FactoryRepository(root)
        workflow = repository.compile_workflow(
            tracker=TrackerConfig(project="local-contract", api_key="unused"),
            runners=_runner_config(runner_profile),
        )
        contract = ValueContract.load(contract_path)
        result = run_contract_flow_sync(
            factory=repository,
            workflow=workflow,
            contract=contract,
            workspace=workspace,
            runner_profile=runner_profile,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    if not result.success:
        raise click.ClickException(
            "contract factory did not reach done: " + ", ".join(result.gate_reasons)
        )
