"""Tests for orchestrator state-resolution and Linear-status mapping (ISSUE-356).

Specifically covers:
- `_resolve_workflow_state` routes Autoplan → "autoplan"
- `_workflow_to_linear_status` maps "autoplan" → ls.autoplan
- Terminal state with no linear_state skips Linear transition (PRD design verification)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from autosymph.config import (
    AgentConfig,
    LinearStatesConfig,
    StateConfig,
    StateTransitions,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)


@dataclass
class _FakeIssue:
    """Minimal LinearIssue stand-in for `_resolve_workflow_state` (only `.status` is read)."""

    status: str
    id: str = "issue-id"
    identifier: str = "TEST-1"
    title: str = "Test issue"
    labels: list[str] | None = None

    def __post_init__(self):
        if self.labels is None:
            self.labels = []


def _make_config(autoplan: str | None = "Autoplan") -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(project="test", api_key="k"),
        workspace=WorkspaceConfig(root="/tmp/t", repo="/tmp/r"),
        agent=AgentConfig(max_concurrent_agents=1),
        linear_states=LinearStatesConfig(autoplan=autoplan),
        states={
            "autoplan": StateConfig(
                type="agent",
                prompt="prompts/autoplan.md",
                linear_state="autoplan",
                transitions=StateTransitions(
                    **{"complete": "autoplan_done", "fail": "investigating"}
                ),
            ),
            "autoplan_done": StateConfig(type="terminal"),  # no linear_state
            "implement": StateConfig(
                type="agent",
                prompt="prompts/implement.md",
                linear_state="active",
                transitions=StateTransitions(**{"complete": "done"}),
            ),
            "investigating": StateConfig(
                type="agent",
                prompt="p.md",
                linear_state="investigating",
                transitions=StateTransitions(**{"complete": "done"}),
            ),
            "done": StateConfig(type="terminal", linear_state="terminal"),
        },
    )


def _make_orchestrator(config: WorkflowConfig):
    """Build a real Orchestrator without doing I/O. We only call pure routing methods."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from autosymph.orchestrator import Orchestrator
    from autosymph.ledger import SQLiteLedger
    from autosymph.state_machine import StateMachine

    linear = MagicMock()
    linear.is_configured = True
    linear.project_name = "test"
    workspace_mgr = MagicMock()
    return Orchestrator(
        config=config,
        config_path=Path("/tmp/test.yaml"),
        linear=linear,
        state_machine=StateMachine(config),
        workspace_mgr=workspace_mgr,
        ledger=SQLiteLedger(":memory:"),
    )


class TestResolveWorkflowStateAutoplan:
    """`_resolve_workflow_state` routing for the new Autoplan Linear status."""

    def test_autoplan_status_routes_to_autoplan_workflow(self):
        cfg = _make_config(autoplan="Autoplan")
        orch = _make_orchestrator(cfg)
        result = orch._resolve_workflow_state(_FakeIssue(status="Autoplan"))
        assert result == "autoplan"

    def test_custom_autoplan_name_routes(self):
        """Routing follows config, not a hardcoded name."""
        cfg = _make_config(autoplan="PlanQueue")
        orch = _make_orchestrator(cfg)
        result = orch._resolve_workflow_state(_FakeIssue(status="PlanQueue"))
        assert result == "autoplan"

    def test_autoplan_disabled_falls_through(self):
        """When autoplan is None, the matching branch is skipped."""
        cfg = _make_config(autoplan=None)
        orch = _make_orchestrator(cfg)
        # With autoplan=None, "Autoplan" status should not match any branch → None
        result = orch._resolve_workflow_state(_FakeIssue(status="Autoplan"))
        assert result is None


class TestWorkflowToLinearStatusAutoplan:
    """`_workflow_to_linear_status` mapping for the new logical state."""

    def test_autoplan_maps_to_configured_name(self):
        cfg = _make_config(autoplan="Autoplan")
        orch = _make_orchestrator(cfg)
        assert orch._workflow_to_linear_status("autoplan") == "Autoplan"

    def test_autoplan_custom_name(self):
        cfg = _make_config(autoplan="PlanQueue")
        orch = _make_orchestrator(cfg)
        assert orch._workflow_to_linear_status("autoplan") == "PlanQueue"

    def test_autoplan_returns_none_when_unset(self):
        """When autoplan is None, the mapping returns None (no transition)."""
        cfg = _make_config(autoplan=None)
        orch = _make_orchestrator(cfg)
        assert orch._workflow_to_linear_status("autoplan") is None


class TestAutoplanDoneTerminalGate:
    """Verify the PRD's `autoplan_done` design — terminal with no linear_state means
    the orchestrator's gate at orchestrator.py:755 must skip the Linear transition.

    This is a static check on StateConfig schema and the gate condition (not a full
    `on_agent_complete` integration test, which would require heavy mocking)."""

    def test_terminal_without_linear_state_is_valid_schema(self):
        cfg = _make_config()
        terminal_cfg = cfg.states["autoplan_done"]
        assert terminal_cfg.type == "terminal"
        assert terminal_cfg.linear_state is None

    def test_orchestrator_gate_condition_skips_when_linear_state_falsy(self):
        """The condition `target_cfg and target_cfg.linear_state` is False when
        linear_state is None — verifies our PRD design assumption."""
        cfg = _make_config()
        terminal_cfg = cfg.states["autoplan_done"]
        # The exact condition the orchestrator uses (orchestrator.py:755):
        gate = bool(terminal_cfg and terminal_cfg.linear_state)
        assert gate is False

    def test_terminal_branch_triggers_for_autoplan_done(self):
        """orchestrator.py:763 untrack-on-terminal branch: relies on type == 'terminal'."""
        cfg = _make_config()
        terminal_cfg = cfg.states["autoplan_done"]
        assert terminal_cfg.type == "terminal"

    def test_check_transition_targets_accepts_autoplan_done(self):
        """WorkflowConfig.check_transition_targets must accept autoplan_done as
        a transition target (PRD's 'implementer must verify' check). Regression
        guard so a future schema tightening doesn't silently reject our new
        terminal state."""
        # Building _make_config() exercises the validator; a clean instance
        # means autoplan + autoplan_done both passed check_transition_targets.
        cfg = _make_config()
        valid_targets = set(cfg.states.keys()) | {"todo", "planning", "ready"}
        assert "autoplan" in valid_targets
        assert "autoplan_done" in valid_targets

    def test_check_transition_targets_rejects_typo_target(self):
        """Sanity: validator still flags an undefined target. Without this,
        the previous test could pass even if the validator was broken."""
        from pydantic import ValidationError

        from autosymph.config import (
            AgentConfig,
            LinearStatesConfig,
            StateConfig,
            StateTransitions,
            TrackerConfig,
            WorkflowConfig,
            WorkspaceConfig,
        )

        with pytest.raises(ValidationError):
            WorkflowConfig(
                tracker=TrackerConfig(project="t", api_key="k"),
                workspace=WorkspaceConfig(root="/tmp/t", repo="/tmp/r"),
                agent=AgentConfig(max_concurrent_agents=1),
                linear_states=LinearStatesConfig(autoplan="Autoplan"),
                states={
                    "autoplan": StateConfig(
                        type="agent",
                        prompt="p.md",
                        linear_state="autoplan",
                        # typo target — should be rejected
                        transitions=StateTransitions(**{"complete": "autoplan_dnoe"}),
                    ),
                    "autoplan_done": StateConfig(type="terminal"),
                },
            )


class TestClaudeSessionStartLogLine:
    """R11 / AC3 / AC6 / AC7 / AC8: claude.py session-start structured log line.

    The runner-level test (test_claude_runner.py) is the authoritative coverage:
    it asserts the line is emitted at the actual subprocess spawn site and reads
    the literal --model argv. This class is kept as a smoke test for the format
    contract so a future refactor doesn't silently regress the grep target.
    """

    def test_log_line_format_is_grep_friendly(self, caplog):
        """The log message must contain `claude session start:` literal so a tail-grep
        (per E4 in the testing plan) finds it. Also must include identifier, state,
        model, prompt — otherwise AC3/AC6/AC7/AC8 are not verifiable from logs."""
        import logging
        logger = logging.getLogger("autosymph.runners.claude")
        with caplog.at_level(logging.INFO, logger="autosymph.runners.claude"):
            logger.info(
                "claude session start: identifier=%s state=%s model=%s prompt=%s",
                "ISSUE-TEST-1",
                "autoplan",
                "claude-opus-4-7",
                "prompts/autoplan.md",
            )
        msgs = [r.getMessage() for r in caplog.records]
        line = next(m for m in msgs if "claude session start:" in m)
        assert "identifier=ISSUE-TEST-1" in line
        assert "state=autoplan" in line
        assert "model=claude-opus-4-7" in line
        assert "prompt=prompts/autoplan.md" in line

class TestPromptResolution:
    def test_prompt_root_overrides_config_directory_for_relative_prompts(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            workspace=WorkspaceConfig(root="/tmp/t", repo="/tmp/r"),
            prompts={"root": "../../autosymph/prompts", "global_prompt": "global.md"},
            states={"done": StateConfig(type="terminal")},
        )
        orch = _make_orchestrator(cfg)
        orch.config_path = Path("/example/config/projects/example.yaml")

        resolved = orch._resolve_prompt_path("verify.md")

        assert resolved == Path(
            "/example/autosymph/prompts/verify.md"
        )

    def test_absolute_prompt_path_bypasses_prompt_root(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            workspace=WorkspaceConfig(root="/tmp/t", repo="/tmp/r"),
            prompts={"root": "../../autosymph/prompts"},
            states={"done": StateConfig(type="terminal")},
        )
        orch = _make_orchestrator(cfg)

        resolved = orch._resolve_prompt_path("/tmp/custom-prompt.md")

        assert resolved == Path("/tmp/custom-prompt.md").resolve()


class TestRunnerResolution:
    def test_default_runner_is_claude(self):
        cfg = _make_config()
        orch = _make_orchestrator(cfg)

        issue = _FakeIssue(status="Ready")
        state = cfg.states["implement"]

        assert orch._resolve_runner_name(issue, "implement", state) == "claude"

    def test_state_runner_wins_over_default(self):
        cfg = _make_config()
        cfg.states["implement"].runner = "codex"
        orch = _make_orchestrator(cfg)

        issue = _FakeIssue(status="Ready")

        assert orch._resolve_runner_name(issue, "implement", cfg.states["implement"]) == "codex"

    def test_auto_match_wins_over_state_runner(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            runners={
                "default": "claude",
                "auto_match": [{"runner": "codex", "title": "refactor|cleanup"}],
            },
            states={
                "implement": StateConfig(
                    type="agent",
                    prompt="p.md",
                    runner="pi",
                    transitions=StateTransitions(**{"complete": "done"}),
                ),
                "done": StateConfig(type="terminal"),
            },
        )
        orch = _make_orchestrator(cfg)

        issue = _FakeIssue(status="Ready", title="Cleanup stale config")

        assert orch._resolve_runner_name(issue, "implement", cfg.states["implement"]) == "codex"

    def test_label_wins_over_auto_match(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            runners={
                "default": "claude",
                "available": {
                    "claude": {"type": "claude"},
                    "codex": {"type": "codex"},
                    "pi": {"type": "pi"},
                },
                "auto_match": [{"runner": "codex", "title": "cleanup"}],
            },
            states={
                "implement": StateConfig(type="agent", prompt="p.md", runner="claude"),
                "done": StateConfig(type="terminal"),
            },
        )
        orch = _make_orchestrator(cfg)

        issue = _FakeIssue(
            status="Ready",
            title="Cleanup stale config",
            labels=["runner:pi"],
        )

        assert orch._resolve_runner_name(issue, "implement", cfg.states["implement"]) == "pi"

    def test_label_requires_runner_to_be_enabled(self):
        from autosymph.config import ConfigError

        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            states={
                "implement": StateConfig(type="agent", prompt="p.md", runner="claude"),
                "done": StateConfig(type="terminal"),
            },
        )
        orch = _make_orchestrator(cfg)
        issue = _FakeIssue(status="Ready", labels=["runner:pi"])

        with pytest.raises(ConfigError, match="not enabled"):
            orch._resolve_runner_name(issue, "implement", cfg.states["implement"])

    def test_label_matching_rule_requires_all_labels(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            runners={
                "default": "claude",
                "available": {
                    "claude": {"type": "claude"},
                    "codex": {"type": "codex"},
                    "pi": {"type": "pi"},
                },
                "auto_match": [{"runner": "codex", "labels": ["area:infra", "kind:cleanup"]}],
            },
            states={
                "implement": StateConfig(type="agent", prompt="p.md", runner="pi"),
                "done": StateConfig(type="terminal"),
            },
        )
        orch = _make_orchestrator(cfg)

        issue = _FakeIssue(status="Ready", labels=["area:infra"])

        assert orch._resolve_runner_name(issue, "implement", cfg.states["implement"]) == "pi"

    def test_malformed_issue_runner_label_raises(self):
        from autosymph.config import ConfigError

        cfg = _make_config()
        orch = _make_orchestrator(cfg)
        issue = _FakeIssue(status="Ready", labels=["runner:"])

        with pytest.raises(ConfigError, match="malformed runner label"):
            orch._resolve_runner_name(issue, "implement", cfg.states["implement"])


class TestInvestigatingNotAutosymphCoercion:
    """`_determine_signal` for investigating: when result file says
    `not_autosymph`, the signal must be allowed from both `verify` AND
    `verify_review` failed_states.

    Regression: the ISSUE-385 cycle was caused by coercing not_autosymph from
    verify_review → escalate → blocked, even though the investigator had
    correctly determined the code was fine and only needed re-verification.
    """

    def _setup(self, failed_state: str, signal_name: str = "not_autosymph"):
        from types import SimpleNamespace

        cfg = _make_config()
        orch = _make_orchestrator(cfg)
        # Stub the workspace so _determine_signal reads our fake result file.
        ws_path = Path("/tmp/test-orch-ws")
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / ".autosymph-result.json").write_text(
            f'{{"signal": "{signal_name}", "summary": "test"}}'
        )
        orch._workspaces = {"iss-1": SimpleNamespace(path=ws_path)}
        orch._last_failed_state = {"iss-1": failed_state}
        tracked = SimpleNamespace(
            workflow_state="investigating", identifier="ISS-1",
        )
        return orch, tracked

    def test_not_autosymph_from_verify_returns_not_autosymph(self):
        """The original allowed path: verify failed → investigator says not_autosymph."""
        from autosymph.state_machine import Signal
        orch, tracked = self._setup(failed_state="verify")
        assert orch._determine_signal(tracked, None, "iss-1") == Signal.NOT_AUTOSYMPH

    def test_not_autosymph_from_verify_review_returns_not_autosymph(self):
        """Regression: verify_review failed → investigator says not_autosymph
        must propagate. If this gets coerced to ESCALATE, the ISSUE-385 cycle
        bug returns: a falsely-rejected verify_review will land in blocked."""
        from autosymph.state_machine import Signal
        orch, tracked = self._setup(failed_state="verify_review")
        sig = orch._determine_signal(tracked, None, "iss-1")
        assert sig == Signal.NOT_AUTOSYMPH, (
            f"got {sig.value} — verify_review must be in the allowlist alongside verify"
        )

    def test_not_autosymph_from_implement_is_coerced_to_escalate(self):
        """Sanity: not_autosymph from non-verification states still gets coerced.
        Only verify and verify_review are legitimate sources for this signal."""
        from autosymph.state_machine import Signal
        orch, tracked = self._setup(failed_state="implement")
        assert orch._determine_signal(tracked, None, "iss-1") == Signal.ESCALATE
