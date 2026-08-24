"""Crash-safe local operational ledger for Autosymph.

The ledger is the durable source of truth for attempts and factory events.
Human-readable logs and remote tracing are downstream projections and may fail
without invalidating a committed run record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Protocol, runtime_checkable


SCHEMA_VERSION = 1
ReliabilityMode = Literal["observe", "enforce_verify", "enforce_all"]


class LedgerError(RuntimeError):
    """Base class for durable ledger errors."""


class LedgerIntegrityError(LedgerError):
    """Raised when SQLite reports an integrity failure."""


class LedgerMigrationError(LedgerError):
    """Raised when a migration cannot be completed safely."""


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    project_slug: str
    issue_id: str
    issue_identifier: str
    state: str
    attempt_number: int
    runner: str
    status: str
    created_at: str

    @property
    def attempt(self) -> int:
        """Compatibility name used by the agent lifecycle."""
        return self.attempt_number


RunAllocation = RunRecord


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    run_id: str | None
    event_type: str
    occurred_at: str
    payload_sha256: str


@dataclass(frozen=True)
class ExportManifest:
    schema_version: int
    format: str
    created_at: str
    event_range: dict[str, str | None]
    row_counts: dict[str, int]
    content_sha256: str


@runtime_checkable
class Ledger(Protocol):
    """Narrow contract consumed by the orchestrator and later reliability layers."""

    read_only: bool

    def allocate_run(
        self,
        *,
        project_slug: str,
        issue_id: str,
        issue_identifier: str,
        state: str,
        runner: str,
        metadata: dict[str, Any] | None = None,
    ) -> RunRecord: ...

    def mark_run_started(self, run_id: str, *, raw_log_path: Path | None = None) -> None: ...

    def append_event(
        self,
        *,
        event_type: str,
        project_slug: str,
        issue_id: str,
        issue_identifier: str | None = None,
        run_id: str | None = None,
        producer: str = "autosymph:runtime",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> EventRecord: ...

    def finalize_run(
        self,
        run_id: str,
        *,
        event_type: str,
        process_outcome: str,
        semantic_outcome: str,
        payload: dict[str, Any] | None = None,
        raw_log_path: Path | None = None,
    ) -> EventRecord: ...

    def record_terminal(
        self,
        *,
        run_id: str,
        event_type: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> EventRecord: ...

    def attach_raw_log(self, run_id: str, path: Path) -> None: ...

    def record_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str | None,
        signal: str,
    ) -> EventRecord: ...

    def commit_run_transition(
        self,
        *,
        run_id: str,
        event_type: str,
        status: str,
        from_state: str,
        to_state: str | None,
        signal: str,
        payload: dict[str, Any] | None = None,
    ) -> EventRecord: ...

    def reconcile_open_runs(self, *, project_slug: str | None = None) -> list[str]: ...

    def close(self) -> None: ...


_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:api_?key|token|secret|password|passwd|credential|authorization|cookie)(?:$|_)",
    re.IGNORECASE,
)
_SAFE_SENSITIVE_SUFFIXES = (
    "_present",
    "_version",
    "_ref",
    "_name",
    "_id",
    "_sha",
    "_sha256",
    "_hash",
    "_usage",
)
_TOKEN_PATTERNS = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\blin_api_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}\b"),
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"),
    re.compile(
        r"(?i)\b([A-Z][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)"
        r"[A-Z0-9_]*=)[^\s]+"
    ),
    re.compile(
        r"(?i)\b((?:api[_-]?key|token|secret|password|credential)\s*[=:]\s*)"
        r"[^\s,;]+"
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid7() -> str:
    """Return a standards-shaped UUIDv7 using Python 3.11 primitives.

    Python 3.11 has no uuid.uuid7(). The first 48 bits carry Unix epoch
    milliseconds, followed by the v7 marker, random bits, and RFC 4122 variant.
    """
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_bits = int.from_bytes(os.urandom(10), "big")
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= (random_bits & ((1 << 12) - 1)) << 64
    value |= 0b10 << 62
    value |= (random_bits >> 12) & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def _redact_string(value: str) -> str:
    if value.startswith("$") and value[1:].replace("_", "").isalnum():
        return value
    result = value
    for pattern in _TOKEN_PATTERNS:
        if pattern.groups:
            result = pattern.sub(r"\1[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def redact_payload(value: Any, *, key: str | None = None) -> Any:
    """Recursively remove secret material before serialization.

    Presence, version, reference, identifier, and hash metadata remain useful
    and are explicitly allowed even when their key contains a sensitive word.
    """
    if key and _SENSITIVE_KEY.search(key) and not key.lower().endswith(
        _SAFE_SENSITIVE_SUFFIXES
    ):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact_payload(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_string(str(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        redact_payload(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _digest(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project_slug TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    issue_identifier TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    runner TEXT NOT NULL,
    status TEXT NOT NULL,
    process_outcome TEXT,
    semantic_outcome TEXT,
    contract_sha256 TEXT,
    config_sha256 TEXT,
    prompt_sha256 TEXT,
    state_package_sha256 TEXT,
    acceptance_contract_sha256 TEXT,
    git_base_sha TEXT,
    raw_log_path TEXT,
    raw_log_sha256 TEXT,
    raw_log_bytes INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    terminal_event_id TEXT,
    UNIQUE(project_slug, issue_id, state, attempt_number)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(run_id),
    project_slug TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    issue_identifier TEXT,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    producer TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS events_run_id_idx ON events(run_id);
CREATE INDEX IF NOT EXISTS events_issue_idx ON events(project_slug, issue_id, occurred_at);
CREATE INDEX IF NOT EXISTS events_type_idx ON events(event_type, occurred_at);

CREATE TABLE IF NOT EXISTS capability_results (
    capability_result_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    capability TEXT NOT NULL,
    required INTEGER NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failures (
    failure_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(run_id),
    fingerprint_id TEXT NOT NULL,
    category TEXT NOT NULL,
    retry_class TEXT NOT NULL,
    evidence_ref TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS failures_fingerprint_idx ON failures(fingerprint_id, created_at);

CREATE TABLE IF NOT EXISTS completion_receipts (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    schema_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transition_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    from_state TEXT NOT NULL,
    to_state TEXT,
    accepted INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    fence_token INTEGER NOT NULL,
    status TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    released_at TEXT,
    UNIQUE(scope, scope_key, fence_token)
);

CREATE TABLE IF NOT EXISTS outbox (
    effect_id TEXT PRIMARY KEY,
    effect_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_receipts (
    external_receipt_id TEXT PRIMARY KEY,
    effect_id TEXT REFERENCES outbox(effect_id),
    provider TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(provider, remote_id)
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    incident_key TEXT NOT NULL UNIQUE,
    fingerprint_id TEXT NOT NULL,
    project_scope TEXT NOT NULL,
    component TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    incident_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    run_id TEXT REFERENCES runs(run_id),
    release_json TEXT NOT NULL,
    breadcrumbs_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE(incident_id, event_id)
);

CREATE TABLE IF NOT EXISTS incident_activity (
    activity_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    activity_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_releases (
    incident_release_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    role TEXT NOT NULL,
    release_json TEXT NOT NULL,
    release_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(incident_id, role, release_sha256)
);

CREATE TABLE IF NOT EXISTS incident_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    incident_id TEXT REFERENCES incidents(incident_id),
    sink TEXT NOT NULL,
    rule_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repairs (
    repair_id TEXT PRIMARY KEY,
    incident_id TEXT REFERENCES incidents(incident_id),
    fingerprint_id TEXT NOT NULL,
    remediation_revision TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_class TEXT NOT NULL,
    repair_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repair_phase_requests (
    phase_request_id TEXT PRIMARY KEY,
    repair_id TEXT NOT NULL REFERENCES repairs(repair_id),
    phase TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repair_receipts (
    repair_receipt_id TEXT PRIMARY KEY,
    repair_id TEXT NOT NULL REFERENCES repairs(repair_id),
    phase TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(repair_id, phase, receipt_sha256)
);
"""

_MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "initial_reliability_ledger", _MIGRATION_1),
)

_EXPORT_TABLES = (
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
    "incident_occurrences",
    "incident_activity",
    "incident_releases",
    "incident_subscriptions",
    "repairs",
    "repair_phase_requests",
    "repair_receipts",
)


class SQLiteLedger:
    """SQLite WAL implementation of the Autosymph ledger contract."""

    def __init__(
        self,
        path: Path | str,
        *,
        read_only: bool = False,
        backup_before_migrate: bool = True,
    ) -> None:
        in_memory = str(path) == ":memory:"
        self.path = Path(":memory:") if in_memory else Path(path).expanduser().resolve()
        self.read_only = read_only
        self._lock = threading.RLock()
        if read_only:
            if in_memory:
                raise LedgerError("An in-memory ledger cannot be opened read-only")
            if not self.path.exists():
                raise LedgerError(f"Ledger does not exist: {self.path}")
            uri = f"file:{self.path}?mode=ro"
            self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            if not in_memory:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = False if in_memory else self.path.exists()
            target: str | Path = ":memory:" if in_memory else self.path
            self._connection = sqlite3.connect(target, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            current_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                self._connection.close()
                raise LedgerMigrationError(
                    f"Ledger schema {current_version} is newer than supported {SCHEMA_VERSION}"
                )
            if current_version < SCHEMA_VERSION:
                if existed and backup_before_migrate:
                    self._backup()
                self._migrate(current_version)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self.check_integrity()

    @property
    def schema_version(self) -> int:
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def _backup(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.path.with_name(f"{self.path.name}.backup-{timestamp}")
        try:
            backup_connection = sqlite3.connect(target)
            with backup_connection:
                self._connection.backup(backup_connection)
            backup_connection.close()
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise LedgerMigrationError("Failed to back up ledger before migration") from exc
        return target

    def backup(self, target: Path | None = None) -> Path:
        """Create an online SQLite backup without copying WAL files directly."""
        if target is None:
            return self._backup()
        resolved = target.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        backup_connection = sqlite3.connect(resolved)
        try:
            with backup_connection:
                self._connection.backup(backup_connection)
        finally:
            backup_connection.close()
        return resolved

    def _migrate(self, current_version: int) -> None:
        try:
            with self._connection:
                for version, name, sql in _MIGRATIONS:
                    if version <= current_version:
                        continue
                    self._connection.executescript(sql)
                    self._connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, _utc_now()),
                    )
                    self._connection.execute(f"PRAGMA user_version={version}")
        except Exception as exc:
            raise LedgerMigrationError("Ledger migration failed") from exc

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise LedgerError("Ledger is open read-only")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def check_integrity(self) -> str:
        row = self._connection.execute("PRAGMA quick_check").fetchone()
        result = str(row[0]) if row else "missing result"
        if result != "ok":
            raise LedgerIntegrityError(f"Ledger integrity check failed: {result}")
        return result

    def allocate_run(
        self,
        *,
        project_slug: str,
        issue_id: str,
        issue_identifier: str,
        state: str,
        runner: str,
        metadata: dict[str, Any] | None = None,
    ) -> RunRecord:
        now = _utc_now()
        run_id = _uuid7()
        metadata_json = _canonical_json(metadata or {})
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM runs WHERE project_slug = ? AND issue_id = ? AND state = ?
                """,
                (project_slug, issue_id, state),
            ).fetchone()
            attempt_number = int(row[0])
            conn.execute(
                """
                INSERT INTO runs(
                    run_id, project_slug, issue_id, issue_identifier, state,
                    attempt_number, runner, status, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'allocated', ?, ?)
                """,
                (
                    run_id,
                    project_slug,
                    issue_id,
                    issue_identifier,
                    state,
                    attempt_number,
                    runner,
                    metadata_json,
                    now,
                ),
            )
            self._append_event(
                conn,
                event_type="run_allocated",
                project_slug=project_slug,
                issue_id=issue_id,
                issue_identifier=issue_identifier,
                run_id=run_id,
                producer="autosymph:ledger:v1",
                payload={"state": state, "attempt_number": attempt_number, "runner": runner},
                idempotency_key=f"run-allocated:{run_id}",
            )
        return RunRecord(
            run_id=run_id,
            project_slug=project_slug,
            issue_id=issue_id,
            issue_identifier=issue_identifier,
            state=state,
            attempt_number=attempt_number,
            runner=runner,
            status="allocated",
            created_at=now,
        )

    def mark_run_started(self, run_id: str, *, raw_log_path: Path | None = None) -> None:
        with self.transaction() as conn:
            run = self._require_run(conn, run_id)
            if run["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return
            conn.execute(
                "UPDATE runs SET status = 'running', started_at = ?, raw_log_path = ? WHERE run_id = ?",
                (_utc_now(), str(raw_log_path) if raw_log_path else None, run_id),
            )
            self._append_event(
                conn,
                event_type="run_started",
                project_slug=run["project_slug"],
                issue_id=run["issue_id"],
                issue_identifier=run["issue_identifier"],
                run_id=run_id,
                producer="autosymph:ledger:v1",
                payload={"raw_log_path": str(raw_log_path) if raw_log_path else None},
                idempotency_key=f"run-started:{run_id}",
            )

    def append_event(
        self,
        *,
        event_type: str,
        project_slug: str,
        issue_id: str,
        issue_identifier: str | None = None,
        run_id: str | None = None,
        producer: str = "autosymph:runtime",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> EventRecord:
        with self.transaction() as conn:
            return self._append_event(
                conn,
                event_type=event_type,
                project_slug=project_slug,
                issue_id=issue_id,
                issue_identifier=issue_identifier,
                run_id=run_id,
                producer=producer,
                payload=payload or {},
                idempotency_key=idempotency_key,
            )

    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        project_slug: str,
        issue_id: str,
        issue_identifier: str | None,
        run_id: str | None,
        producer: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> EventRecord:
        if idempotency_key:
            existing = conn.execute(
                "SELECT * FROM events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return EventRecord(
                    event_id=existing["event_id"],
                    run_id=existing["run_id"],
                    event_type=existing["event_type"],
                    occurred_at=existing["occurred_at"],
                    payload_sha256=existing["payload_sha256"],
                )
        event_id = _uuid7()
        occurred_at = _utc_now()
        payload_json = _canonical_json(payload)
        payload_sha256 = _digest(payload_json)
        conn.execute(
            """
            INSERT INTO events(
                event_id, run_id, project_slug, issue_id, issue_identifier,
                event_type, occurred_at, producer, payload_json, payload_sha256,
                idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                project_slug,
                issue_id,
                issue_identifier,
                event_type,
                occurred_at,
                producer,
                payload_json,
                payload_sha256,
                idempotency_key,
            ),
        )
        return EventRecord(event_id, run_id, event_type, occurred_at, payload_sha256)

    def finalize_run(
        self,
        run_id: str,
        *,
        event_type: str,
        process_outcome: str,
        semantic_outcome: str,
        payload: dict[str, Any] | None = None,
        raw_log_path: Path | None = None,
    ) -> EventRecord:
        """Atomically append a terminal event and terminalize its run.

        Repeated calls are idempotent and return the originally committed
        terminal event rather than changing a previously observed outcome.
        """
        log_path = raw_log_path
        if log_path is None:
            existing_run = self.get_run(run_id)
            if existing_run is not None and existing_run["raw_log_path"]:
                log_path = Path(existing_run["raw_log_path"])
        log_sha: str | None = None
        log_bytes: int | None = None
        if log_path and log_path.is_file():
            try:
                data = log_path.read_bytes()
                log_sha = _digest(data)
                log_bytes = len(data)
            except OSError:
                # A raw-log projection failure must never roll back the
                # terminal event.
                log_path = None

        with self.transaction() as conn:
            run = self._require_run(conn, run_id)
            if run["terminal_event_id"]:
                existing = conn.execute(
                    "SELECT * FROM events WHERE event_id = ?", (run["terminal_event_id"],)
                ).fetchone()
                if existing is None:
                    raise LedgerIntegrityError(
                        f"Run {run_id} references missing terminal event"
                    )
                return EventRecord(
                    existing["event_id"],
                    existing["run_id"],
                    existing["event_type"],
                    existing["occurred_at"],
                    existing["payload_sha256"],
                )
            event = self._append_event(
                conn,
                event_type=event_type,
                project_slug=run["project_slug"],
                issue_id=run["issue_id"],
                issue_identifier=run["issue_identifier"],
                run_id=run_id,
                producer="autosymph:orchestrator:v1",
                payload=payload or {},
                idempotency_key=f"run-terminal:{run_id}",
            )
            status = {
                "succeeded": "completed",
                "failed": "failed",
                "timed_out": "failed",
                "cancelled": "cancelled",
                "interrupted": "interrupted",
                "provider_error": "failed",
            }.get(process_outcome, "failed")
            conn.execute(
                """
                UPDATE runs SET
                    status = ?, process_outcome = ?, semantic_outcome = ?,
                    completed_at = ?, terminal_event_id = ?, raw_log_path = ?,
                    raw_log_sha256 = ?, raw_log_bytes = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    process_outcome,
                    semantic_outcome,
                    event.occurred_at,
                    event.event_id,
                    str(log_path) if log_path else run["raw_log_path"],
                    log_sha,
                    log_bytes,
                    run_id,
                ),
            )
            return event

    def record_terminal(
        self,
        *,
        run_id: str,
        event_type: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> EventRecord:
        """Terminalize an attempt using the runtime status vocabulary."""
        process_outcome = {
            "completed": "succeeded",
            "failed": "failed",
            "crashed": "failed",
            "preflight_blocked": "failed",
            "cancelled": "cancelled",
            "timed_out": "timed_out",
            "interrupted": "interrupted",
            "provider_error": "provider_error",
        }.get(status, "failed")
        semantic_outcome = (
            "completion_candidate" if status == "completed" else "completion_rejected"
        )
        return self.finalize_run(
            run_id,
            event_type=event_type,
            process_outcome=process_outcome,
            semantic_outcome=semantic_outcome,
            payload=payload,
        )

    def commit_run_transition(
        self,
        *,
        run_id: str,
        event_type: str,
        status: str,
        from_state: str,
        to_state: str | None,
        signal: str,
        payload: dict[str, Any] | None = None,
    ) -> EventRecord:
        """Atomically commit terminal run state, its event, and routing decision."""
        process_outcome = {
            "completed": "succeeded",
            "failed": "failed",
            "crashed": "failed",
            "preflight_blocked": "failed",
            "cancelled": "cancelled",
            "timed_out": "timed_out",
            "interrupted": "interrupted",
            "provider_error": "provider_error",
        }.get(status, "failed")
        semantic_outcome = (
            "completion_candidate" if status == "completed" else "completion_rejected"
        )
        decision_id = _uuid7()
        decision = {
            "decision_id": decision_id,
            "signal": signal,
            "from_state": from_state,
            "to_state": to_state,
        }
        with self.transaction() as conn:
            run = self._require_run(conn, run_id)
            if run["terminal_event_id"]:
                existing = conn.execute(
                    "SELECT * FROM events WHERE event_id = ?", (run["terminal_event_id"],)
                ).fetchone()
                if existing is None:
                    raise LedgerIntegrityError(
                        f"Run {run_id} references missing terminal event"
                    )
                return EventRecord(
                    existing["event_id"],
                    existing["run_id"],
                    existing["event_type"],
                    existing["occurred_at"],
                    existing["payload_sha256"],
                )

            event = self._append_event(
                conn,
                event_type=event_type,
                project_slug=run["project_slug"],
                issue_id=run["issue_id"],
                issue_identifier=run["issue_identifier"],
                run_id=run_id,
                producer="autosymph:orchestrator:v1",
                payload={**(payload or {}), "transition": decision},
                idempotency_key=f"run-terminal:{run_id}",
            )
            conn.execute(
                """
                INSERT INTO transition_decisions(
                    decision_id, run_id, from_state, to_state, accepted,
                    reason_code, decision_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    from_state,
                    to_state,
                    1 if to_state else 0,
                    signal,
                    _canonical_json(decision),
                    event.occurred_at,
                ),
            )
            conn.execute(
                """
                UPDATE runs SET status = ?, process_outcome = ?, semantic_outcome = ?,
                    completed_at = ?, terminal_event_id = ?
                WHERE run_id = ?
                """,
                (
                    "completed" if status == "completed" else status,
                    process_outcome,
                    semantic_outcome,
                    event.occurred_at,
                    event.event_id,
                    run_id,
                ),
            )
            return event

    def attach_raw_log(self, run_id: str, path: Path) -> None:
        """Attach raw NDJSON by path, digest, and byte size."""
        resolved = path.expanduser().resolve()
        data = resolved.read_bytes()
        with self.transaction() as conn:
            self._require_run(conn, run_id)
            conn.execute(
                """
                UPDATE runs SET raw_log_path = ?, raw_log_sha256 = ?, raw_log_bytes = ?
                WHERE run_id = ?
                """,
                (str(resolved), _digest(data), len(data), run_id),
            )

    def record_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str | None,
        signal: str,
    ) -> EventRecord:
        """Atomically commit a transition decision and its audit event."""
        with self.transaction() as conn:
            run = self._require_run(conn, run_id)
            idempotency_key = f"transition:{run_id}:{from_state}:{to_state}:{signal}"
            existing = conn.execute(
                "SELECT * FROM events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return EventRecord(
                    existing["event_id"],
                    existing["run_id"],
                    existing["event_type"],
                    existing["occurred_at"],
                    existing["payload_sha256"],
                )
            decision_id = _uuid7()
            decision = {
                "signal": signal,
                "from_state": from_state,
                "to_state": to_state,
            }
            conn.execute(
                """
                INSERT INTO transition_decisions(
                    decision_id, run_id, from_state, to_state, accepted,
                    reason_code, decision_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    from_state,
                    to_state,
                    1 if to_state else 0,
                    signal,
                    _canonical_json(decision),
                    _utc_now(),
                ),
            )
            return self._append_event(
                conn,
                event_type="transition_decided",
                project_slug=run["project_slug"],
                issue_id=run["issue_id"],
                issue_identifier=run["issue_identifier"],
                run_id=run_id,
                producer="autosymph:state-machine:v1",
                payload={"decision_id": decision_id, **decision},
                idempotency_key=idempotency_key,
            )

    def reconcile_open_runs(self, *, project_slug: str | None = None) -> list[str]:
        """Terminalize attempts left open by a prior process.

        Lease-aware live-process reattachment belongs to IMP-553. Until then,
        startup records interruption explicitly so no attempt disappears or is
        misreported as success.
        """
        where = "WHERE status IN ('allocated', 'running')"
        params: tuple[Any, ...] = ()
        if project_slug:
            where += " AND project_slug = ?"
            params = (project_slug,)
        rows = self._connection.execute(
            f"SELECT run_id FROM runs {where} ORDER BY created_at", params
        ).fetchall()
        reconciled: list[str] = []
        for row in rows:
            self.finalize_run(
                row["run_id"],
                event_type="run_interrupted",
                process_outcome="interrupted",
                semantic_outcome="completion_rejected",
                payload={"reason_code": "DAEMON_RESTART_BEFORE_TERMINAL_EVENT"},
            )
            reconciled.append(row["run_id"])
        return reconciled

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def list_events(self, *, run_id: str | None = None) -> list[sqlite3.Row]:
        if run_id:
            return list(
                self._connection.execute(
                    "SELECT * FROM events WHERE run_id = ? ORDER BY occurred_at, event_id",
                    (run_id,),
                ).fetchall()
            )
        return list(
            self._connection.execute("SELECT * FROM events ORDER BY occurred_at, event_id").fetchall()
        )

    def _require_run(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise LedgerError(f"Unknown run: {run_id}")
        return run

    def export_jsonl(
        self,
        path: Path,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> ExportManifest:
        """Export a deterministic, manifest-prefixed JSONL snapshot."""
        resolved = path.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        row_lines: list[str] = []
        counts: dict[str, int] = {}
        first_event: str | None = None
        last_event: str | None = None
        for table in _EXPORT_TABLES:
            columns = [row[1] for row in self._connection.execute(f"PRAGMA table_info({table})")]
            query = f"SELECT * FROM {table}"
            params: list[str] = []
            time_column = "occurred_at" if "occurred_at" in columns else (
                "created_at" if "created_at" in columns else None
            )
            filters: list[str] = []
            if time_column and since:
                filters.append(f"{time_column} >= ?")
                params.append(since)
            if time_column and until:
                filters.append(f"{time_column} <= ?")
                params.append(until)
            if filters:
                query += " WHERE " + " AND ".join(filters)
            order = next(
                (column for column in ("occurred_at", "created_at", columns[0]) if column in columns),
                columns[0],
            )
            query += f" ORDER BY {order}, {columns[0]}"
            rows = self._connection.execute(query, params).fetchall()
            counts[table] = len(rows)
            for row in rows:
                item = {key: row[key] for key in row.keys()}
                row_lines.append(
                    json.dumps(
                        {"record_type": table, "data": item},
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                )
                if table == "events":
                    first_event = first_event or row["event_id"]
                    last_event = row["event_id"]
        content = "\n".join(row_lines)
        content_sha = _digest((content + "\n") if content else "")
        manifest = ExportManifest(
            schema_version=self.schema_version,
            format="autosymph-ledger-jsonl-v1",
            created_at=_utc_now(),
            event_range={"first_event_id": first_event, "last_event_id": last_event},
            row_counts=counts,
            content_sha256=content_sha,
        )
        manifest_line = json.dumps(
            {"record_type": "manifest", "data": manifest.__dict__},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        with resolved.open("w", encoding="utf-8") as handle:
            handle.write(manifest_line + "\n")
            if content:
                handle.write(content + "\n")
        return manifest

    def export_parquet(
        self,
        path: Path,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> ExportManifest:
        """Export the append-only event stream to Parquet with a JSON manifest."""
        try:
            import pyarrow as pa  # type: ignore[import-not-found,import-untyped]
            import pyarrow.parquet as pq  # type: ignore[import-not-found,import-untyped]
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise LedgerError(
                "Parquet export requires the optional 'parquet' dependency"
            ) from exc

        filters: list[str] = []
        params: list[str] = []
        if since:
            filters.append("occurred_at >= ?")
            params.append(since)
        if until:
            filters.append("occurred_at <= ?")
            params.append(until)
        query = "SELECT * FROM events"
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY occurred_at, event_id"
        rows = self._connection.execute(query, params).fetchall()
        data = [{key: row[key] for key in row.keys()} for row in rows]
        resolved = path.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(data) if data else pa.table({"event_id": pa.array([], type=pa.string())})
        pq.write_table(table, resolved)
        file_sha = _digest(resolved.read_bytes())
        manifest = ExportManifest(
            schema_version=self.schema_version,
            format="autosymph-ledger-events-parquet-v1",
            created_at=_utc_now(),
            event_range={
                "first_event_id": rows[0]["event_id"] if rows else None,
                "last_event_id": rows[-1]["event_id"] if rows else None,
            },
            row_counts={"events": len(rows)},
            content_sha256=file_sha,
        )
        manifest_path = resolved.with_suffix(resolved.suffix + ".manifest.json")
        manifest_path.write_text(
            json.dumps(manifest.__dict__, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return manifest

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteLedger:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
