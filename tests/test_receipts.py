from __future__ import annotations

from autosymph.receipts import AttemptReceipt, digest_file, evaluate_proof, write_json_atomic
from autosymph.runners.base import RunResult


def _receipt(tmp_path) -> AttemptReceipt:
    subject = tmp_path / "sample.txt"
    subject.write_text("autosymph sample complete\n")
    return AttemptReceipt.from_run(
        attempt_id="attempt-1",
        state="implement",
        runner_profile="claude-code",
        adapter_type="claude",
        model="sonnet",
        auth_mode="subscription",
        prompt="edit",
        base_git_sha="base",
        resulting_git_sha="base",
        workspace_path=tmp_path,
        subject_path=subject,
        raw_log_path=tmp_path / "run.ndjson",
        result=RunResult(success=True, exit_code=0, session_id="implementation-session"),
    )


def test_gate_accepts_hash_bound_independent_proof(tmp_path):
    receipt = _receipt(tmp_path)
    proof = tmp_path / "proof.json"
    write_json_atomic(
        proof,
        {
            "schema_version": 1,
            "attempt_id": receipt.attempt_id,
            "validator_id": "deterministic:test-v1",
            "subject_sha256": receipt.subject_sha256,
            "checks": [
                {
                    "name": "exact-content",
                    "passed": True,
                    "artifact_path": "sample.txt",
                    "artifact_sha256": digest_file(tmp_path / "sample.txt"),
                }
            ],
        },
    )

    decision = evaluate_proof(
        receipt, proof, workspace=tmp_path, required_checks={"exact-content"}
    )

    assert decision.allowed is True
    assert decision.reason_codes == ()


def test_gate_rejects_mutated_artifact(tmp_path):
    receipt = _receipt(tmp_path)
    proof = tmp_path / "proof.json"
    write_json_atomic(
        proof,
        {
            "schema_version": 1,
            "attempt_id": receipt.attempt_id,
            "validator_id": "deterministic:test-v1",
            "subject_sha256": receipt.subject_sha256,
            "checks": [
                {
                    "name": "exact-content",
                    "passed": True,
                    "artifact_path": "sample.txt",
                    "artifact_sha256": digest_file(tmp_path / "sample.txt"),
                }
            ],
        },
    )
    (tmp_path / "sample.txt").write_text("tampered\n")

    decision = evaluate_proof(
        receipt, proof, workspace=tmp_path, required_checks={"exact-content"}
    )

    assert decision.allowed is False
    assert "CHECK_ARTIFACT_MUTATED:exact-content" in decision.reason_codes
    assert "SUBJECT_MUTATED" in decision.reason_codes


def test_gate_rejects_symlinked_proof(tmp_path):
    receipt = _receipt(tmp_path)
    real_proof = tmp_path / "real-proof.json"
    write_json_atomic(
        real_proof,
        {
            "schema_version": 1,
            "attempt_id": receipt.attempt_id,
            "validator_id": "deterministic:test-v1",
            "subject_sha256": receipt.subject_sha256,
            "checks": [],
        },
    )
    proof = tmp_path / "proof.json"
    proof.symlink_to(real_proof.name)

    decision = evaluate_proof(receipt, proof, workspace=tmp_path, required_checks=set())

    assert decision.allowed is False
    assert "PROOF_SYMLINKED" in decision.reason_codes
