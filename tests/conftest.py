"""Shared test fixtures for autosymph."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from autosymph.config import (
    AgentConfig,
    ReliabilityConfig,
    StateConfig,
    StateTransitions,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)


@pytest.fixture
def mock_config_factory(tmp_path: Path):
    """Factory for generating valid WorkflowConfig with customizable fields."""

    def _make(
        project: str = "test-project",
        repo: str = "/tmp/test-repo",
        api_key: str = "test-key",
        max_agents: int = 3,
        **overrides: Any,
    ) -> WorkflowConfig:
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project=project, api_key=api_key),
            workspace=WorkspaceConfig(root="/tmp/test-workspaces", repo=repo),
            reliability=ReliabilityConfig(
                database_path=str(tmp_path / f"{project}-ledger.sqlite3")
            ),
            agent=AgentConfig(max_concurrent_agents=max_agents),
            states={
                "implement": StateConfig(
                    type="agent",
                    prompt="prompts/implement.md",
                    transitions=StateTransitions(**{"complete": "done", "fail": "done"}),
                ),
                "done": StateConfig(type="terminal"),
            },
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    return _make


@pytest.fixture
def mock_linear_client():
    """Mock LinearClient with no real HTTP."""
    client = MagicMock()
    client.resolve_project = AsyncMock(return_value="project-id-123")
    client.fetch_actionable_issues = AsyncMock(return_value=[])
    client.transition_issue = AsyncMock()
    client.post_comment = AsyncMock()
    client.close = AsyncMock()
    client.is_configured = True
    client.project_name = "test-project"
    return client


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Create a temp directory with N YAML config files for discovery tests."""

    def _make(configs: dict[str, dict] | None = None) -> Path:
        config_dir = tmp_path / "configs"
        config_dir.mkdir()

        if configs is None:
            # Default: 2 valid configs
            configs = {
                "project-a.yaml": {
                    "tracker": {"project": "Project A", "api_key": "key-a"},
                    "states": {
                        "implement": {"type": "agent", "prompt": "p.md", "transitions": {"complete": "done"}},
                        "done": {"type": "terminal"},
                    },
                },
                "project-b.yaml": {
                    "tracker": {"project": "Project B", "api_key": "key-b"},
                    "states": {
                        "implement": {"type": "agent", "prompt": "p.md", "transitions": {"complete": "done"}},
                        "done": {"type": "terminal"},
                    },
                },
            }

        for name, content in configs.items():
            (config_dir / name).write_text(yaml.dump(content))

        return config_dir

    return _make


@pytest.fixture
def shutdown_event():
    """Provide a fresh asyncio.Event for shutdown signaling."""
    return asyncio.Event()
