from __future__ import annotations

from evals.run import (
    STATE_CASES,
    TRACE_CASES,
    VERIFY_CASES,
    exact_score,
    state_task,
    trace_task,
)


def test_trace_contract_cases_pass() -> None:
    for case in TRACE_CASES:
        output = trace_task(case["input"])
        assert exact_score(case["input"], output, case["expected"]) == 1.0, case["id"]


def test_state_machine_cases_pass() -> None:
    for case in STATE_CASES:
        output = state_task(case["input"])
        assert exact_score(case["input"], output, case["expected"]) == 1.0, case["id"]


def test_verify_dataset_covers_all_verdict_classes() -> None:
    assert {case["expected"] for case in VERIFY_CASES} == {
        "approve",
        "reject",
        "reject_structify",
    }
