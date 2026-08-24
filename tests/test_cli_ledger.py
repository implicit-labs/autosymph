"""Operator-facing ledger check and export commands."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from autosymph.cli import main
from autosymph.ledger import SQLiteLedger


def _seed(path: Path) -> None:
    with SQLiteLedger(path) as ledger:
        ledger.allocate_run(
            project_slug="autosymph",
            issue_id="linear-1",
            issue_identifier="IMP-552",
            state="implement",
            runner="claude",
        )


def test_ledger_check_reports_integrity_and_schema(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    _seed(database)

    result = CliRunner().invoke(main, ["ledger", "check", "--database", str(database)])

    assert result.exit_code == 0, result.output
    assert "integrity: ok" in result.output
    assert "schema:    1" in result.output


def test_ledger_export_writes_manifest_prefixed_jsonl(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    output = tmp_path / "ledger.jsonl"
    _seed(database)

    result = CliRunner().invoke(
        main,
        [
            "ledger",
            "export",
            "--database",
            str(database),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert manifest["record_type"] == "manifest"
    assert manifest["data"]["row_counts"]["runs"] == 1
    assert "sha256:" in result.output
