from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from autosymph.factory_cli import factory


def test_cli_authors_audits_and_compiles_factory(tmp_path: Path):
    runner = CliRunner()
    root = tmp_path / "factory"
    output = tmp_path / "compiled.yaml"

    assert runner.invoke(factory, ["init", str(root), "cli-sample"]).exit_code == 0
    assert runner.invoke(
        factory,
        [
            "create-state",
            str(root),
            "done",
            "--kind",
            "terminal",
            "--description",
            "Record acceptance.",
        ],
    ).exit_code == 0
    assert runner.invoke(
        factory,
        [
            "create-state",
            str(root),
            "implement",
            "--kind",
            "agent",
            "--description",
            "Perform bounded work.",
            "--runner",
            "codex",
            "--transition",
            "complete=done",
        ],
    ).exit_code == 0

    audit = runner.invoke(factory, ["audit", str(root)])
    compiled = runner.invoke(
        factory,
        [
            "compile",
            str(root),
            "--output",
            str(output),
            "--project",
            "CLI Sample",
            "--runner",
            "codex",
        ],
    )

    assert audit.exit_code == 0
    assert "factory audit passed" in audit.output
    assert compiled.exit_code == 0
    assert output.is_file()
    assert "implement:" in output.read_text()


def test_cli_audit_fails_on_broken_transition(tmp_path: Path):
    runner = CliRunner()
    root = tmp_path / "factory"
    runner.invoke(factory, ["init", str(root), "broken-sample"])
    runner.invoke(
        factory,
        [
            "create-state",
            str(root),
            "done",
            "--kind",
            "terminal",
            "--description",
            "Record acceptance.",
        ],
    )
    runner.invoke(
        factory,
        [
            "create-state",
            str(root),
            "implement",
            "--kind",
            "agent",
            "--description",
            "Broken work.",
            "--transition",
            "complete=missing",
        ],
    )

    audit = runner.invoke(factory, ["audit", str(root), "--scope", "transitions"])

    assert audit.exit_code == 1
    assert "MISSING_TARGET" in audit.output
    assert "NO_PATH_TO_TERMINAL" in audit.output
