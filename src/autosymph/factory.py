"""Repository-native factory state packages for Autosymph.

The authored definition lives beside the product code. Each state owns its
skill, schemas, and pinned shell or Node scripts. This module validates that
source tree and compiles one last-known-good ``WorkflowConfig`` for the runtime.
Models may propose edits, but only this deterministic layer promotes them.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Iterator, Literal

import yaml  # type: ignore[import-untyped]

from autosymph.config import (
    PromptsConfig,
    RunnersConfig,
    StateConfig,
    StateTransitions,
    TrackerConfig,
    WorkflowConfig,
)
from autosymph.state_machine import Signal


STATE_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
STATE_KINDS = {"agent", "gate", "terminal"}
SCRIPT_PHASES = {"enter", "run", "validate", "exit", "recover", "guard", "effect"}
SEVERITY_ORDER = {"error": 3, "warning": 2, "info": 1}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    state: str | None = None
    transition: str | None = None
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScriptResult:
    state: str
    phase: str
    path: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepairAction:
    code: str
    path: str
    automatic: bool
    description: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HealResult:
    applied: bool
    promoted: bool
    before_errors: int
    after_errors: int
    actions: tuple[RepairAction, ...]
    findings: tuple[Finding, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "promoted": self.promoted,
            "before_errors": self.before_errors,
            "after_errors": self.after_errors,
            "actions": [action.as_dict() for action in self.actions],
            "findings": [finding.as_dict() for finding in self.findings],
        }


def _read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def _write_yaml_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(value, sort_keys=False, width=100)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _has_symlink_component(path: Path, root: Path) -> bool:
    """Detect symlinks before resolve() erases how a definition was reached."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _error_count(findings: list[Finding] | tuple[Finding, ...]) -> int:
    return sum(finding.severity == "error" for finding in findings)


def _state_definition_hash(directory: Path) -> str:
    digest = sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative = path.relative_to(directory).as_posix()
        digest.update(relative.encode())
        digest.update(str(stat.S_IMODE(path.stat().st_mode)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class FactoryRepository:
    """Create, update, audit, compile, and mechanically heal one factory."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.workflow_path = self.root / "factory.yaml"
        self.states_root = self.root / "states"

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        lock = self.root / ".factory" / f"{name}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def initialize(self, name: str, *, initial_state: str = "implement") -> None:
        if self.workflow_path.exists():
            raise FileExistsError(f"factory already exists at {self.root}")
        if not STATE_ID.fullmatch(name):
            raise ValueError("factory name must be lowercase kebab-case")
        if not STATE_ID.fullmatch(initial_state):
            raise ValueError("initial state must be lowercase kebab-case")
        self.states_root.mkdir(parents=True, exist_ok=True)
        _write_yaml_atomic(
            self.workflow_path,
            {
                "schema_version": 1,
                "name": name,
                "revision": 1,
                "initial_state": initial_state,
                "terminal_states": [],
                "policy": {
                    "definition_updates": "revision-checked",
                    "transition_authority": "state-machine-only",
                    "script_integrity": "sha256-pinned",
                    "max_repair_attempts": 3,
                },
            },
        )

    def state_path(self, state_id: str) -> Path:
        if not STATE_ID.fullmatch(state_id):
            raise ValueError(f"invalid state id {state_id!r}")
        return self.states_root / state_id

    def load_workflow(self) -> dict[str, Any]:
        return _read_yaml(self.workflow_path)

    def load_state(self, state_id: str) -> dict[str, Any]:
        return _read_yaml(self.state_path(state_id) / "state.yaml")

    def definition_hash(self, state_id: str) -> str:
        return _state_definition_hash(self.state_path(state_id))

    def iter_states(self) -> Iterator[tuple[str, Path, dict[str, Any]]]:
        if not self.states_root.exists():
            return
        for directory in sorted(path for path in self.states_root.iterdir() if path.is_dir()):
            manifest = directory / "state.yaml"
            if manifest.exists():
                yield directory.name, directory, _read_yaml(manifest)

    def create_state(
        self,
        state_id: str,
        *,
        kind: str,
        description: str,
        transitions: list[dict[str, Any]] | None = None,
        runner: str | None = "codex",
    ) -> Path:
        if kind not in STATE_KINDS:
            raise ValueError(f"kind must be one of {sorted(STATE_KINDS)}")
        directory = self.state_path(state_id)
        if directory.exists():
            raise FileExistsError(f"state {state_id!r} already exists")
        (directory / "scripts").mkdir(parents=True)
        (directory / "schemas").mkdir()
        (directory / "fixtures").mkdir()

        skill = (
            "---\n"
            f"name: {state_id}\n"
            f"description: {description.strip()} Use when the factory enters {state_id}.\n"
            "---\n\n"
            f"# {state_id.replace('-', ' ').title()}\n\n"
            f"{description.strip()}\n\n"
            "## Contract\n\n"
            "Perform only this state's bounded work. Emit evidence and propose one declared signal. "
            "Never mutate workflow control state directly.\n"
        )
        (directory / "SKILL.md").write_text(skill)
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
        (directory / "schemas" / "input.json").write_text(json.dumps(schema, indent=2) + "\n")
        (directory / "schemas" / "output.json").write_text(json.dumps(schema, indent=2) + "\n")

        scripts: list[dict[str, Any]] = []
        if kind != "terminal":
            validation = (
                "#!/usr/bin/env node\n"
                "import fs from 'node:fs';\n"
                "const [inputPath, outputPath] = process.argv.slice(2);\n"
                "if (!inputPath || !outputPath) process.exit(64);\n"
                "for (const path of [inputPath, outputPath]) {\n"
                "  const value = JSON.parse(fs.readFileSync(path, 'utf8'));\n"
                "  if (value === null || typeof value !== 'object' || Array.isArray(value)) process.exit(1);\n"
                "}\n"
                "process.stdout.write(JSON.stringify({ok: true}) + '\\n');\n"
            )
            script_path = directory / "scripts" / "validate.mjs"
            script_path.write_text(validation)
            scripts.append(
                {
                    "phase": "validate",
                    "path": "scripts/validate.mjs",
                    "sha256": sha256(validation.encode()).hexdigest(),
                    "timeout_seconds": 60,
                }
            )

        manifest = {
            "schema_version": 1,
            "id": state_id,
            "revision": 1,
            "kind": kind,
            "description": description.strip(),
            "skill": "SKILL.md",
            "runner": runner if kind == "agent" else None,
            "input_schema": "schemas/input.json",
            "output_schema": "schemas/output.json",
            "scripts": scripts,
            "evidence": [] if kind == "terminal" else ["state-output"],
            "retry": {"max_attempts": 0 if kind == "terminal" else 2},
            "transitions": transitions or [],
        }
        _write_yaml_atomic(directory / "state.yaml", manifest)
        self._bump_workflow(terminal=state_id if kind == "terminal" else None)
        return directory

    def _bump_workflow(self, *, terminal: str | None = None) -> None:
        with self._lock("definitions"):
            workflow = self.load_workflow()
            workflow["revision"] = int(workflow.get("revision", 0)) + 1
            terminals = list(workflow.get("terminal_states", []))
            if terminal and terminal not in terminals:
                terminals.append(terminal)
            workflow["terminal_states"] = terminals
            _write_yaml_atomic(self.workflow_path, workflow)

    def update_state(
        self,
        state_id: str,
        patch: dict[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self._lock("definitions"):
            directory = self.state_path(state_id)
            manifest = directory / "state.yaml"
            current = _read_yaml(manifest)
            if current.get("revision") != expected_revision:
                raise RuntimeError(
                    f"stale state revision: expected {expected_revision}, "
                    f"current is {current.get('revision')}"
                )
            immutable = {"schema_version", "id", "revision"}.intersection(patch)
            if immutable:
                raise ValueError(f"cannot patch immutable fields: {', '.join(sorted(immutable))}")
            candidate = {**current, **patch, "revision": expected_revision + 1}
            findings = self._audit_state(state_id, directory, candidate)
            if _error_count(findings):
                raise ValueError(
                    "candidate state failed audit: "
                    + "; ".join(f"{item.code}: {item.message}" for item in findings)
                )
            history = self.root / ".factory" / "definitions" / state_id
            history.mkdir(parents=True, exist_ok=True)
            _write_yaml_atomic(history / f"v{expected_revision}.yaml", current)
            shutil.copytree(directory, history / f"v{expected_revision}")
            _write_yaml_atomic(manifest, candidate)
            workflow = self.load_workflow()
            workflow["revision"] = int(workflow["revision"]) + 1
            _write_yaml_atomic(self.workflow_path, workflow)
        return candidate

    def audit_states(self) -> list[Finding]:
        findings: list[Finding] = []
        try:
            workflow = self.load_workflow()
            states = list(self.iter_states())
        except (FileNotFoundError, ValueError, yaml.YAMLError) as error:
            return [Finding("INVALID_WORKFLOW", "error", str(error), path=str(self.workflow_path))]
        if workflow.get("schema_version") != 1:
            findings.append(Finding("WORKFLOW_SCHEMA_VERSION", "error", "schema_version must be 1"))
        if not states:
            findings.append(Finding("NO_STATES", "error", "factory contains no states"))
        for state_id, directory, state in states:
            findings.extend(self._audit_state(state_id, directory, state))
        return self._sort(findings)

    def _audit_state(self, state_id: str, directory: Path, state: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []

        def add(code: str, message: str, path: Path | None = None) -> None:
            findings.append(
                Finding(code, "error", message, state=state_id, path=str(path) if path else None)
            )

        required = {
            "schema_version",
            "id",
            "revision",
            "kind",
            "description",
            "skill",
            "scripts",
            "retry",
            "transitions",
        }
        for key in sorted(required - state.keys()):
            add("MISSING_FIELD", f"missing required field {key!r}", directory / "state.yaml")
        if directory.is_symlink():
            add("SYMLINKED_STATE", "state directory cannot be a symlink", directory)
        if not STATE_ID.fullmatch(state_id):
            add("INVALID_STATE_ID", "state directory must be lowercase kebab-case", directory)
        if state.get("id") != state_id:
            add("STATE_ID_MISMATCH", f"manifest id must equal directory {state_id!r}")
        if state.get("kind") not in STATE_KINDS:
            add("INVALID_STATE_KIND", f"kind must be one of {sorted(STATE_KINDS)}")
        if not isinstance(state.get("revision"), int) or state.get("revision", 0) < 1:
            add("STATE_REVISION", "revision must be a positive integer")

        skill_value = state.get("skill")
        skill = directory / str(skill_value)
        if not isinstance(skill_value, str) or not _within(skill, directory):
            add("INVALID_SKILL_PATH", "skill must stay inside the state directory", skill)
        elif _has_symlink_component(skill, directory):
            add("SYMLINKED_DEFINITION", "skill path cannot contain symlinks", skill)
        elif skill.name != "SKILL.md" or not skill.is_file():
            add("MISSING_SKILL", "state skill must be a regular SKILL.md", skill)
        else:
            match = re.match(r"^---\n(.*?)\n---\n", skill.read_text(), re.DOTALL)
            try:
                metadata = yaml.safe_load(match.group(1)) if match else None
            except yaml.YAMLError:
                metadata = None
            if not isinstance(metadata, dict) or not metadata.get("name") or not metadata.get("description"):
                add("INVALID_SKILL", "SKILL.md needs name and description frontmatter", skill)
            elif metadata.get("name") != state_id:
                add("SKILL_NAME_MISMATCH", "skill frontmatter name must match state id", skill)

        for key in ("input_schema", "output_schema"):
            value = state.get(key)
            schema_path = directory / str(value)
            if not isinstance(value, str) or not _within(schema_path, directory):
                add("INVALID_SCHEMA_PATH", f"{key} must stay inside the state directory", schema_path)
            elif _has_symlink_component(schema_path, directory):
                add("SYMLINKED_DEFINITION", f"{key} path cannot contain symlinks", schema_path)
            elif not schema_path.is_file():
                add("MISSING_SCHEMA", f"{key} must reference a regular JSON file", schema_path)
            else:
                try:
                    json.loads(schema_path.read_text())
                except json.JSONDecodeError as error:
                    add("INVALID_SCHEMA", f"{key} is invalid JSON: {error}", schema_path)

        scripts = state.get("scripts")
        if not isinstance(scripts, list):
            add("INVALID_SCRIPTS", "scripts must be a list")
            scripts = []
        for index, script in enumerate(scripts):
            if not isinstance(script, dict):
                add("INVALID_SCRIPT", f"script {index} must be a mapping")
                continue
            phase = script.get("phase")
            value = script.get("path")
            path = directory / str(value)
            if phase not in SCRIPT_PHASES:
                add("INVALID_SCRIPT_PHASE", f"script {index} has invalid phase {phase!r}")
            if not isinstance(value, str) or not _within(path, directory):
                add("INVALID_SCRIPT_PATH", f"script {index} escapes its state package", path)
                continue
            if _has_symlink_component(path, directory):
                add("SYMLINKED_DEFINITION", "script path cannot contain symlinks", path)
            elif not path.is_file():
                add("MISSING_SCRIPT", "declared script must be a regular file", path)
            elif path.suffix not in {".sh", ".mjs"}:
                add("INVALID_SCRIPT_RUNTIME", "scripts must end in .sh or .mjs", path)
            else:
                actual = sha256(path.read_bytes()).hexdigest()
                if script.get("sha256") != actual:
                    add("SCRIPT_HASH_MISMATCH", "script bytes do not match pinned sha256", path)
                elif path.suffix == ".sh" and not os.access(path, os.X_OK):
                    add("SCRIPT_NOT_EXECUTABLE", "pinned shell script must be executable", path)
            if not isinstance(script.get("timeout_seconds"), int) or script.get("timeout_seconds", 0) < 1:
                add("SCRIPT_TIMEOUT", "script needs a positive timeout_seconds", path)
        if state.get("kind") == "agent" and not state.get("runner"):
            add("MISSING_RUNNER", "agent state needs a runner profile")
        if state.get("kind") != "terminal" and not state.get("evidence"):
            add("MISSING_EVIDENCE", "nonterminal state must declare evidence")
        retry = state.get("retry")
        if not isinstance(retry, dict) or not isinstance(retry.get("max_attempts"), int):
            add("INVALID_RETRY_POLICY", "retry.max_attempts must be an integer")
        elif retry["max_attempts"] < 0:
            add("INVALID_RETRY_POLICY", "retry.max_attempts cannot be negative")
        return findings

    def audit_transitions(self) -> list[Finding]:
        findings: list[Finding] = []
        try:
            workflow = self.load_workflow()
            states = {state_id: state for state_id, _, state in self.iter_states()}
        except (FileNotFoundError, ValueError, yaml.YAMLError) as error:
            return [Finding("INVALID_WORKFLOW", "error", str(error))]
        initial = workflow.get("initial_state")
        if initial not in states:
            findings.append(Finding("MISSING_INITIAL_STATE", "error", f"state {initial!r} does not exist"))
        declared_terminals = set(workflow.get("terminal_states", []))
        actual_terminals = {name for name, state in states.items() if state.get("kind") == "terminal"}
        if declared_terminals != actual_terminals:
            findings.append(
                Finding(
                    "TERMINAL_SET_MISMATCH",
                    "error",
                    f"declared {sorted(declared_terminals)} != actual {sorted(actual_terminals)}",
                )
            )
        if not actual_terminals:
            findings.append(Finding("NO_TERMINAL", "error", "factory needs a terminal state"))

        adjacency: dict[str, list[str]] = {name: [] for name in states}
        valid_signals = {signal.value for signal in Signal}
        for state_id, state in states.items():
            transitions = state.get("transitions")
            if not isinstance(transitions, list):
                findings.append(Finding("INVALID_TRANSITIONS", "error", "transitions must be a list", state_id))
                continue
            if state.get("kind") != "terminal" and not transitions:
                findings.append(Finding("DEAD_END", "error", "nonterminal state has no transition", state_id))
            if state.get("kind") == "terminal" and transitions:
                findings.append(Finding("TERMINAL_HAS_TRANSITIONS", "error", "terminal cannot transition", state_id))
            seen: set[str] = set()
            for index, transition in enumerate(transitions):
                label = f"{state_id}[{index}]"
                if not isinstance(transition, dict):
                    findings.append(Finding("INVALID_TRANSITION", "error", "edge must be a mapping", state_id, label))
                    continue
                signal = transition.get("signal")
                target = transition.get("target")
                if signal not in valid_signals:
                    findings.append(Finding("UNKNOWN_SIGNAL", "error", f"unknown signal {signal!r}", state_id, label))
                elif signal in seen:
                    findings.append(Finding("AMBIGUOUS_SIGNAL", "error", f"duplicate signal {signal!r}", state_id, label))
                else:
                    seen.add(signal)
                if target not in states:
                    findings.append(Finding("MISSING_TARGET", "error", f"target {target!r} does not exist", state_id, label))
                else:
                    adjacency[state_id].append(str(target))

        if initial in states:
            reachable = self._reachable(str(initial), adjacency)
            for state_id in sorted(set(states) - reachable):
                findings.append(Finding("UNREACHABLE_STATE", "error", "unreachable from initial state", state_id))
        reverse: dict[str, list[str]] = {name: [] for name in states}
        for source, targets in adjacency.items():
            for target in targets:
                reverse[target].append(source)
        completable: set[str] = set()
        for terminal in actual_terminals:
            completable |= self._reachable(terminal, reverse)
        for state_id in sorted(set(states) - completable):
            findings.append(Finding("NO_PATH_TO_TERMINAL", "error", "no path to a terminal", state_id))
        for component in self._strongly_connected(adjacency):
            cyclic = len(component) > 1 or any(node in adjacency[node] for node in component)
            if not cyclic:
                continue
            has_exit = any(
                target not in component
                for node in component
                for target in adjacency[node]
            )
            bounded = all(
                states[node].get("retry", {}).get("max_attempts", 0) > 0
                for node in component
            )
            if not has_exit or not bounded:
                findings.append(
                    Finding(
                        "UNBOUNDED_CYCLE",
                        "error",
                        f"cycle {sorted(component)} needs an exit and finite retry budgets",
                    )
                )
        return self._sort(findings)

    @staticmethod
    def _reachable(start: str, adjacency: dict[str, list[str]]) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(adjacency.get(current, []))
        return seen

    @staticmethod
    def _strongly_connected(adjacency: dict[str, list[str]]) -> list[set[str]]:
        index = 0
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        components: list[set[str]] = []

        def visit(node: str) -> None:
            nonlocal index
            indices[node] = lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            for target in adjacency.get(node, []):
                if target not in indices:
                    visit(target)
                    lowlinks[node] = min(lowlinks[node], lowlinks[target])
                elif target in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[target])
            if lowlinks[node] == indices[node]:
                component: set[str] = set()
                while True:
                    member = stack.pop()
                    on_stack.remove(member)
                    component.add(member)
                    if member == node:
                        break
                components.append(component)

        for node in adjacency:
            if node not in indices:
                visit(node)
        return components

    @staticmethod
    def _sort(findings: list[Finding]) -> list[Finding]:
        return sorted(
            findings,
            key=lambda item: (-SEVERITY_ORDER[item.severity], item.state or "", item.code),
        )

    def audit(self) -> list[Finding]:
        return self._sort(self.audit_states() + self.audit_transitions())

    def compile_workflow(
        self,
        *,
        tracker: TrackerConfig,
        runners: RunnersConfig,
    ) -> WorkflowConfig:
        findings = self.audit()
        if _error_count(findings):
            raise ValueError("factory audit failed: " + "; ".join(item.code for item in findings))
        missing_profiles = sorted(
            {
                str(manifest.get("runner"))
                for _, _, manifest in self.iter_states()
                if manifest.get("kind") == "agent"
                and manifest.get("runner") not in runners.available
            }
        )
        if missing_profiles:
            raise ValueError(
                "factory references unavailable runner profiles: " + ", ".join(missing_profiles)
            )
        states: dict[str, StateConfig] = {}
        workflow_revision = int(self.load_workflow()["revision"])
        for state_id, directory, manifest in self.iter_states():
            kind = manifest["kind"]
            transitions = {
                transition["signal"]: transition["target"]
                for transition in manifest.get("transitions", [])
            }
            states[state_id] = StateConfig(
                type=kind,
                prompt=str((directory / manifest["skill"]).resolve()) if kind == "agent" else None,
                runner=manifest.get("runner") if kind == "agent" else None,
                transitions=(
                    StateTransitions.model_validate(transitions) if transitions else None
                ),
                factory_root=str(self.root),
                factory_revision=workflow_revision,
                state_revision=int(manifest["revision"]),
                definition_sha256=self.definition_hash(state_id),
                scripts=manifest.get("scripts", []),
            )
        return WorkflowConfig(
            tracker=tracker,
            runners=runners,
            prompts=PromptsConfig(root=str(self.root)),
            states=states,
        )

    def run_scripts(
        self,
        state_id: str,
        phase: str,
        *arguments: Path | str,
    ) -> list[ScriptResult]:
        if phase not in SCRIPT_PHASES:
            raise ValueError(f"invalid script phase {phase!r}")
        findings = self.audit_states()
        state_errors = [item for item in findings if item.state == state_id and item.severity == "error"]
        if state_errors:
            raise ValueError("state audit failed: " + "; ".join(item.code for item in state_errors))
        directory = self.state_path(state_id)
        state = self.load_state(state_id)
        results: list[ScriptResult] = []
        for definition in state.get("scripts", []):
            if definition.get("phase") != phase:
                continue
            path = directory / definition["path"]
            command = ["node", str(path)] if path.suffix == ".mjs" else [str(path)]
            command.extend(str(Path(argument).resolve()) for argument in arguments)
            try:
                completed = subprocess.run(
                    command,
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=definition["timeout_seconds"],
                    check=False,
                )
                results.append(
                    ScriptResult(
                        state_id,
                        phase,
                        definition["path"],
                        completed.returncode == 0,
                        completed.returncode,
                        completed.stdout,
                        completed.stderr,
                    )
                )
            except subprocess.TimeoutExpired as error:
                stdout = (
                    error.stdout.decode(errors="replace")
                    if isinstance(error.stdout, bytes)
                    else error.stdout or ""
                )
                stderr = (
                    error.stderr.decode(errors="replace")
                    if isinstance(error.stderr, bytes)
                    else error.stderr or "script timed out"
                )
                results.append(
                    ScriptResult(
                        state_id,
                        phase,
                        definition["path"],
                        False,
                        124,
                        stdout,
                        stderr,
                    )
                )
            except OSError as error:
                results.append(
                    ScriptResult(
                        state_id,
                        phase,
                        definition["path"],
                        False,
                        -1,
                        "",
                        str(error),
                    )
                )
        return results

    def plan_repairs(self, findings: list[Finding] | None = None) -> list[RepairAction]:
        actions: list[RepairAction] = []
        for finding in findings or self.audit():
            if finding.code == "SCRIPT_NOT_EXECUTABLE" and finding.path:
                actions.append(
                    RepairAction(
                        "MAKE_SCRIPT_EXECUTABLE",
                        str(Path(finding.path).relative_to(self.root)),
                        True,
                        "restore executable bits on a hash-pinned shell script",
                    )
                )
            elif finding.severity == "error":
                actions.append(
                    RepairAction(
                        f"REVIEW_{finding.code}",
                        "factory.yaml",
                        False,
                        finding.message,
                    )
                )
        return list({(action.code, action.path): action for action in actions}.values())

    def self_heal(self, *, apply: bool = False) -> HealResult:
        before = self.audit()
        actions = self.plan_repairs(before)
        automatic = [action for action in actions if action.automatic]
        if not apply or not automatic:
            return HealResult(False, False, _error_count(before), _error_count(before), tuple(actions), tuple(before))

        candidate_parent = Path(tempfile.mkdtemp(prefix="autosymph-heal-"))
        candidate = candidate_parent / self.root.name
        try:
            shutil.copytree(self.root, candidate)
            for action in automatic:
                path = candidate / action.path
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            after = FactoryRepository(candidate).audit()
            old_errors = {(item.code, item.state, item.transition) for item in before if item.severity == "error"}
            new_errors = {
                (item.code, item.state, item.transition)
                for item in after
                if item.severity == "error" and (item.code, item.state, item.transition) not in old_errors
            }
            improved = _error_count(after) < _error_count(before) and not new_errors
            if not improved:
                return HealResult(True, False, _error_count(before), _error_count(after), tuple(actions), tuple(after))
            backup = self.root / ".factory" / "heals"
            for action in automatic:
                source = candidate / action.path
                destination = self.root / action.path
                saved = backup / action.path
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved)
                replacement = destination.with_name(f".{destination.name}.heal")
                shutil.copy2(source, replacement)
                os.replace(replacement, destination)
            final = self.audit()
            return HealResult(True, True, _error_count(before), _error_count(final), tuple(actions), tuple(final))
        finally:
            shutil.rmtree(candidate_parent, ignore_errors=True)


def findings_as_json(findings: list[Finding]) -> str:
    return json.dumps([finding.as_dict() for finding in findings], indent=2, sort_keys=True)
