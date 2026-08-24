"""Hash-bound attempt receipts and deterministic acceptance proof gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from autosymph.runners.base import RunResult


def digest_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


@dataclass(frozen=True)
class AttemptReceipt:
    schema_version: int
    attempt_id: str
    state: str
    runner_profile: str
    adapter_type: str
    model: str | None
    auth_mode: str
    prompt_sha256: str
    base_git_sha: str
    resulting_git_sha: str
    subject_path: str
    subject_sha256: str
    raw_log_path: str
    session_id: str | None
    exit_code: int
    success: bool
    duration_seconds: float
    token_usage: dict[str, int]
    completed_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_run(
        cls,
        *,
        attempt_id: str,
        state: str,
        runner_profile: str,
        adapter_type: str,
        model: str | None,
        auth_mode: str,
        prompt: str,
        base_git_sha: str,
        resulting_git_sha: str,
        workspace_path: Path,
        subject_path: Path,
        raw_log_path: Path,
        result: RunResult,
    ) -> AttemptReceipt:
        if subject_path.is_symlink():
            raise ValueError("receipt subject must not be a symlink")
        workspace = workspace_path.resolve()
        subject = subject_path.resolve(strict=True)
        try:
            relative_subject = subject.relative_to(workspace).as_posix()
        except ValueError as error:
            raise ValueError("receipt subject must be inside the workspace") from error
        return cls(
            schema_version=1,
            attempt_id=attempt_id,
            state=state,
            runner_profile=runner_profile,
            adapter_type=adapter_type,
            model=model,
            auth_mode=auth_mode,
            prompt_sha256=digest_bytes(prompt.encode()),
            base_git_sha=base_git_sha,
            resulting_git_sha=resulting_git_sha,
            subject_path=relative_subject,
            subject_sha256=digest_file(subject),
            raw_log_path=str(raw_log_path),
            session_id=result.session_id,
            exit_code=result.exit_code,
            success=result.success,
            duration_seconds=round(result.duration_seconds, 3),
            token_usage=result.token_usage,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason_codes: tuple[str, ...]
    receipt_sha256: str
    proof_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def evaluate_proof(
    receipt: AttemptReceipt,
    proof_path: Path,
    *,
    workspace: Path,
    required_checks: set[str],
) -> GateDecision:
    """Validate a proof produced outside the implementation agent context."""
    reasons: list[str] = []
    proof_bytes = b""
    try:
        if proof_path.is_symlink():
            reasons.append("PROOF_SYMLINKED")
        resolved_proof = proof_path.resolve(strict=True)
        resolved_proof.relative_to(workspace.resolve())
        if "PROOF_SYMLINKED" not in reasons:
            proof_bytes = resolved_proof.read_bytes()
    except (FileNotFoundError, ValueError):
        reasons.append("PROOF_MISSING_OR_OUTSIDE_WORKSPACE")

    proof: dict[str, Any] = {}
    if proof_bytes:
        try:
            value = json.loads(proof_bytes)
            if not isinstance(value, dict):
                raise ValueError
            proof = value
        except (json.JSONDecodeError, ValueError):
            reasons.append("PROOF_INVALID")

    if not receipt.success or receipt.exit_code != 0:
        reasons.append("IMPLEMENTATION_ATTEMPT_FAILED")
    subject_candidate = workspace / receipt.subject_path
    try:
        if subject_candidate.is_symlink():
            reasons.append("SUBJECT_SYMLINKED")
        current_subject = subject_candidate.resolve(strict=True)
        current_subject.relative_to(workspace.resolve())
        if digest_file(current_subject) != receipt.subject_sha256:
            reasons.append("SUBJECT_MUTATED")
    except (FileNotFoundError, ValueError):
        reasons.append("SUBJECT_MISSING_OR_OUTSIDE_WORKSPACE")
    if proof:
        if proof.get("schema_version") != 1:
            reasons.append("PROOF_SCHEMA_UNSUPPORTED")
        if proof.get("attempt_id") != receipt.attempt_id:
            reasons.append("PROOF_ATTEMPT_MISMATCH")
        if proof.get("validator_id") in {None, "", receipt.session_id}:
            reasons.append("VALIDATOR_NOT_INDEPENDENT")
        if proof.get("subject_sha256") != receipt.subject_sha256:
            reasons.append("PROOF_SUBJECT_MISMATCH")
        checks = proof.get("checks")
        if not isinstance(checks, list):
            reasons.append("PROOF_CHECKS_INVALID")
            checks = []
        by_name = {item.get("name"): item for item in checks if isinstance(item, dict)}
        for check_name in required_checks:
            check = by_name.get(check_name)
            if check is None:
                reasons.append(f"CHECK_MISSING:{check_name}")
                continue
            if check.get("passed") is not True:
                reasons.append(f"CHECK_FAILED:{check_name}")
            artifact_value = check.get("artifact_path")
            artifact_candidate = workspace / str(artifact_value)
            try:
                if artifact_candidate.is_symlink():
                    reasons.append(f"CHECK_ARTIFACT_SYMLINKED:{check_name}")
                artifact = artifact_candidate.resolve(strict=True)
                artifact.relative_to(workspace.resolve())
            except (FileNotFoundError, ValueError):
                reasons.append(f"CHECK_ARTIFACT_INVALID:{check_name}")
                continue
            if digest_file(artifact) != check.get("artifact_sha256"):
                reasons.append(f"CHECK_ARTIFACT_MUTATED:{check_name}")

    receipt_bytes = json.dumps(receipt.as_dict(), sort_keys=True, separators=(",", ":")).encode()
    return GateDecision(
        allowed=not reasons,
        reason_codes=tuple(sorted(set(reasons))),
        receipt_sha256=digest_bytes(receipt_bytes),
        proof_sha256=digest_bytes(proof_bytes),
    )
