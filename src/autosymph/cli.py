"""CLI entry point — autosymph start, status, config check, logs, tail, dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
from pathlib import Path

import click

from autosymph.factory_cli import factory
from autosymph.config import (
    ConfigError,
    WorkflowConfig,
    load_config,
    load_device_config,
    merge_device_project,
    validate_config,
)
from autosymph.models import (
    KNOWN_STALE,
    LATEST,
    collect_stale_models,
    compute_registry_update,
    pick_latest_per_family,
    write_models_py,
)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy HTTP debug logs
    for name in ("httpcore", "httpx", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _setup_logging_file_only(log_root: Path | None = None) -> None:
    """In TUI mode, send all logs to file instead of stdout."""
    log_dir = log_root or Path("~/.autosymph/logs").expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "autosymph.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Remove any existing handlers (prevent stdout leakage)
    root.handlers.clear()
    handler = logging.FileHandler(str(log_file))
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(handler)
    for name in ("httpcore", "httpx", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


@click.group()
@click.version_option(package_name="autosymph")
def main() -> None:
    """autosymph — AI agent orchestrator."""


main.add_command(factory)


@main.command("sample-flow")
@click.option(
    "--runner",
    "runner_profile",
    type=click.Choice(["claude-code", "codex", "omp-subscription", "omp-claude-api"]),
    default="claude-code",
    show_default=True,
)
@click.option(
    "--workspace",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Empty directory for the sample repository; defaults to a new /tmp directory.",
)
def sample_flow(runner_profile: str, workspace: Path | None) -> None:
    """Run a real local implement -> deterministic verify -> done flow."""
    from autosymph.sample_flow import run_sample_flow_sync

    result = run_sample_flow_sync(runner_profile=runner_profile, workspace=workspace)
    click.echo(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    if not result.success:
        raise click.ClickException(
            "sample flow did not reach done: " + ", ".join(result.gate_reasons)
        )


def _find_config(explicit: str | None) -> Path:
    """Resolve config path: explicit flag > $AUTOSYMPH_CONFIG_DIR/{hostname}.yaml > local workflow.yaml."""
    if explicit:
        return Path(explicit)

    import os
    import socket
    hostname = socket.gethostname().split(".")[0].lower()
    config_dir = os.environ.get("AUTOSYMPH_CONFIG_DIR", str(Path.home() / ".autosymph" / "config"))
    machine_config = Path(config_dir) / f"{hostname}.yaml"
    if machine_config.exists():
        return machine_config

    return Path("workflow.yaml")


def _default_config_dir() -> Path:
    return Path(
        os.environ.get(
            "AUTOSYMPH_CONFIG_DIR",
            str(Path.home() / ".autosymph" / "config"),
        )
    )


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].strip()
    if "=" not in stripped:
        return None

    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        return None

    try:
        parts = shlex.split(raw_value, comments=True, posix=True)
    except ValueError:
        value = raw_value.strip()
    else:
        value = parts[0] if parts else ""
    return key, value


def _load_local_env(config_dir: Path | None = None) -> Path | None:
    """Load config-local environment defaults from local.env.

    Shell-provided variables win. This lets a machine keep secrets and local
    startup defaults beside the runtime config without requiring every launch
    command to repeat them.
    """
    env_path = (config_dir or _default_config_dir()) / "local.env"
    if not env_path.exists():
        return None

    for line in env_path.read_text().splitlines():
        parsed = _parse_env_line(line)
        if not parsed:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)
    return env_path


def _find_configs(explicit: str | None) -> tuple[list[tuple[Path, WorkflowConfig]], list[str]]:
    """Discover and load workflow configs.

    Supports three modes:
    1. Explicit -c flag: load that single file (legacy WorkflowConfig)
    2. Layered: devices/{hostname}.yaml + projects/*.yaml → merged configs
    3. Legacy flat: *.yaml in config dir → each is a full WorkflowConfig

    Returns (valid_configs, warnings) where warnings are human-readable
    strings for skipped/invalid files.
    """
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise click.ClickException(f"Config file not found: {path}")
        try:
            cfg = load_config(path)
            validate_config(cfg)
            return ([(path.resolve(), cfg)], [])
        except ConfigError as e:
            raise click.ClickException(f"Invalid config {path}: {e}") from e

    config_dir = _default_config_dir()

    valid_configs: list[tuple[Path, WorkflowConfig]] = []
    warnings: list[str] = []

    # Mode 2: Layered — devices/ + projects/ subdirectories
    devices_dir = config_dir / "devices"
    projects_dir = config_dir / "projects"

    if devices_dir.is_dir() and projects_dir.is_dir():
        import socket

        hostname = socket.gethostname().split(".")[0].lower()
        device_path = devices_dir / f"{hostname}.yaml"

        if not device_path.exists():
            warnings.append(f"No device config for hostname '{hostname}' in {devices_dir}")
        else:
            try:
                device = load_device_config(device_path)
            except ConfigError as e:
                raise click.ClickException(f"Invalid device config {device_path}: {e}") from e

            # Load each project listed in the device config
            for project_slug, override in device.projects.items():
                project_path = projects_dir / f"{project_slug}.yaml"
                if not project_path.exists():
                    warnings.append(
                        f"Project '{project_slug}' listed in device config "
                        f"but {project_path} not found"
                    )
                    continue
                try:
                    project_cfg = load_config(project_path)
                    merged = merge_device_project(device, project_cfg, override)
                    validate_config(merged)
                    valid_configs.append((project_path.resolve(), merged))
                except Exception as e:
                    warnings.append(f"Skipped project {project_slug}: {e}")

        if valid_configs:
            return (valid_configs, warnings)
        # Fall through to legacy if layered produced nothing

    # Mode 3: Legacy flat — each *.yaml in config dir is a full WorkflowConfig
    if config_dir.is_dir():
        for yaml_file in sorted(config_dir.glob("*.yaml")):
            try:
                cfg = load_config(yaml_file)
                validate_config(cfg)
                valid_configs.append((yaml_file.resolve(), cfg))
            except Exception as e:
                warnings.append(f"Skipped {yaml_file.name}: {e}")

    if not valid_configs:
        # Fallback to local workflow.yaml
        fallback = Path("workflow.yaml")
        if fallback.exists():
            try:
                cfg = load_config(fallback)
                validate_config(cfg)
                valid_configs.append((fallback.resolve(), cfg))
            except ConfigError as e:
                raise click.ClickException(f"Invalid workflow.yaml: {e}") from e
        else:
            raise click.ClickException(
                f"No configs found in {config_dir} and no local workflow.yaml"
            )

    return (valid_configs, warnings)


@main.command()
@click.option("--daemon", is_flag=True, help="Run in background, logs to disk only.")
@click.option("-c", "--config", "config_path", default=None, help="Path to workflow.yaml")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
@click.option("--max-agents", type=int, default=None, help="Global max concurrent agents across all projects.")
def start(daemon: bool, config_path: str | None, verbose: bool, max_agents: int | None) -> None:
    """Start the orchestrator (poll loop).

    With -c: single config mode (backward compatible).
    Without -c: scans config dir for all YAML files, launches multi-instance via Supervisor.
    """
    _load_local_env()
    _setup_logging(verbose)

    # Discover configs
    configs, warnings = _find_configs(config_path)

    if len(configs) == 1 and not max_agents:
        # Single config — use legacy path for full backward compat
        filepath, cfg = configs[0]

        from autosymph.linear_client import LinearClient
        from autosymph.orchestrator import Orchestrator
        from autosymph.state_machine import StateMachine
        from autosymph.workspace import WorkspaceManager

        linear = LinearClient(
            api_key=cfg.tracker.api_key,
            project_name=cfg.tracker.project,
            assignee_filter=cfg.tracker.assignee_filter,
        )
        sm = StateMachine(cfg)
        try:
            ws_mgr = WorkspaceManager(config=cfg.workspace, hooks=cfg.hooks)
        except RuntimeError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if not ws_mgr.repo.exists():
            click.echo(f"Error: workspace repo not found: {ws_mgr.repo}", err=True)
            click.echo("  Set AUTOSYMPH_REPO to your git repo path.", err=True)
            sys.exit(1)

        orch = Orchestrator(
            config=cfg,
            config_path=filepath,
            linear=linear,
            state_machine=sm,
            workspace_mgr=ws_mgr,
        )

        if not linear.is_configured:
            click.echo("Warning: LINEAR_API_KEY not set. Set it with:", err=True)
            click.echo("  export LINEAR_API_KEY=lin_api_...", err=True)
            click.echo("Orchestrator will start but skip polling until the key is available.\n", err=True)

        # Validate Linear workspace state if autoplan is configured.
        # Skipped when Linear isn't configured (dev/test) — startup still proceeds.
        if cfg.linear_states.autoplan and linear.is_configured:
            from autosymph.diagnostics import check_linear_states, ConfigError as DiagError
            try:
                asyncio.run(check_linear_states(cfg, linear))
            except DiagError as e:
                click.echo(f"Configuration error: {e}", err=True)
                asyncio.run(linear.close())
                sys.exit(1)

        machine = cfg.machine_name or "unknown"

        if daemon or verbose:
            click.echo(f"autosymph starting on {machine} — polling every {cfg.polling.interval_ms}ms")
            click.echo(f"  config:  {filepath}")
            click.echo(f"  repo:    {ws_mgr.repo}")
            click.echo(f"  states:  {', '.join(cfg.states.keys())}")
            click.echo(f"  max agents: {cfg.agent.max_concurrent_agents}")
            click.echo()
            try:
                asyncio.run(orch.run())
            except KeyboardInterrupt:
                click.echo("\nShutdown complete.")
            finally:
                asyncio.run(linear.close())
        else:
            _setup_logging_file_only(cfg.logging.resolved_log_root())
            from autosymph.tui import TUI
            tui = TUI(orchestrator=orch)
            try:
                asyncio.run(tui.run())
            except KeyboardInterrupt:
                pass
            finally:
                try:
                    asyncio.run(linear.close())
                except RuntimeError:
                    pass
            click.echo("Shutdown complete.")

    else:
        # Multi-instance mode via Supervisor
        from autosymph.supervisor import Supervisor
        from autosymph.tui import TUI

        sup = Supervisor(configs=configs, warnings=warnings, max_agents=max_agents)

        project_names = [cfg.project_slug for _, cfg in configs]
        click.echo(f"autosymph starting {len(configs)} projects: {', '.join(project_names)}")
        if warnings:
            for w in warnings:
                click.echo(f"  ⚠ {w}", err=True)
        if max_agents:
            click.echo(f"  global agent cap: {max_agents}")
        click.echo()

        if daemon or verbose:
            try:
                asyncio.run(sup.run())
            except KeyboardInterrupt:
                click.echo("\nShutdown complete.")
        else:
            _setup_logging_file_only()
            tui = TUI(supervisor=sup)
            try:
                asyncio.run(tui.run())
            except KeyboardInterrupt:
                pass
            click.echo("Shutdown complete.")


@main.command()
@click.option("--port", default=4200, help="Status API port (default: 4200)")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def status(port: int, as_json: bool) -> None:
    """Show one-shot status of active runners via the status API."""
    import json as _json
    import urllib.request
    import urllib.error

    url = f"http://127.0.0.1:{port}/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = _json.loads(resp.read().decode())
    except urllib.error.URLError:
        click.echo("autosymph not running (connection refused on port %d)" % port, err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error querying status API: {exc}", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(_json.dumps(data, indent=2, default=str))
        return

    # Formatted table output
    total_active = 0
    for slug, project in data.get("projects", {}).items():
        runners = project.get("runners", {})
        if runners:
            click.echo(f"\n{slug}")
            click.echo("  %-12s %-14s %-7s %8s %6s %8s  %s" % ("Issue", "State", "Runner", "Duration", "Turns", "Tokens", "Last Tool"))
            click.echo("  " + "-" * 80)
            for identifier, info in runners.items():
                dur_s = info.get("duration_s", 0)
                dur_str = f"{int(dur_s // 60)}m {int(dur_s % 60):02d}s"
                tokens = info.get("tokens", 0)
                tok_str = f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)
                click.echo(
                    "  %-12s %-14s %-7s %8s %6d %8s  %s"
                    % (
                        identifier,
                        info.get("state", "?"),
                        info.get("runner", "claude"),
                        dur_str,
                        info.get("turns", 0),
                        tok_str,
                        info.get("last_tool", "—"),
                    )
                )
                total_active += 1

    completed = data.get("total_completed_today", 0)
    failed = data.get("total_failed_today", 0)
    click.echo(f"\n{total_active} active | {completed} completed today | {failed} failed today")


@main.command("init")
def init_cmd() -> None:
    """Interactive onboarding wizard. Bootstraps a fresh device + project config.

    Walks through Linear API key validation, project selection, workflow-state
    creation (with explicit confirmation), repo-path collection, optional
    simulator + Braintrust setup, and writes the device + project YAMLs and
    local.env atomically. Designed for fresh-setup-only — re-running on a
    configured device exits with guidance.
    """
    from autosymph.wizard.runner import run_wizard
    sys.exit(run_wizard())


@main.group("config")
def config_cmd() -> None:
    """Configuration commands."""


@config_cmd.command("check")
@click.argument("path", default=None, required=False, type=click.Path(exists=False))
def config_check(path: str | None) -> None:
    """Validate a workflow.yaml file."""
    filepath = _find_config(path)
    if not filepath.exists():
        click.echo(f"Error: {filepath} not found.", err=True)
        sys.exit(1)

    try:
        cfg = load_config(filepath)
        validate_config(cfg)
        click.echo(f"OK — {filepath} is valid.")
        click.echo(f"  tracker:  {cfg.tracker.kind}")
        click.echo(f"  states:   {', '.join(cfg.states.keys())}")
        click.echo(
            f"  runners:  default={cfg.runners.default}, "
            f"available={', '.join(cfg.runners.available.keys())}"
        )
        click.echo(f"  polling:  {cfg.polling.interval_ms}ms")

        # Show per-state details
        for name, state in cfg.states.items():
            detail = f"    {name}: type={state.type}"
            if state.prompt:
                detail += f", prompt={state.prompt}"
            if state.runner:
                detail += f", runner={state.runner}"
            if state.transitions:
                extras = state.transitions.__pydantic_extra__ or {}
                targets = ", ".join(f"{k}→{v}" for k, v in extras.items())
                detail += f", transitions=[{targets}]"
            click.echo(detail)

    except ConfigError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# -- models commands --


@main.group("models")
def models_cmd() -> None:
    """Manage Claude model registry — check for stale references, refresh from API."""


@models_cmd.command("check")
@click.option("-c", "--config", "config_path", default=None, help="Path to a single workflow.yaml")
def models_check(config_path: str | None) -> None:
    """Fail if any loaded config references a known-stale Claude model id.

    Exit 0: every config is on a current model.
    Exit 1: at least one stale reference, listed as `path:state field old → new`.
    Intended for CI / pre-commit hooks.
    """
    configs, warnings = _find_configs(config_path)

    for warning in warnings:
        click.echo(f"warning: {warning}", err=True)

    if not configs:
        click.echo("No configs found.", err=True)
        sys.exit(1)

    any_stale = False
    for path, cfg in configs:
        refs = collect_stale_models(cfg)
        if not refs:
            continue
        any_stale = True
        click.echo(f"{path}:")
        for ref in refs:
            click.echo(f"  {ref.field}  {ref.model_id} → {ref.suggested}")

    if any_stale:
        click.echo(
            "\nStale model references found. "
            "Run `autosymph models refresh --apply` to update.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"OK — {len(configs)} config(s) reference current models only.")


def _fetch_models_response(api_key: str) -> dict:
    """Call Anthropic GET /v1/models. Isolated for monkeypatching in tests."""
    import httpx

    response = httpx.get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()


@models_cmd.command("refresh")
@click.option("--apply", "apply_changes", is_flag=True, help="Rewrite models.py (default: dry-run).")
@click.option("--api-key", envvar="ANTHROPIC_API_KEY", default=None,
              help="Anthropic API key (else $ANTHROPIC_API_KEY).")
def models_refresh(apply_changes: bool, api_key: str | None) -> None:
    """Query Anthropic /v1/models, bump the registry to the newest models per family.

    Default is dry-run: prints the diff. Pass --apply to rewrite models.py.
    Intended to be invoked by a scheduled agent that opens a PR if the diff is non-empty.
    """
    if not api_key:
        click.echo("ANTHROPIC_API_KEY not set", err=True)
        sys.exit(1)

    try:
        api_response = _fetch_models_response(api_key)
    except Exception as e:
        click.echo(f"Failed to fetch /v1/models: {e}", err=True)
        sys.exit(1)

    api_latest = pick_latest_per_family(api_response)
    if not api_latest:
        click.echo("API returned no recognizable models — aborting.", err=True)
        sys.exit(1)

    update = compute_registry_update(
        current_latest=LATEST,
        current_stale=KNOWN_STALE,
        api_latest=api_latest,
    )

    if not update.changed:
        click.echo("OK — registry already current; no change.")
        return

    click.echo("Registry bump available:")
    for family in update.bumped_families:
        old = LATEST.get(family, "<none>")
        new = update.new_latest[family]
        click.echo(f"  {family}: {old} → {new}")

    if not apply_changes:
        click.echo("\nDry-run. Pass --apply to rewrite models.py.")
        return

    target = Path(__file__).parent / "models.py"
    write_models_py(target, latest=update.new_latest, known_stale=update.new_stale)
    click.echo(f"\nWrote {target}")


@main.command()
@click.argument("issue_id")
@click.option("-c", "--config", "config_path", default=None, help="Path to workflow.yaml")
@click.option("--project", default=None, help="Project slug for namespaced log lookup.")
def logs(issue_id: str, config_path: str | None, project: str | None) -> None:
    """Show logs for an issue — orchestrator events + run summaries."""
    import json as _json
    from autosymph.logging.events import EventLog

    # Resolve log_root from config if available, else use default
    try:
        filepath = _find_config(config_path)
        cfg = load_config(filepath)
        log_root = cfg.logging.resolved_log_root()
    except Exception:
        log_root = Path("~/.autosymph/logs").expanduser().resolve()
    issue_upper = issue_id.upper()
    issue_lower = issue_id.lower()

    # Orchestrator events
    event_log = EventLog(log_root)
    events = event_log.read_recent(issue_upper)
    if events:
        click.echo("Orchestrator events:")
        for line in events:
            click.echo(f"  {line}")
        click.echo()

    # Run summaries — namespace by project if provided
    if project:
        log_dir = log_root / project / issue_lower
    else:
        log_dir = log_root / issue_lower
    if not log_dir.exists():
        if not events:
            click.echo(f"No logs found for {issue_id}")
            sys.exit(1)
        return

    metas = sorted(log_dir.glob("*.meta.json"))
    if metas:
        click.echo("Runs:")
        for meta_path in metas:
            meta = _json.loads(meta_path.read_text())
            outcome = "OK" if meta["success"] else "FAIL"
            tokens = meta.get("token_usage", {})
            token_str = ""
            if tokens:
                parts = []
                if tokens.get("input_tokens"):
                    parts.append(f"{tokens['input_tokens']:,} in")
                if tokens.get("output_tokens"):
                    parts.append(f"{tokens['output_tokens']:,} out")
                token_str = f"  tokens: {', '.join(parts)}" if parts else ""

            session = meta.get("session_id", "")[:8]
            runner = meta.get("runner", "claude")
            ndjson_path = meta_path.with_suffix(".ndjson")
            click.echo(
                f"  {meta['state']} run {meta['run']}  "
                f"{outcome}  {meta['duration_seconds']}s{token_str}  "
                f"runner={runner} session={session}"
            )
            click.echo(f"    {ndjson_path}")
            if meta.get("error"):
                click.echo(f"    error: {meta['error']}")


@main.command()
@click.argument("issue_id")
def tail(issue_id: str) -> None:
    """Live-tail a running agent."""
    click.echo(f"Tailing {issue_id} — not yet implemented.")


@main.command()
@click.argument("issue_id")
def dispatch(issue_id: str) -> None:
    """Manually dispatch an issue to an agent."""
    click.echo(f"Dispatching {issue_id} — not yet implemented.")
