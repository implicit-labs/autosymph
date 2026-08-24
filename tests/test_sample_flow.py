from __future__ import annotations

import asyncio
from pathlib import Path

from autosymph.runners.base import AgentRunner, RunResult
from autosymph.sample_flow import run_sample_flow, run_sample_flow_sync


class SampleRunner(AgentRunner):
    def parse_event(self, line):
        del line
        return None

    async def run(self, prompt, workspace_path, config, session_id=None, on_event=None, on_raw_line=None):
        del prompt, config, session_id, on_event
        Path(workspace_path, "sample.txt").write_text("autosymph sample complete\n")
        if on_raw_line:
            on_raw_line('{"type":"result","success":true}')
        return RunResult(success=True, exit_code=0, session_id="sample-runner")

    async def kill(self, pid):
        del pid


def test_sample_flow_reaches_done_with_evidence(tmp_path):
    workspace = tmp_path / "sample"
    result = asyncio.run(
        run_sample_flow(
            "claude-code", workspace, runner_registry={"claude": SampleRunner()}
        )
    )

    assert result.success is True
    assert result.states == ("implement", "verify", "done")
    assert Path(result.receipt_path).is_file()
    assert Path(result.proof_path).is_file()
    assert Path(result.raw_log_path).is_file()


def test_sync_wrapper_forwards_compiled_flow_options(monkeypatch, tmp_path):
    captured = {}

    async def fake_run_sample_flow(**kwargs):
        captured.update(kwargs)
        return "forwarded"

    monkeypatch.setattr("autosymph.sample_flow.run_sample_flow", fake_run_sample_flow)

    def extra_checks(workspace):
        del workspace
        return []

    result = run_sample_flow_sync(
        "codex",
        tmp_path / "workspace",
        workflow=None,
        prompt_prefix="skill",
        extra_checks=extra_checks,
    )

    assert result == "forwarded"
    assert captured["prompt_prefix"] == "skill"
    assert captured["extra_checks"] is extra_checks


def test_extra_check_exception_fails_closed(tmp_path):
    workspace = tmp_path / "sample"

    def broken_check(target):
        del target
        raise RuntimeError("validator unavailable")

    result = asyncio.run(
        run_sample_flow(
            "claude-code",
            workspace,
            runner_registry={"claude": SampleRunner()},
            extra_checks=broken_check,
        )
    )

    assert result.success is False
    assert result.states == ("implement", "verify", "blocked")
    assert "CHECK_FAILED:extra-check-execution" in result.gate_reasons
