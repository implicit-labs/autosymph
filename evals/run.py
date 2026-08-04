from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from autosymph.config import (
    AgentConfig,
    StateConfig,
    StateTransitions,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from autosymph.logging.braintrust import BraintrustTracer
from autosymph.runners import default_runner_registry
from autosymph.runners.base import AgentEvent, EventType, RunResult
from autosymph.state_machine import Signal, StateMachine

PROJECT = "autosymph"
ROOT = Path(__file__).resolve().parents[1]
VERDICT_RE = re.compile(r'autosymph:verify_review\s+({.*?})\s*-->', re.DOTALL)


TRACE_CASES = [
    {
        "id": "claude-denial",
        "input": {
            "runner": "claude",
            "lines": [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "Bash",
                                "input": {"command": "pytest"},
                            }
                        ]
                    },
                },
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "content_block": {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Bash",
                        },
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-1",
                                "content": "Permission denied by policy",
                                "is_error": True,
                            }
                        ]
                    },
                },
            ],
        },
        "expected": {"tools": 1, "errors": 1, "denials": 1, "pending": 0},
    },
    {
        "id": "codex-denial",
        "input": {
            "runner": "codex",
            "lines": [
                {"type": "exec_command.started", "id": "tool-1", "command": "pytest"},
                {
                    "type": "exec_command.completed",
                    "id": "tool-1",
                    "exit_code": 1,
                    "output": "Permission denied by policy",
                },
            ],
        },
        "expected": {"tools": 1, "errors": 1, "denials": 1, "pending": 0},
    },
    {
        "id": "pi-denial",
        "input": {
            "runner": "pi",
            "lines": [
                {
                    "type": "toolcall_start",
                    "id": "tool-1",
                    "name": "shell",
                    "input": {"command": "pytest"},
                },
                {
                    "type": "toolcall_end",
                    "id": "tool-1",
                    "error": "Permission denied by policy",
                },
            ],
        },
        "expected": {"tools": 1, "errors": 1, "denials": 1, "pending": 0},
    },
    {
        "id": "pi-current-denial",
        "input": {
            "runner": "pi",
            "lines": [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "tool-1",
                    "toolName": "bash",
                    "args": {"command": "pytest"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "tool-1",
                    "toolName": "bash",
                    "result": "Permission denied by policy",
                    "isError": True,
                },
            ],
        },
        "expected": {"tools": 1, "errors": 1, "denials": 1, "pending": 0},
    },
    {
        "id": "codex-success",
        "input": {
            "runner": "codex",
            "lines": [
                {"type": "exec_command.started", "id": "tool-1", "command": "pytest"},
                {
                    "type": "exec_command.completed",
                    "id": "tool-1",
                    "exit_code": 0,
                    "output": "12 passed",
                },
            ],
        },
        "expected": {"tools": 1, "errors": 0, "denials": 0, "pending": 0},
    },
]


STATE_CASES = [
    {"id": "implement-complete", "input": {"kind": "transition", "state": "implement", "signal": "complete"}, "expected": "verify"},
    {"id": "implement-fail", "input": {"kind": "transition", "state": "implement", "signal": "fail"}, "expected": "investigating"},
    {"id": "verify-complete", "input": {"kind": "transition", "state": "verify", "signal": "complete"}, "expected": "verify_review"},
    {"id": "verify-review-approve", "input": {"kind": "transition", "state": "verify_review", "signal": "complete"}, "expected": "review"},
    {"id": "verify-review-reject", "input": {"kind": "transition", "state": "verify_review", "signal": "fail"}, "expected": "verify"},
    {"id": "verify-review-structify", "input": {"kind": "transition", "state": "verify_review", "signal": "structify"}, "expected": "investigating"},
    {"id": "review-approve", "input": {"kind": "transition", "state": "review", "signal": "approve"}, "expected": "finalize"},
    {"id": "invalid-transition", "input": {"kind": "transition", "state": "review", "signal": "complete"}, "expected": None},
    {"id": "claim-lifecycle", "input": {"kind": "claim_lifecycle"}, "expected": [True, False, 1, 0, True]},
    {"id": "rework-exhaustion", "input": {"kind": "rework_exhaustion"}, "expected": ["rework", "rework", "todo"]},
]


VERIFY_CASES = [
    {
        "id": "complete-visual-evidence",
        "input": {
            "testing_plan": ["pytest [cli]", "Green button visible [ios-simulator + screenshot]"],
            "implementation": "PR #41 exists with implementation commits.",
            "summary": "pytest passed. ![Green button](https://uploads.linear.app/good.png)",
            "trace": "pytest exited 0; simulator screenshot captured; create_attachment succeeded.",
        },
        "expected": "approve",
    },
    {
        "id": "missing-screenshot-url",
        "input": {
            "testing_plan": ["Green button visible [ios-simulator + screenshot]"],
            "implementation": "PR #42 exists with implementation commits.",
            "summary": "PASS: screenshot captured locally.",
            "trace": "simulator screenshot captured; no create_attachment call.",
        },
        "expected": "reject_structify",
    },
    {
        "id": "no-implementation",
        "input": {
            "testing_plan": ["pytest [cli]"],
            "implementation": "No PR and zero implementation commits.",
            "summary": "pytest passed on main.",
            "trace": "pytest exited 0.",
        },
        "expected": "reject",
    },
    {
        "id": "permission-denial",
        "input": {
            "testing_plan": ["swift test [cli]"],
            "implementation": "PR #43 exists.",
            "summary": "BLOCKED: swift test permission denied.",
            "trace": "swift test attempted three times; permission_denied each time.",
        },
        "expected": "reject_structify",
    },
    {
        "id": "human-only-haptics",
        "input": {
            "testing_plan": ["Haptic feels correct [ios-simulator + video]"],
            "implementation": "PR #44 exists and unit tests pass.",
            "summary": "BLOCKED: haptics require a physical device; human check requested.",
            "trace": "Implementation and unit tests inspected; simulator cannot render haptics.",
        },
        "expected": "approve",
    },
    {
        "id": "missing-plan-item",
        "input": {
            "testing_plan": ["pytest [cli]", "API returns 200 [cli]"],
            "implementation": "PR #45 exists.",
            "summary": "pytest passed.",
            "trace": "pytest exited 0; API check never attempted.",
        },
        "expected": "reject",
    },
    {
        "id": "auth-outage",
        "input": {
            "testing_plan": ["Signed-in dashboard loads [browser]"],
            "implementation": "PR #46 exists.",
            "summary": "BLOCKED: test auth endpoint returned 500.",
            "trace": "Authentication attempted once; backend returned HTTP 500.",
        },
        "expected": "reject_structify",
    },
    {
        "id": "api-cli-evidence",
        "input": {
            "testing_plan": ["API returns expected JSON [cli]"],
            "implementation": "PR #47 exists.",
            "summary": "curl returned 200 and exact expected JSON.",
            "trace": "curl command exited 0; response body matched fixture.",
        },
        "expected": "approve",
    },
]


MICROREPO_CASES = [
    {
        "id": "python-addition",
        "input": {
            "files": {
                "calculator.py": "def add(a: int, b: int) -> int:\n    return a - b\n",
                "test_calculator.py": "from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            },
            "allowed_changes": ["calculator.py"],
            "prompt": "Fix the implementation so the existing tests pass. Do not change tests. Run the tests before finishing.",
        },
        "expected": {"tests_passed": True, "scope_ok": True},
    },
    {
        "id": "python-slug",
        "input": {
            "files": {
                "slug.py": "def slugify(value: str) -> str:\n    return value\n",
                "test_slug.py": "from slug import slugify\n\ndef test_slugify():\n    assert slugify('Hello World') == 'hello-world'\n",
            },
            "allowed_changes": ["slug.py"],
            "prompt": "Fix the implementation so the existing tests pass. Do not change tests. Run the tests before finishing.",
        },
        "expected": {"tests_passed": True, "scope_ok": True},
    },
]


@dataclass
class _MemorySpan:
    name: str
    children: list[_MemorySpan] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    ended: bool = False

    def start_span(self, name: str, **_kwargs: Any) -> _MemorySpan:
        child = _MemorySpan(name)
        self.children.append(child)
        return child

    def log(self, **kwargs: Any) -> None:
        self.logs.append(kwargs)

    def end(self) -> None:
        self.ended = True


@dataclass
class _MemoryLogger:
    roots: list[_MemorySpan] = field(default_factory=list)

    def start_span(self, name: str, **_kwargs: Any) -> _MemorySpan:
        span = _MemorySpan(name)
        self.roots.append(span)
        return span


def trace_task(input: dict[str, Any]) -> dict[str, int]:
    runners = default_runner_registry()
    runner = runners[input["runner"]]
    logger = _MemoryLogger()
    tracer = BraintrustTracer.__new__(BraintrustTracer)
    tracer._logger = logger
    tracer._issue_spans = {}
    tracer._runs = {}
    tracer.start_run("issue-1", "ISSUE-1", "implement", 1, "eval", {})

    for payload in input["lines"]:
        event = runner.parse_event(json.dumps(payload))
        if event is not None:
            tracer.on_event("issue-1", event)

    pending = len(tracer._runs["issue-1"].pending_tools)
    tracer.end_run("issue-1", RunResult(success=True, exit_code=0))
    run_span = logger.roots[0].children[0]
    summary = run_span.logs[-1]["metadata"]["tool_summary"]
    return {
        "tools": summary["total"],
        "errors": summary["errors"],
        "denials": summary["denials"],
        "pending": pending,
    }


def _workflow() -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(project="eval", api_key="unused"),
        workspace=WorkspaceConfig(root="/tmp/autosymph-eval", repo="/tmp/repo"),
        agent=AgentConfig(max_concurrent_agents=3),
        states={
            "implement": StateConfig(
                type="agent",
                transitions=StateTransitions(complete="verify", fail="investigating"),
            ),
            "verify": StateConfig(
                type="agent",
                transitions=StateTransitions(complete="verify_review", fail="implement"),
            ),
            "verify_review": StateConfig(
                type="agent",
                transitions=StateTransitions(
                    complete="review",
                    fail="verify",
                    structify="investigating",
                ),
            ),
            "review": StateConfig(
                type="gate",
                rework_to="rework",
                max_rework=2,
                rework_exhausted="todo",
                transitions=StateTransitions(approve="finalize", fail="rework"),
            ),
            "rework": StateConfig(
                type="agent",
                transitions=StateTransitions(complete="verify"),
            ),
            "investigating": StateConfig(
                type="agent",
                transitions=StateTransitions(complete="verify", escalate="blocked"),
            ),
            "finalize": StateConfig(
                type="agent",
                transitions=StateTransitions(complete="done"),
            ),
            "blocked": StateConfig(type="terminal"),
            "done": StateConfig(type="terminal"),
        },
    )


def state_task(input: dict[str, Any]) -> Any:
    sm = StateMachine(_workflow())
    sm.track_issue("issue-1", "ISSUE-1", input.get("state", "implement"))
    kind = input["kind"]
    if kind == "transition":
        return sm.next_state(input["state"], Signal(input["signal"]), "issue-1")
    if kind == "claim_lifecycle":
        first = sm.claim("issue-1")
        duplicate = sm.claim("issue-1")
        sm.mark_running("issue-1")
        running = sm.running_count()
        sm.release("issue-1")
        released = sm.running_count()
        reclaimed = sm.claim("issue-1")
        return [first, duplicate, running, released, reclaimed]
    if kind == "rework_exhaustion":
        return [sm.next_state("review", Signal.FAIL, "issue-1") for _ in range(3)]
    raise ValueError(f"unknown state eval kind: {kind}")


def _collect_text(events: Iterable[AgentEvent]) -> str:
    parts: list[str] = []
    for event in events:
        if event.type == EventType.ASSISTANT_MESSAGE:
            text = event.data.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif event.type == EventType.ASSISTANT_TURN:
            parts.extend(str(text) for text in event.data.get("texts", []) if text)
    return "\n".join(parts)


def _decision(text: str) -> str | None:
    match = VERDICT_RE.search(text)
    if not match:
        return None
    try:
        value = json.loads(match.group(1)).get("decision")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


async def verify_task(input: dict[str, Any], runner_name: str, model: str | None) -> dict[str, Any]:
    policy = (ROOT / "prompts" / "verify-review.md").read_text()
    prompt = f"""{policy}

## Offline evaluation mode

All context-gathering and completion-audit steps have already been performed.
Do not use tools or modify files. Apply the verdict policy to the evidence package
below. Return a short rationale followed by exactly one machine-readable verdict
block as the final line.

Evidence package:
{json.dumps(input, indent=2, sort_keys=True)}
"""
    runner = default_runner_registry()[runner_name]
    config: dict[str, Any] = {
        "permission_mode": "plan",
        "max_turns": 3,
        "env_vars": {"BRAINTRUST_API_KEY": "", "LINEAR_API_KEY": ""},
    }
    if model:
        config["model"] = model
    result = await asyncio.wait_for(
        runner.run(prompt, str(ROOT), config),
        timeout=240,
    )
    text = _collect_text(result.events)
    return {
        "decision": _decision(text),
        "runner_success": result.success,
        "text": text[-4000:],
        "duration_seconds": round(result.duration_seconds, 3),
        "tokens": result.token_usage,
    }


def _write_microrepo(path: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        (path / relative).write_text(content)
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=autosymph-eval", "-c", "user.email=eval@local", "commit", "-qm", "fixture"],
        cwd=path,
        check=True,
    )


async def microrepo_task(
    input: dict[str, Any], runner_name: str, model: str | None
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"autosymph-{runner_name}-") as tmp:
        path = Path(tmp)
        _write_microrepo(path, input["files"])
        baseline = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        runner = default_runner_registry()[runner_name]
        config: dict[str, Any] = {
            "permission_mode": "acceptEdits",
            "max_turns": 10,
            "env_vars": {"BRAINTRUST_API_KEY": "", "LINEAR_API_KEY": ""},
        }
        if model:
            config["model"] = model
        result = await asyncio.wait_for(
            runner.run(input["prompt"], str(path), config),
            timeout=300,
        )
        test = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=path,
            text=True,
            capture_output=True,
            timeout=60,
        )
        working_tree_changes = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
        committed_changes = subprocess.run(
            ["git", "diff", "--name-only", baseline, "HEAD"],
            cwd=path,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
        changed_paths = sorted(
            set(committed_changes)
            | {line[3:] for line in working_tree_changes if len(line) > 3}
        )
        allowed = set(input["allowed_changes"])
        scope_ok = bool(changed_paths) and set(changed_paths).issubset(allowed)
        return {
            "tests_passed": test.returncode == 0,
            "scope_ok": scope_ok,
            "changed_paths": changed_paths,
            "runner_success": result.success,
            "duration_seconds": round(result.duration_seconds, 3),
            "tokens": result.token_usage,
            "test_output": (test.stdout + test.stderr)[-2000:],
        }


def exact_score(input: Any, output: Any, expected: Any) -> float:
    del input
    return float(output == expected)


def verdict_score(input: Any, output: dict[str, Any], expected: str) -> float:
    del input
    return float(output.get("decision") == expected)


def runner_completed_score(input: Any, output: dict[str, Any], expected: Any) -> float:
    del input, expected
    return float(bool(output.get("runner_success")))


def tests_passed_score(input: Any, output: dict[str, Any], expected: dict[str, Any]) -> float:
    del input
    return float(output.get("tests_passed") == expected["tests_passed"])


def scope_score(input: Any, output: dict[str, Any], expected: dict[str, Any]) -> float:
    del input
    return float(output.get("scope_ok") == expected["scope_ok"])


def _load_api_key(env_file: Path) -> None:
    if os.environ.get("BRAINTRUST_API_KEY"):
        return
    if not env_file.exists():
        return
    for raw_line in env_file.read_text().splitlines():
        if raw_line.startswith("BRAINTRUST_API_KEY="):
            os.environ["BRAINTRUST_API_KEY"] = raw_line.split("=", 1)[1].strip()
            return


def _dataset(name: str, cases: list[dict[str, Any]], upload: bool) -> Any:
    rows = [
        {
            "input": case["input"],
            "expected": case["expected"],
            "metadata": {"case_id": case["id"]},
        }
        for case in cases
    ]
    if not upload:
        return rows

    import braintrust

    dataset = braintrust.init_dataset(
        project=PROJECT,
        name=name,
        description="Versioned autosymph evaluation fixtures.",
    )
    namespace = uuid.UUID("7d02b6c9-78cf-414a-9507-e96a357e2098")
    for row, case in zip(rows, cases, strict=True):
        dataset.insert(
            id=str(uuid.uuid5(namespace, f"{name}:{case['id']}")),
            input=row["input"],
            expected=row["expected"],
            metadata=row["metadata"],
        )
    dataset.flush()
    return dataset


def _eval(
    *,
    dataset_name: str,
    experiment_name: str,
    cases: list[dict[str, Any]],
    task: Any,
    scores: list[Any],
    upload: bool,
    metadata: dict[str, Any] | None = None,
) -> Any:
    import braintrust

    return braintrust.Eval(
        PROJECT,
        data=_dataset(dataset_name, cases, upload),
        task=task,
        scores=scores,
        experiment_name=experiment_name,
        metadata=metadata or {},
        max_concurrency=1,
        no_send_logs=not upload,
    )


def _summary(result: Any) -> dict[str, Any]:
    summary = getattr(result, "summary", None)
    if summary is None:
        return {"result": str(result)}
    failures = []
    for row in getattr(result, "results", []):
        if any(score < 1 for score in (row.scores or {}).values()):
            failures.append(
                {
                    "case_id": (row.metadata or {}).get("case_id"),
                    "expected": row.expected,
                    "output": row.output,
                    "scores": row.scores,
                    "error": str(row.error) if row.error else None,
                }
            )
    return {
        "experiment_name": getattr(summary, "experiment_name", None),
        "experiment_url": getattr(summary, "experiment_url", None),
        "scores": str(getattr(summary, "scores", {})),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run autosymph quality evaluations.")
    parser.add_argument(
        "--suite",
        action="append",
        choices=("trace", "state", "verify", "microrepo", "all"),
        help="Suite to run; may be repeated. Defaults to trace and state.",
    )
    parser.add_argument("--upload", action="store_true", help="Upload datasets and experiments to Braintrust.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("~/.autosymph/config/local.env").expanduser(),
    )
    parser.add_argument("--verify-runners", default="claude,codex")
    parser.add_argument("--microrepo-runners", default="claude,codex,pi")
    parser.add_argument("--claude-model", default="sonnet")
    parser.add_argument("--codex-model", default=None)
    parser.add_argument("--pi-model", default=None)
    args = parser.parse_args()

    suites = args.suite or ["trace", "state"]
    if "all" in suites:
        suites = ["trace", "state", "verify", "microrepo"]
    if args.upload:
        _load_api_key(args.env_file)
        if not os.environ.get("BRAINTRUST_API_KEY"):
            parser.error("--upload requires BRAINTRUST_API_KEY or --env-file containing it")

    results: dict[str, Any] = {}
    if "trace" in suites:
        result = _eval(
            dataset_name="autosymph-trace-contract-v1",
            experiment_name="trace-contract-v1",
            cases=TRACE_CASES,
            task=trace_task,
            scores=[exact_score],
            upload=args.upload,
        )
        results["trace"] = _summary(result)
    if "state" in suites:
        result = _eval(
            dataset_name="autosymph-state-machine-v1",
            experiment_name="state-machine-v1",
            cases=STATE_CASES,
            task=state_task,
            scores=[exact_score],
            upload=args.upload,
        )
        results["state"] = _summary(result)
    if "verify" in suites:
        for runner_name in args.verify_runners.split(","):
            runner_name = runner_name.strip()
            model = {
                "claude": args.claude_model,
                "codex": args.codex_model,
                "pi": args.pi_model,
            }.get(runner_name)

            async def task(input: dict[str, Any], _runner: str = runner_name, _model: str | None = model) -> dict[str, Any]:
                return await verify_task(input, _runner, _model)

            result = _eval(
                dataset_name="autosymph-verify-review-v1",
                experiment_name=f"verify-review-v1-{runner_name}",
                cases=VERIFY_CASES,
                task=task,
                scores=[verdict_score, runner_completed_score],
                upload=args.upload,
                metadata={"runner": runner_name, "model": model or "default"},
            )
            results[f"verify:{runner_name}"] = _summary(result)
    if "microrepo" in suites:
        for runner_name in args.microrepo_runners.split(","):
            runner_name = runner_name.strip()
            model = {
                "claude": args.claude_model,
                "codex": args.codex_model,
                "pi": args.pi_model,
            }.get(runner_name)

            async def task(input: dict[str, Any], _runner: str = runner_name, _model: str | None = model) -> dict[str, Any]:
                return await microrepo_task(input, _runner, _model)

            result = _eval(
                dataset_name="autosymph-microrepo-v1",
                experiment_name=f"microrepo-v1-{runner_name}",
                cases=MICROREPO_CASES,
                task=task,
                scores=[tests_passed_score, scope_score, runner_completed_score],
                upload=args.upload,
                metadata={"runner": runner_name, "model": model or "default"},
            )
            results[f"microrepo:{runner_name}"] = _summary(result)

    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
