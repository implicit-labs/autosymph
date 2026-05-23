"""``run_wizard`` orchestrates the step pipeline.

The Click command in ``cli.py`` is the only public caller. Top-level catches
each wizard exception type and produces the matching exit message + exit
code.
"""
from __future__ import annotations

import logging

import click

from autosymph.linear_client import LinearClient
from autosymph.wizard import steps
from autosymph.wizard.errors import (
    WizardAborted,
    WizardAlreadyConfigured,
    WizardLinearMutationFailed,
    WizardPromptsRootMissing,
)
from autosymph.wizard.prompter import ClickPrompter, Prompter
from autosymph.wizard.state import WizardState

logger = logging.getLogger(__name__)


def run_wizard(prompter: Prompter | None = None) -> int:
    """Execute the wizard. Returns the desired process exit code.

    ``prompter`` defaults to ``ClickPrompter``. Tests pass a ``ScriptedPrompter``.
    """
    p: Prompter = prompter or ClickPrompter()
    state = WizardState()

    try:
        with steps.step_acquire_lock(state):
            steps.step_detect_existing_config(state)
            steps.step_collect_linear_key(state, p)
            # Build the live LinearClient now that we have a valid key.
            assert state.linear_api_key is not None
            linear = LinearClient(
                api_key=state.linear_api_key,
                project_name="__wizard_pending__",  # overridden after select
            )
            try:
                steps.step_select_project(state, p, linear)
                # The select step set state.project_id; rebind project_name for
                # the client so downstream resolves consistently.
                if state.project_name:
                    linear.project_name = state.project_name
                steps.step_fetch_workspace(state, linear)
                steps.step_compute_state_diff(state)
                steps.step_resolve_near_matches(state, p)
                steps.step_collect_repo_path(state, p)
                steps.step_detect_simulators(state, p)
                steps.step_choose_template(state, p)
                steps.step_resolve_prompts_root(state)
                steps.step_collect_braintrust(state, p)
                steps.step_build_plan(state)
                steps.step_present_combined_preview(state, p)
                steps.step_execute_linear_mutations(state, linear)
                steps.step_atomic_writes(state)
                steps.step_verify_via_find_configs(state)
                steps.step_print_next_steps(state)
            finally:
                # Close the httpx clients that back LinearClient. Each step
                # ran its own asyncio.run, so LinearClient may hold one
                # client per (now-dead) loop. ``close()`` drops them safely.
                import asyncio
                try:
                    asyncio.run(linear.close())
                except Exception:
                    pass
    except WizardAlreadyConfigured as exc:
        click.echo(
            click.style(
                f"Existing config detected at {exc.trigger_path}.",
                fg="yellow",
            ),
            err=True,
        )
        click.echo(f"  reason: {exc.reason}\n", err=True)
        if state.project_slug:
            click.echo(
                f"  to validate: uv run autosymph config check "
                f"{steps._config_dir()}/projects/{state.project_slug}.yaml",
                err=True,
            )
        click.echo(
            f"  to add a new project: edit {steps._config_dir()}/devices/"
            f"{steps._hostname_slug()}.yaml + create projects/<new-slug>.yaml",
            err=True,
        )
        click.echo(
            f"  to restart from scratch: rm -rf {steps._config_dir()} && uv run autosymph init",
            err=True,
        )
        return 1
    except WizardAborted as exc:
        click.echo(
            click.style(f"Wizard aborted at {exc.step}. No changes made — re-run when ready.",
                        fg="yellow"),
            err=True,
        )
        return 0
    except WizardPromptsRootMissing as exc:
        click.echo(
            click.style(
                f"autosymph package missing prompts/ at {exc.resolved_path}.",
                fg="red",
            ),
            err=True,
        )
        click.echo(f"  missing files: {exc.missing_files}", err=True)
        click.echo(
            "  install autosymph from source (e.g. uv sync) and re-run.",
            err=True,
        )
        return 1
    except WizardLinearMutationFailed as exc:
        click.echo(
            click.style(
                f"Linear mutation failed on {exc.failed_state!r}: {exc.cause}",
                fg="red",
            ),
            err=True,
        )
        click.echo(
            f"  succeeded states (recorded in wizard-mutations.log): {exc.succeeded_states}",
            err=True,
        )
        click.echo(
            "  no user-config files were written. Re-run autosymph init — already-created "
            "states will be detected and skipped.",
            err=True,
        )
        return 1
    except KeyboardInterrupt:
        click.echo(
            click.style("Interrupted. No partial config files left on disk.", fg="yellow"),
            err=True,
        )
        return 130

    click.echo(click.style("autosymph init: success", fg="green"), err=True)
    return 0
