"""NDJSON stream — tees raw agent output to disk (Tier 2 logging).

Log layout (default ~/.autosymph/logs/, configurable via logging.log_root):
    {log_root}/{project_slug}/{issue_id}/{state}-run{N}.ndjson    — namespaced
    {log_root}/{issue_id}/{state}-run{N}.ndjson                   — flat (backward compat)
    {log_root}/{issue_id}/{state}-run{N}.meta.json                — summary stats
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from autosymph.ledger import redact_payload

logger = logging.getLogger(__name__)


class LogStream:
    """Writes raw NDJSON lines to disk for post-hoc analysis."""

    def __init__(self, log_root: Path, project_slug: str | None = None) -> None:
        self.log_root = log_root.expanduser().resolve()
        self.project_slug = project_slug

    def _issue_dir(self, issue_id: str) -> Path:
        """Return the log directory for an issue, namespaced if project_slug is set."""
        if self.project_slug:
            return self.log_root / self.project_slug / issue_id
        return self.log_root / issue_id

    def open(self, issue_id: str, state: str, run_number: int) -> Path:
        """Create and return the log file path for a run."""
        log_dir = self._issue_dir(issue_id)
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{state}-run{run_number}.ndjson"
        logger.info("Logging to %s", path)
        return path

    def write_line(self, path: Path, line: str) -> None:
        """Append a single raw NDJSON line to the log file."""
        with path.open("a") as f:
            f.write(line + "\n")

    def write_meta(
        self,
        path: Path,
        *,
        issue_id: str,
        state: str,
        run_number: int,
        success: bool,
        exit_code: int,
        duration_seconds: float,
        token_usage: dict[str, int],
        session_id: str | None,
        error: str | None,
        runner: str = "claude",
        run_id: str | None = None,
    ) -> Path:
        """Write a summary meta file alongside the NDJSON log."""
        meta_path = path.with_suffix(".meta.json")
        meta = {
            "issue_id": issue_id,
            "state": state,
            "run": run_number,
            "success": success,
            "exit_code": exit_code,
            "duration_seconds": round(duration_seconds, 1),
            "token_usage": token_usage,
            "session_id": session_id,
            "runner": runner,
            "run_id": run_id,
            "error": error,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_path.write_text(json.dumps(redact_payload(meta), indent=2) + "\n")
        return meta_path

    def count_runs(self, issue_id: str, state: str) -> int:
        """Count existing runs to determine the next run number."""
        log_dir = self._issue_dir(issue_id)
        if not log_dir.exists():
            return 0
        return len(list(log_dir.glob(f"{state}-run*.ndjson")))
