"""Durability, recovery, export, and redaction tests for the local ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autosymph.ledger import (
    SCHEMA_VERSION,
    LedgerError,
    LedgerMigrationError,
    SQLiteLedger,
)
from autosymph.logging.stream import LogStream


def _allocate(ledger: SQLiteLedger, *, state: str = "implement"):
    return ledger.allocate_run(
        project_slug="autosymph",
        issue_id="linear-1",
        issue_identifier="IMP-552",
        state=state,
        runner="claude",
        metadata={"model": "sonnet"},
    )


def test_schema_contains_foundation_tables_and_uses_wal(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    tables = {
        row[0]
        for row in ledger._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert ledger.schema_version == SCHEMA_VERSION
    assert ledger._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert {
        "runs",
        "events",
        "capability_results",
        "failures",
        "completion_receipts",
        "transition_decisions",
        "leases",
        "outbox",
        "external_receipts",
        "incidents",
        "repair_phase_requests",
        "repair_receipts",
    } <= tables
    ledger.close()


def test_attempt_allocation_and_uuid7_are_durable(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with SQLiteLedger(path) as ledger:
        first = _allocate(ledger)
        second = _allocate(ledger)

    assert uuid.UUID(first.run_id).version == 7
    assert (first.attempt_number, second.attempt_number) == (1, 2)

    with SQLiteLedger(path) as restarted:
        third = _allocate(restarted)
        assert third.attempt_number == 3


def test_concurrent_connections_allocate_unique_attempts(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    first = SQLiteLedger(path)
    second = SQLiteLedger(path)
    barrier = threading.Barrier(2)

    def allocate(ledger: SQLiteLedger) -> int:
        barrier.wait(timeout=2)
        return _allocate(ledger).attempt_number

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(allocate, (first, second)))

    first.close()
    second.close()
    assert sorted(attempts) == [1, 2]


def test_terminal_event_and_run_update_are_idempotent(tmp_path: Path) -> None:
    with SQLiteLedger(tmp_path / "ledger.db") as ledger:
        run = _allocate(ledger)
        first = ledger.record_terminal(
            run_id=run.run_id,
            event_type="run_completed",
            status="completed",
            payload={"exit_code": 0},
        )
        second = ledger.record_terminal(
            run_id=run.run_id,
            event_type="run_failed",
            status="failed",
            payload={"exit_code": 1},
        )
        stored = ledger.get_run(run.run_id)
        terminal_events = [
            row for row in ledger.list_events(run_id=run.run_id)
            if row["idempotency_key"] == f"run-terminal:{run.run_id}"
        ]

    assert second.event_id == first.event_id
    assert stored is not None
    assert stored["status"] == "completed"
    assert stored["terminal_event_id"] == first.event_id
    assert len(terminal_events) == 1


def test_restart_reconciles_open_run_exactly_once(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    raw_log = tmp_path / "interrupted.ndjson"
    raw_log.write_text('{"partial":true}\n', encoding="utf-8")
    with SQLiteLedger(path) as ledger:
        run = _allocate(ledger)
        ledger.mark_run_started(run.run_id, raw_log_path=raw_log)

    with SQLiteLedger(path) as restarted:
        assert restarted.reconcile_open_runs(project_slug="autosymph") == [run.run_id]
        assert restarted.reconcile_open_runs(project_slug="autosymph") == []
        stored = restarted.get_run(run.run_id)
        events = restarted.list_events(run_id=run.run_id)

    assert stored is not None
    assert stored["status"] == "interrupted"
    assert stored["raw_log_sha256"] == hashlib.sha256(raw_log.read_bytes()).hexdigest()
    assert [row["event_type"] for row in events].count("run_interrupted") == 1


def test_transition_decision_and_event_are_idempotent(tmp_path: Path) -> None:
    with SQLiteLedger(tmp_path / "ledger.db") as ledger:
        run = _allocate(ledger)
        first = ledger.record_transition(
            run_id=run.run_id,
            from_state="implement",
            to_state="verify",
            signal="complete",
        )
        second = ledger.record_transition(
            run_id=run.run_id,
            from_state="implement",
            to_state="verify",
            signal="complete",
        )
        decision_count = ledger._connection.execute(
            "SELECT COUNT(*) FROM transition_decisions WHERE run_id = ?", (run.run_id,)
        ).fetchone()[0]

    assert first.event_id == second.event_id
    assert decision_count == 1


def test_terminal_run_event_and_transition_roll_back_together(tmp_path: Path) -> None:
    with SQLiteLedger(tmp_path / "ledger.db") as ledger:
        run = _allocate(ledger)
        ledger._connection.execute(
            """
            CREATE TRIGGER reject_decision BEFORE INSERT ON transition_decisions
            BEGIN SELECT RAISE(ABORT, 'simulated decision crash'); END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="simulated decision crash"):
            ledger.commit_run_transition(
                run_id=run.run_id,
                event_type="run_completed",
                status="completed",
                from_state="implement",
                to_state="verify",
                signal="complete",
            )

        stored = ledger.get_run(run.run_id)
        terminal_count = ledger._connection.execute(
            "SELECT COUNT(*) FROM events WHERE idempotency_key = ?",
            (f"run-terminal:{run.run_id}",),
        ).fetchone()[0]

    assert stored is not None
    assert stored["status"] == "allocated"
    assert stored["terminal_event_id"] is None
    assert terminal_count == 0


def test_raw_log_is_stored_by_path_hash_and_size(tmp_path: Path) -> None:
    raw_log = tmp_path / "run.ndjson"
    raw_log.write_text('{"type":"assistant"}\n', encoding="utf-8")
    with SQLiteLedger(tmp_path / "ledger.db") as ledger:
        run = _allocate(ledger)
        ledger.attach_raw_log(run.run_id, raw_log)
        stored = ledger.get_run(run.run_id)

    assert stored is not None
    assert stored["raw_log_path"] == str(raw_log.resolve())
    assert stored["raw_log_bytes"] == raw_log.stat().st_size
    assert stored["raw_log_sha256"] == hashlib.sha256(raw_log.read_bytes()).hexdigest()


def test_existing_database_is_backed_up_before_migration(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    connection = sqlite3.connect(path)
    fixture = Path(__file__).parent / "fixtures" / "migrations" / "v0.sql"
    connection.executescript(fixture.read_text(encoding="utf-8"))
    connection.commit()
    connection.close()

    with SQLiteLedger(path) as ledger:
        assert ledger.schema_version == SCHEMA_VERSION

    backups = list(tmp_path.glob("ledger.db.backup-*") )
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    assert backup.execute("SELECT value FROM legacy_fixture").fetchone()[0] == "preserve-me"
    backup.close()


def test_newer_schema_can_be_opened_read_only_for_recovery(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE future_fixture(value TEXT)")
    connection.execute("PRAGMA user_version=999")
    connection.commit()
    connection.close()

    with pytest.raises(LedgerMigrationError):
        SQLiteLedger(path)

    with SQLiteLedger(path, read_only=True) as recovered:
        assert recovered.schema_version == 999
        with pytest.raises(LedgerError, match="read-only"):
            _allocate(recovered)


def test_jsonl_export_manifest_counts_hash_and_redacts_secrets(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    output = tmp_path / "export.jsonl"
    secret = "sk-ant-this-must-never-appear"
    with SQLiteLedger(path) as ledger:
        run = _allocate(ledger)
        ledger.append_event(
            event_type="provider_observed",
            project_slug="autosymph",
            issue_id="linear-1",
            issue_identifier="IMP-552",
            run_id=run.run_id,
            payload={"api_key": secret, "detail": f"Authorization: Bearer {secret}"},
        )
        manifest = ledger.export_jsonl(output)

    lines = output.read_text(encoding="utf-8").splitlines()
    exported_body = "\n".join(lines[1:]) + "\n"
    parsed_manifest = json.loads(lines[0])["data"]
    assert parsed_manifest["row_counts"] == manifest.row_counts
    assert manifest.row_counts["runs"] == 1
    assert manifest.row_counts["events"] == 2
    assert hashlib.sha256(exported_body.encode()).hexdigest() == manifest.content_sha256
    assert secret not in output.read_text(encoding="utf-8")
    assert "[REDACTED]" in output.read_text(encoding="utf-8")


def test_log_metadata_redacts_secret_bearing_errors(tmp_path: Path) -> None:
    stream = LogStream(tmp_path)
    path = stream.open("imp-552", "implement", 1)
    meta_path = stream.write_meta(
        path,
        issue_id="IMP-552",
        state="implement",
        run_number=1,
        success=False,
        exit_code=1,
        duration_seconds=0.1,
        token_usage={},
        session_id=None,
        error="provider failed password=hunter2 api_key=topsecret",
    )

    content = meta_path.read_text(encoding="utf-8")
    assert "hunter2" not in content
    assert "topsecret" not in content
    assert content.count("[REDACTED]") == 2


def test_parquet_export_manifest_and_redaction(tmp_path: Path) -> None:
    pyarrow = pytest.importorskip("pyarrow")
    import pyarrow.parquet as parquet

    output = tmp_path / "events.parquet"
    with SQLiteLedger(tmp_path / "ledger.db") as ledger:
        run = _allocate(ledger)
        ledger.append_event(
            event_type="provider_observed",
            project_slug="autosymph",
            issue_id="linear-1",
            run_id=run.run_id,
            payload={"password": "must-not-export"},
        )
        manifest = ledger.export_parquet(output)

    table = parquet.read_table(output)
    assert pyarrow is not None
    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.row_counts == {"events": 2}
    assert manifest.content_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert "must-not-export" not in json.dumps(table.to_pylist())
