from __future__ import annotations

import asyncio
from pathlib import Path

from autosymph.runners.base import AgentRunner, RunResult
from autosymph.sample_flow import run_sample_flow


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
