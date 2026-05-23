"""Wizard step functions.

Each step takes ``(state, prompter, linear)`` (or a subset) and mutates
``state`` in place. The runner orchestrates the order. Steps raise wizard
exceptions (see ``errors.py``) on aborts/failures so the runner can centralize
the recovery messaging.

Critical ordering invariant (PRD R2 / R14):
- Steps 1-2 run BEFORE the first network call (lockfile + read-only scan).
- Step 3 is the first Linear call (resolve_viewer).
- All local input collection happens BEFORE step_present_combined_preview.
- Linear mutations execute ONLY after the combined-preview confirmation.
- User-config file writes execute ONLY after Linear mutations succeed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import autosymph
from autosymph.linear_client import LinearClient
from autosymph.resources import _workspace_needs_simulator
from autosymph.wizard.errors import (
    WizardAborted,
    WizardAlreadyConfigured,
    WizardLinearMutationFailed,
    WizardPromptsRootMissing,
)
from autosymph.wizard.prompter import Prompter
from autosymph.wizard.state import (
    PlannedMutation,
    StateDiffEntry,
    WizardState,
)
from autosymph.wizard.state_defaults import (
    KNOWN_ALIASES,
    REQUIRED_V1,
    find_default,
)

logger = logging.getLogger(__name__)


def _config_dir() -> Path:
    """Resolve the autosymph config directory (~/.autosymph/config by default)."""
    override = os.environ.get("AUTOSYMPH_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".autosymph" / "config"


def _hostname_slug() -> str:
    """Lowercased short hostname (matches the loader's lookup pattern)."""
    return socket.gethostname().split(".")[0].lower()


# --- Step 1: lockfile -------------------------------------------------------


@contextmanager
def step_acquire_lock(state: WizardState) -> Iterator[Path]:
    """Acquire ``~/.autosymph/config/.wizard.lock`` for the duration of the run.

    Uses an exclusive open to detect concurrent runs. Stale lockfiles (PID no
    longer alive) are reported separately by ``step_detect_existing_config`` —
    we do NOT auto-delete here to avoid the case where two real wizards
    collide.
    """
    cfg_dir = _config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg_dir / ".wizard.lock"
    try:
        # O_EXCL: fail if it exists. PID written so a future run can detect stale.
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise WizardAlreadyConfigured(
            str(lock_path),
            "another wizard run already holds the lockfile (or a previous "
            "run crashed without releasing it; see existing-config guidance)",
        )
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        logger.info("Acquired wizard lockfile at %s", lock_path)
        yield lock_path
    finally:
        try:
            lock_path.unlink()
            logger.info("Released wizard lockfile")
        except FileNotFoundError:
            pass


def _is_pid_alive(pid: int) -> bool:
    """Cheap liveness check via signal 0."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by a different user — count it as alive.
        return True
    return True


# --- Step 2: detect existing config ----------------------------------------


def step_detect_existing_config(state: WizardState) -> WizardState:
    """Read-only scan; raise ``WizardAlreadyConfigured`` on any sign of prior setup.

    Triggers (any one is enough):
    - ``devices/{hostname}.yaml`` exists
    - any ``projects/*.yaml`` exists
    - ``local.env`` exists with non-empty content
    - stale ``.wizard.lock`` (PID file but no live process) — separate message
    """
    cfg_dir = _config_dir()
    devices_dir = cfg_dir / "devices"
    projects_dir = cfg_dir / "projects"
    local_env = cfg_dir / "local.env"
    lock_path = cfg_dir / ".wizard.lock"

    # Stale lockfile — distinct from "another run holds it" because that case
    # would have been caught earlier by step_acquire_lock's O_EXCL failure.
    # If we're here and the lock exists, that means it's stale (no live PID
    # holding it) OR our own run, but our own run only just created it after
    # step_acquire_lock succeeded — so this branch is unreachable from a
    # production caller. We still check, defensively, in case tests bypass
    # the runner sequencing.
    if lock_path.exists():
        try:
            content = lock_path.read_text().strip()
            pid = int(content) if content else 0
        except (ValueError, OSError):
            pid = 0
        if pid and pid != os.getpid() and not _is_pid_alive(pid):
            raise WizardAlreadyConfigured(
                str(lock_path),
                f"stale lockfile from PID {pid}; remove with: rm {lock_path}",
            )

    device_yaml = devices_dir / f"{_hostname_slug()}.yaml"
    if device_yaml.exists():
        raise WizardAlreadyConfigured(
            str(device_yaml),
            "a device config for this hostname already exists",
        )

    if projects_dir.is_dir():
        for project_yaml in projects_dir.glob("*.yaml"):
            raise WizardAlreadyConfigured(
                str(project_yaml),
                "a project config already exists",
            )

    if local_env.exists() and local_env.read_text().strip():
        raise WizardAlreadyConfigured(
            str(local_env),
            "non-empty local.env already exists",
        )

    return state


# --- Step 3: collect Linear key --------------------------------------------


def step_collect_linear_key(state: WizardState, prompter: Prompter) -> WizardState:
    """Prompt for ``LINEAR_API_KEY`` and validate via ``resolve_viewer()``.

    First network call. First user-config-blocking error gate. Loops until a
    valid key is provided or the user aborts. Existing ``LINEAR_API_KEY`` env
    var is offered as a default.
    """
    env_default = os.environ.get("LINEAR_API_KEY", "")
    while True:
        prompt = "Linear API key (lin_api_...)"
        if env_default:
            prompt += " [press enter to use $LINEAR_API_KEY from env]"
        raw = prompter.ask(prompt, default=env_default if env_default else None)
        key = raw.strip()
        if not key:
            if not prompter.confirm("No key entered — abort the wizard?", default=False):
                continue
            raise WizardAborted("collect_linear_key")

        # Validate via resolve_viewer. Bind a temporary client just for the check.
        # The real client used downstream is built from the wizard state after
        # the project is selected (LinearClient takes project_name in __init__).
        check_client = LinearClient(api_key=key, project_name="__wizard_probe__")

        async def _probe() -> str:
            try:
                return await check_client.resolve_viewer()
            finally:
                await check_client.close()

        try:
            viewer_id = asyncio.run(_probe())
        except Exception as exc:
            logger.warning("Linear API key validation failed: %s", exc)
            if not prompter.confirm(
                f"Key didn't validate ({exc!s}). Try a different key?", default=True
            ):
                raise WizardAborted("collect_linear_key")
            continue

        state.linear_api_key = key
        state.viewer_user_id = viewer_id
        return state


# --- Step 4: select project -------------------------------------------------


def step_select_project(
    state: WizardState, prompter: Prompter, linear: LinearClient
) -> WizardState:
    """List visible projects and let the user pick one."""
    assert state.linear_api_key, "step_collect_linear_key must run first"
    projects = asyncio.run(linear.list_projects())
    if not projects:
        raise WizardAborted("select_project: no Linear projects visible to this API key")

    labels = [
        f"{p['name']}"
        + (f"  ({', '.join(p.get('team_names', [])) or '—'})" if p.get("team_names") else "")
        for p in projects
    ]
    idx = prompter.select("Select the Linear project for this autosymph instance:", labels)
    chosen = projects[idx]
    state.project_id = chosen["id"]
    state.project_name = chosen["name"]
    # Slug is derived from the project name (filesystem-safe, no spaces).
    state.project_slug = _slugify(chosen["name"])
    # Pick the first team (resolve_team_for_project on the live client will
    # match this when we call it next).
    return state


def _slugify(name: str) -> str:
    """Lowercase, replace whitespace and slashes with hyphens, strip non-alnum."""
    out: list[str] = []
    prev_dash = False
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif ch in (" ", "_", "-", "/", ".") and not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")
    return slug or "project"


# --- Step 5: fetch workspace -----------------------------------------------


def step_fetch_workspace(state: WizardState, linear: LinearClient) -> WizardState:
    """Resolve the project's team and fetch existing states + labels."""
    assert state.project_id, "step_select_project must run first"
    # The LinearClient already lazy-resolves project_id from project_name; we
    # short-circuit by setting _project_id directly so resolve_team_for_project
    # uses our chosen project.
    linear._project_id = state.project_id
    state.team_id = asyncio.run(linear.resolve_team_for_project())
    state.existing_states = asyncio.run(linear.fetch_workspace_states(state.team_id))
    state.existing_labels = asyncio.run(linear.fetch_workspace_labels())
    return state


# --- Step 6: compute state diff --------------------------------------------


def step_compute_state_diff(state: WizardState) -> WizardState:
    """Categorize each REQUIRED_V1 state as exact / near / missing.

    Comparison rules (PRD R4):
    - "exact": case-insensitive equality with a workspace state name.
    - "near":  workspace has a state name in KNOWN_ALIASES whose value equals
               the canonical name we're checking.
    - "missing": neither.

    No substring or fuzzy matching.
    """
    workspace_lower = {s.lower(): s for s in state.existing_states}
    # Pre-compute alias lookup: canonical_name -> linear_name (if a Linear state
    # matches an alias key). Case-insensitive on the alias key.
    alias_for_canonical: dict[str, str] = {}
    for linear_existing, canonical in KNOWN_ALIASES.items():
        if linear_existing.lower() in workspace_lower:
            alias_for_canonical[canonical] = workspace_lower[linear_existing.lower()]

    diff: list[StateDiffEntry] = []
    for entry in REQUIRED_V1:
        canonical = entry.name
        if canonical.lower() in workspace_lower:
            diff.append(
                StateDiffEntry(
                    canonical_name=canonical,
                    linear_name=workspace_lower[canonical.lower()],
                    category="exact",
                )
            )
        elif canonical in alias_for_canonical:
            diff.append(
                StateDiffEntry(
                    canonical_name=canonical,
                    linear_name=alias_for_canonical[canonical],
                    category="near",
                )
            )
        else:
            diff.append(
                StateDiffEntry(
                    canonical_name=canonical,
                    linear_name=None,
                    category="missing",
                )
            )
    state.state_diff = diff
    return state


# --- Step 7: resolve near matches ------------------------------------------


def step_resolve_near_matches(state: WizardState, prompter: Prompter) -> WizardState:
    """Per-near-match prompt: accept the alias mapping or demote to missing.

    Per PRD R5: when the user rejects a near match, that slot is re-categorized
    as ``missing`` and will be created in the combined-preview phase.
    """
    for entry in state.state_diff:
        if entry.category != "near":
            continue
        prompt = (
            f"Linear has state {entry.linear_name!r}. "
            f"Use it for autosymph's {entry.canonical_name!r} slot? "
            f"(rejecting will create a new {entry.canonical_name!r} state)"
        )
        if prompter.confirm(prompt, default=True):
            # Keep the alias mapping; nothing to change on the entry.
            continue
        # Demote: re-categorize this slot as missing and clear linear_name.
        entry.category = "missing"
        entry.linear_name = None
    return state


# --- Step 8: collect repo path ---------------------------------------------


def step_collect_repo_path(state: WizardState, prompter: Prompter) -> WizardState:
    """Prompt for the target repo path; validate via ``git rev-parse``."""
    while True:
        raw = prompter.ask("Path to the project repo (must be a git repo)")
        path = Path(raw.strip()).expanduser()
        if not path.exists():
            if prompter.confirm(f"{path} does not exist. Try again?", default=True):
                continue
            raise WizardAborted("collect_repo_path")
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:
            if prompter.confirm(f"git invocation failed ({exc!s}). Try again?", default=True):
                continue
            raise WizardAborted("collect_repo_path")
        if result.returncode != 0:
            if prompter.confirm(
                f"{path} is not a git repository ({result.stderr.strip()}). Try again?",
                default=True,
            ):
                continue
            raise WizardAborted("collect_repo_path")
        # Use the toplevel from git so symlinks and subdirs normalize.
        state.repo_path = Path(result.stdout.strip()).resolve()
        return state


# --- Step 9: detect simulators ---------------------------------------------


def step_detect_simulators(state: WizardState, prompter: Prompter) -> WizardState:
    """Auto-detect iOS via ``_workspace_needs_simulator``; if needed, list sims.

    On simctl failure or timeout, prints a warning and proceeds with no sim
    block. Wizard never blocks on this step.
    """
    assert state.repo_path is not None, "step_collect_repo_path must run first"
    state.needs_simulator = _workspace_needs_simulator(state.repo_path)
    if not state.needs_simulator:
        logger.info("No iOS project files detected — skipping simulator selection")
        return state

    try:
        result = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "available", "-j"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("xcrun simctl unavailable (%s) — skipping sim detection", exc)
        # Print a hint so the user knows what was skipped.
        try:
            sys.stderr.write(
                "  warning: xcrun simctl unavailable; install Xcode Command Line "
                "Tools and re-run if iOS verification is needed.\n"
            )
        except Exception:
            pass
        return state
    if result.returncode != 0:
        logger.warning("xcrun simctl returned %d — skipping sim detection", result.returncode)
        return state

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("simctl output was not JSON — skipping sim detection")
        return state

    sims: list[str] = []
    for runtime, devices in data.get("devices", {}).items():
        for dev in devices:
            if dev.get("isAvailable") and dev.get("name"):
                sims.append(dev["name"])
    if not sims:
        logger.info("No available simulators — skipping selection")
        return state

    # Dedupe while keeping order (some sims share names across runtimes).
    seen: set[str] = set()
    unique_sims: list[str] = []
    for s in sims:
        if s not in seen:
            unique_sims.append(s)
            seen.add(s)

    chosen_idx = prompter.multi_select(
        "Select iOS simulators for this project (multiple allowed; blank for none):",
        unique_sims,
    )
    state.selected_simulators = [unique_sims[i] for i in chosen_idx]
    return state


# --- Step 10: choose template ----------------------------------------------


def step_choose_template(state: WizardState, prompter: Prompter) -> WizardState:
    """Default to ios if simulator detected, else web; allow override."""
    default: str = "ios" if state.needs_simulator else "web"
    choices = ["ios", "web"]
    default_idx = choices.index(default)
    msg = (
        f"Project template (default: {default} — based on "
        f"{'.xcodeproj/capacitor detection' if state.needs_simulator else 'no iOS markers'}):"
    )
    # Render with the default annotated.
    annotated = [c + (" [default]" if c == default else "") for c in choices]
    chosen = prompter.select(msg, annotated)
    template = choices[chosen]
    if template not in ("ios", "web"):
        raise ValueError(f"unexpected template choice {template!r}")  # defensive
    state.template_choice = template  # type: ignore[assignment]
    _ = default_idx  # silence linter; used implicitly in the annotated rendering
    return state


# --- Step 11: resolve prompts.root -----------------------------------------


REQUIRED_PROMPT_FILES = ("implement.md", "verify.md", "merge.md", "global.md")


def step_resolve_prompts_root(state: WizardState) -> WizardState:
    """Compute and verify the absolute path to autosymph's ``prompts/`` dir.

    Layout (source / editable install):
      <repo>/src/autosymph/__init__.py    <- autosymph.__file__
      <repo>/prompts/

    So we walk up from ``autosymph.__file__`` two parents (autosymph -> src ->
    repo). For wheel-only installs, this directory typically does not exist;
    AC13 says we exit with clear guidance instead of writing a broken path.
    """
    pkg_init = Path(autosymph.__file__).resolve()
    # autosymph/__init__.py -> autosymph -> src -> repo
    repo_root = pkg_init.parent.parent.parent
    candidate = repo_root / "prompts"

    missing = [
        f for f in REQUIRED_PROMPT_FILES if not (candidate / f).is_file()
    ]
    if missing:
        raise WizardPromptsRootMissing(str(candidate), missing)

    state.prompts_root_abs = candidate
    return state


# --- Step 12: collect optional Braintrust key ------------------------------


def step_collect_braintrust(state: WizardState, prompter: Prompter) -> WizardState:
    """Optional: ask for ``BRAINTRUST_API_KEY``. No validation."""
    if not prompter.confirm("Configure Braintrust tracing? (optional)", default=False):
        return state
    raw = prompter.ask("BRAINTRUST_API_KEY (input will be stored in local.env)")
    key = raw.strip()
    if key:
        state.braintrust_api_key = key
    return state


# --- Step 13: build plan ---------------------------------------------------


def step_build_plan(state: WizardState) -> WizardState:
    """Compute ``planned_mutations`` and ``planned_writes``.

    - ``planned_mutations``: one ``PlannedMutation`` per ``missing`` diff entry.
    - ``planned_writes``: dict of path -> rendered YAML content for the device,
      project, and local.env files. Project YAML's ``linear_states`` block uses
      Linear's existing names where the diff is ``exact`` or ``near``, and the
      canonical names for ``missing`` entries (which will be created).
    """
    assert state.repo_path is not None
    assert state.prompts_root_abs is not None
    assert state.template_choice is not None
    assert state.project_name is not None
    assert state.project_slug is not None
    assert state.linear_api_key is not None

    # Mutations: one per missing.
    mutations: list[PlannedMutation] = []
    for entry in state.state_diff:
        if entry.category != "missing":
            continue
        default = find_default(entry.canonical_name)
        if default is None:
            # Defensive: REQUIRED_V1 is the source list, so this can't happen
            # unless the diff was constructed wrong.
            raise RuntimeError(
                f"No StateDefault for canonical name {entry.canonical_name!r}"
            )
        mutations.append(
            PlannedMutation(
                canonical_name=default.name,
                color=default.color,
                position=default.position,
                state_type=default.state_type,
            )
        )
    state.planned_mutations = mutations
    state.state_defaults_used = [d for d in REQUIRED_V1]

    # Build the linear_states mapping that goes into the project YAML.
    # Slots in autosymph's schema: todo, active, verifying, review,
    # gate_approved, rework, blocked, terminal (+ optional autoplan / verify_review
    # which we never set in v1).
    slot_for_canonical = {
        "Ready": "todo",
        "Implementing": "active",
        "Verifying": "verifying",
        "Investigating": "investigating",
        "In Review": "review",
        "Rework": "rework",
        "Merging": "gate_approved",
        "Blocked": "blocked",
    }
    linear_states_map: dict[str, str | list[str]] = {}
    terminals: list[str] = []
    for entry in state.state_diff:
        # The actual name we use is: linear_name if present (exact or
        # accepted near), otherwise canonical (because we'll create it).
        actual = entry.linear_name if entry.linear_name else entry.canonical_name
        if entry.canonical_name in ("Done", "Canceled", "Duplicate"):
            terminals.append(actual)
            continue
        slot = slot_for_canonical.get(entry.canonical_name)
        if slot is None:
            continue  # shouldn't happen for v1 set; defensive
        linear_states_map[slot] = actual
    linear_states_map["terminal"] = terminals

    # File writes.
    cfg_dir = _config_dir()
    devices_dir = cfg_dir / "devices"
    projects_dir = cfg_dir / "projects"
    device_yaml = devices_dir / f"{_hostname_slug()}.yaml"
    project_yaml = projects_dir / f"{state.project_slug}.yaml"
    local_env = cfg_dir / "local.env"

    state.planned_writes = {
        device_yaml: _render_device_yaml(state),
        project_yaml: _render_project_yaml(state, linear_states_map),
        local_env: _render_local_env(state),
    }
    return state


def _yaml_quote(value: str) -> str:
    """Return a YAML-safe double-quoted string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_device_yaml(state: WizardState) -> str:
    """Render the device YAML based on collected inputs."""
    assert state.repo_path is not None
    assert state.project_slug is not None

    lines: list[str] = []
    lines.append(f"# autosymph device config for {_hostname_slug()}")
    lines.append("# Generated by `autosymph init` — edit freely after generation.")
    lines.append("")
    lines.append(f"machine_name: {_yaml_quote(_hostname_slug())}")
    lines.append("")
    if state.selected_simulators:
        lines.append("resources:")
        lines.append("  ios_simulator:")
        for sim in state.selected_simulators:
            lines.append(f"    - name: {_yaml_quote(sim)}")
            lines.append("      udid: auto")
        lines.append("")
    lines.append("agent:")
    lines.append("  max_concurrent_agents: 3")
    lines.append("")
    lines.append("projects:")
    lines.append(f"  {state.project_slug}:")
    lines.append(f"    repo: {_yaml_quote(str(state.repo_path))}")
    lines.append("")
    return "\n".join(lines)


def _render_project_yaml(
    state: WizardState, linear_states_map: dict[str, str | list[str]]
) -> str:
    """Render the project YAML for the chosen template (ios or web)."""
    assert state.template_choice in ("ios", "web")
    assert state.prompts_root_abs is not None
    assert state.project_name is not None

    lines: list[str] = []
    lines.append(f"# autosymph project config for {state.project_name}")
    lines.append("# Generated by `autosymph init`.")
    lines.append("")
    lines.append("tracker:")
    lines.append("  kind: linear")
    lines.append(f"  project: {_yaml_quote(state.project_name)}")
    lines.append('  api_key: "$LINEAR_API_KEY"')
    lines.append("")
    lines.append("polling:")
    lines.append("  interval_ms: 30000")
    lines.append("")
    lines.append("linear_states:")
    for slot in (
        "todo", "active", "verifying", "investigating",
        "review", "rework", "gate_approved", "blocked",
    ):
        if slot in linear_states_map:
            lines.append(f"  {slot}: {_yaml_quote(str(linear_states_map[slot]))}")
    terminal = linear_states_map.get("terminal")
    if isinstance(terminal, list) and terminal:
        rendered = ", ".join(_yaml_quote(t) for t in terminal)
        lines.append(f"  terminal: [{rendered}]")
    lines.append("")
    lines.append("workspace:")
    lines.append('  root: "~/.autosymph/workspaces"')
    lines.append("")
    lines.append("prompts:")
    lines.append(f"  root: {_yaml_quote(str(state.prompts_root_abs))}")
    lines.append('  global_prompt: "global.md"')
    lines.append("")
    lines.append("runners:")
    lines.append('  default: claude')
    lines.append("  available:")
    lines.append("    claude:")
    lines.append("      type: claude")
    lines.append("    codex:")
    lines.append("      type: codex")
    lines.append("  auto_match: []")
    lines.append("")
    lines.append("states:")
    lines.append("  implement:")
    lines.append("    type: agent")
    lines.append('    prompt: "implement.md"')
    lines.append("    linear_state: active")
    lines.append("    max_turns: 100")
    lines.append("    session: inherit")
    lines.append("    transitions:")
    lines.append("      complete: verify")
    lines.append("  verify:")
    lines.append("    type: agent")
    lines.append('    prompt: "verify.md"')
    lines.append("    linear_state: verifying")
    lines.append("    max_turns: 50")
    lines.append("    session: new")
    lines.append("    transitions:")
    lines.append("      complete: review")
    lines.append("      fail: implement")
    lines.append("  review:")
    lines.append("    type: gate")
    lines.append("    linear_state: review")
    lines.append("    rework_to: rework")
    lines.append("    max_rework: 3")
    lines.append("    rework_exhausted: todo")
    lines.append("    transitions:")
    lines.append("      approve: finalize")
    lines.append("  rework:")
    lines.append("    type: agent")
    lines.append('    prompt: "implement.md"')
    lines.append("    linear_state: rework")
    lines.append("    max_turns: 100")
    lines.append("    session: inherit")
    lines.append("    transitions:")
    lines.append("      complete: verify")
    lines.append("  finalize:")
    lines.append("    type: agent")
    lines.append('    prompt: "merge.md"')
    lines.append("    linear_state: gate_approved")
    lines.append("    max_turns: 100")
    lines.append("    session: inherit")
    lines.append("    transitions:")
    lines.append("      complete: done")
    lines.append("  done:")
    lines.append("    type: terminal")
    lines.append("    linear_state: terminal")
    lines.append("")
    return "\n".join(lines)


def _render_local_env(state: WizardState) -> str:
    """Render local.env with the collected secrets."""
    assert state.linear_api_key is not None
    lines = [
        "# autosymph local secrets — generated by `autosymph init`.",
        "# Shell-provided env vars take precedence over these defaults.",
        "",
        f"LINEAR_API_KEY={state.linear_api_key}",
    ]
    if state.braintrust_api_key:
        lines.append(f"BRAINTRUST_API_KEY={state.braintrust_api_key}")
    lines.append("")
    return "\n".join(lines)


# --- Step 14: combined preview ---------------------------------------------


def step_present_combined_preview(state: WizardState, prompter: Prompter) -> WizardState:
    """Render the full plan and ask for one all-or-nothing confirmation."""
    out = sys.stderr
    out.write("\n" + "=" * 60 + "\n")
    out.write("AUTOSYMPH INIT — PREVIEW\n")
    out.write("=" * 60 + "\n\n")

    if state.planned_mutations:
        out.write("Linear states to CREATE:\n")
        for m in state.planned_mutations:
            out.write(
                f"  + {m.canonical_name:<14} color={m.color}  "
                f"type={m.state_type}  position={m.position}\n"
            )
    else:
        out.write("Linear states to CREATE: (none — workspace already has all required states)\n")
    out.write("\n")

    accepted_aliases = [
        e for e in state.state_diff if e.category == "near"
    ]
    if accepted_aliases:
        out.write("Accepted Linear-name aliases:\n")
        for e in accepted_aliases:
            out.write(
                f"  - Linear {e.linear_name!r:<24} -> autosymph slot {e.canonical_name!r}\n"
            )
        out.write("\n")

    out.write("Files to WRITE:\n")
    for path, content in state.planned_writes.items():
        out.write(f"\n  --- {path} ({len(content)} bytes) ---\n")
        for line in content.splitlines():
            out.write(f"    {line}\n")
    out.write("\n" + "=" * 60 + "\n")

    if not prompter.confirm(
        "Execute all the above (Linear mutations + file writes)?", default=False
    ):
        raise WizardAborted("present_combined_preview")
    return state


# --- Step 15: execute Linear mutations -------------------------------------


def step_execute_linear_mutations(state: WizardState, linear: LinearClient) -> WizardState:
    """Serialized ``workflowStateCreate`` calls; append to ``wizard-mutations.log`` per success.

    On persistent failure, raises ``WizardLinearMutationFailed`` with the list
    of names that succeeded so the runner can produce recovery guidance. Per
    R14, the mutation log is part of the Linear-mutation phase, NOT a
    user-config write.
    """
    assert state.team_id is not None, "step_fetch_workspace must run first"
    cfg_dir = _config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg_dir / "wizard-mutations.log"

    succeeded: list[str] = []
    for m in state.planned_mutations:
        try:
            state_id = asyncio.run(
                linear.create_workflow_state(
                    team_id=state.team_id,
                    name=m.canonical_name,
                    color=m.color,
                    position=m.position,
                    state_type=m.state_type,
                )
            )
        except Exception as exc:
            raise WizardLinearMutationFailed(
                failed_state=m.canonical_name,
                succeeded_states=list(succeeded),
                cause=str(exc),
            ) from exc
        # Append-only audit log.
        ts = datetime.now(timezone.utc).isoformat()
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{ts}\t{m.canonical_name}\t{state_id}\n")
        succeeded.append(m.canonical_name)
    return state


# --- Step 16: atomic writes ------------------------------------------------


def step_atomic_writes(state: WizardState) -> WizardState:
    """Write each ``(path, content)`` via temp-then-rename. SIGINT-safe.

    Only user-config files (``devices/``, ``projects/``, ``local.env``). Does
    NOT touch the mutation log.
    """
    for path, content in state.planned_writes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        # Open exclusively to avoid clobbering a concurrent write (defensive —
        # the lockfile already prevents concurrent wizards).
        with tmp.open("w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
        # Permissions for local.env: keep secrets owner-readable.
        if path.name == "local.env":
            try:
                os.chmod(path, 0o600)
            except OSError:
                logger.warning("Could not chmod 0600 on %s", path)
    return state


# --- Step 17: verify via _find_configs -------------------------------------


def step_verify_via_find_configs(state: WizardState) -> WizardState:
    """Call the same loader ``autosymph start`` uses; assert the wizard's outputs load.

    Wires ``AUTOSYMPH_CONFIG_DIR`` to the wizard's output dir before calling
    so the loader picks up our just-written files.
    """
    cfg_dir = _config_dir()
    # Defer the import so this module stays cheap to import in tests.
    from autosymph.cli import _find_configs  # type: ignore[attr-defined]

    prev = os.environ.get("AUTOSYMPH_CONFIG_DIR")
    os.environ["AUTOSYMPH_CONFIG_DIR"] = str(cfg_dir)
    try:
        valid, warnings = _find_configs(None)
    finally:
        if prev is None:
            os.environ.pop("AUTOSYMPH_CONFIG_DIR", None)
        else:
            os.environ["AUTOSYMPH_CONFIG_DIR"] = prev

    # Filter warnings to only those tied to wizard-written paths.
    wizard_paths = {str(p) for p in state.planned_writes.keys()}
    relevant_warnings = [
        w for w in warnings
        if any(p in w for p in wizard_paths)
    ]
    if not valid:
        raise RuntimeError(
            f"Wizard self-check failed: _find_configs() found 0 valid configs "
            f"in {cfg_dir}. Warnings: {warnings}"
        )
    if relevant_warnings:
        raise RuntimeError(
            f"Wizard self-check failed: warnings on wizard outputs: {relevant_warnings}"
        )
    logger.info("Wizard self-check passed: %d valid config(s) loaded", len(valid))
    return state


# --- Step 18: print next steps ---------------------------------------------


def step_print_next_steps(state: WizardState) -> WizardState:
    """Final guidance block (printed to stderr so it shows alongside the work)."""
    cfg_dir = _config_dir()
    out = sys.stderr
    out.write("\n" + "=" * 60 + "\n")
    out.write("AUTOSYMPH INIT — DONE\n")
    out.write("=" * 60 + "\n\n")
    out.write("Next steps:\n")
    out.write("  1. Install or symlink runtime skills:\n")
    out.write("       scripts/install-skills.sh --target $HOME/.autosymph/skills\n")
    out.write("  2. Validate your config:\n")
    if state.project_slug:
        out.write(
            f"       uv run autosymph config check {cfg_dir}/projects/{state.project_slug}.yaml\n"
        )
    out.write("  3. Check model registry is current:\n")
    out.write("       uv run autosymph models check\n")
    out.write("  4. Run autosymph:\n")
    out.write("       uv run autosymph start\n\n")
    out.write(
        "Note: your coding agent (Claude Code, Codex, etc.) needs Linear MCP\n"
        "configured separately — autosymph doesn't manage your agent harness.\n"
    )
    if state.planned_mutations:
        log_path = cfg_dir / "wizard-mutations.log"
        out.write(
            f"\nLinear states created by this run are listed in:\n"
            f"  {log_path}\n"
        )
    out.write("\n")
    _ = time  # silence unused-import linter; reserved for future dur tracking
    return state
