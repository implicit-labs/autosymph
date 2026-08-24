"""Tests for config schema changes and config discovery."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from autosymph.config import (
    ConfigError,
    DeviceConfig,
    DeviceProjectOverride,
    LinearStatesConfig,
    RunnerDefinition,
    TrackerConfig,
    WorkflowConfig,
    StateConfig,
    merge_device_project,
    validate_config,
)


# -- Schema tests --


class TestAssigneeFilter:
    def test_assignee_filter_present(self):
        cfg = TrackerConfig(project="test", api_key="k", assignee_filter="me")
        assert cfg.assignee_filter == "me"

    def test_assignee_filter_raw_id(self):
        cfg = TrackerConfig(project="test", api_key="k", assignee_filter="user-id-123")
        assert cfg.assignee_filter == "user-id-123"

    def test_assignee_filter_default_none(self):
        cfg = TrackerConfig(project="test", api_key="k")
        assert cfg.assignee_filter is None


class TestLinearStatesAutoplan:
    """Tests for the optional `autoplan` field on LinearStatesConfig (ISSUE-356)."""

    def test_autoplan_default_none(self):
        """When omitted, autoplan defaults to None (preserves backward compat)."""
        cfg = LinearStatesConfig()
        assert cfg.autoplan is None

    def test_autoplan_explicit_value(self):
        """When set, autoplan stores the configured Linear state name."""
        cfg = LinearStatesConfig(autoplan="Autoplan")
        assert cfg.autoplan == "Autoplan"


class TestRunnerConfig:
    def test_default_runners_preserve_claude_default(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            states={"done": StateConfig(type="terminal")},
        )

        validate_config(cfg)
        assert cfg.runners.default == "claude"
        assert set(cfg.runners.available) == {"claude", "codex"}

    def test_state_runner_reference_is_validated(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            runners={"available": {"claude": {"type": "claude"}}, "default": "claude"},
            states={
                "implement": StateConfig(type="agent", prompt="p.md", runner="codex"),
                "done": StateConfig(type="terminal"),
            },
        )

        with pytest.raises(ConfigError, match="State 'implement'.*unknown runner 'codex'"):
            validate_config(cfg)

    def test_auto_match_runner_reference_is_validated(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            runners={
                "available": {"claude": {"type": "claude"}},
                "default": "claude",
                "auto_match": [{"runner": "pi", "title": "cleanup"}],
            },
            states={"done": StateConfig(type="terminal")},
        )

        with pytest.raises(ConfigError, match="auto_match\\[0\\].*unknown runner 'pi'"):
            validate_config(cfg)

    def test_default_runner_reference_is_validated(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            runners={"available": {"claude": {"type": "claude"}}, "default": "codex"},
            states={"done": StateConfig(type="terminal")},
        )

        with pytest.raises(ConfigError, match="runners.default.*unknown runner 'codex'"):
            validate_config(cfg)

    def test_malformed_static_runner_label_is_rejected(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="test", api_key="k"),
            runners={
                "default": "claude",
                "auto_match": [{"runner": "claude", "labels": ["runner:"]}],
            },
            states={"done": StateConfig(type="terminal")},
        )

        with pytest.raises(ConfigError, match="malformed runner label 'runner:'"):
            validate_config(cfg)

    def test_named_omp_profiles_keep_auth_separate(self):
        subscription = RunnerDefinition(
            type="omp", auth_mode="stored_profile", profile="claude-subscription"
        )
        api = RunnerDefinition(
            type="omp",
            auth_mode="environment",
            auth_env="ANTHROPIC_API_KEY",
            profile="claude-api",
        )

        assert subscription.auth_env is None
        assert api.auth_env == "ANTHROPIC_API_KEY"

    def test_environment_auth_requires_env_name(self):
        with pytest.raises(ValueError, match="environment auth requires auth_env"):
            RunnerDefinition(type="omp", auth_mode="environment", profile="claude-api")

    def test_omp_requires_profile(self):
        with pytest.raises(ValueError, match="OMP runner profiles require profile"):
            RunnerDefinition(type="omp")


class TestProjectSlug:
    def test_simple_name(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="demo", api_key="k"),
            states={"done": StateConfig(type="terminal")},
        )
        assert cfg.project_slug == "demo"

    def test_capitalized_name(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="Demo", api_key="k"),
            states={"done": StateConfig(type="terminal")},
        )
        assert cfg.project_slug == "demo"

    def test_spaces_and_special_chars(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="My Cool Project!", api_key="k"),
            states={"done": StateConfig(type="terminal")},
        )
        assert cfg.project_slug == "my-cool-project"

    def test_multiple_dashes_collapsed(self):
        cfg = WorkflowConfig(
            tracker=TrackerConfig(project="foo---bar", api_key="k"),
            states={"done": StateConfig(type="terminal")},
        )
        assert cfg.project_slug == "foo-bar"


# -- Config discovery tests --


class TestFindConfigs:
    """Tests for _find_configs() in cli.py."""

    def _make_valid_yaml(self, path: Path, project: str = "test") -> None:
        """Write a minimal valid workflow YAML."""
        data = {
            "tracker": {"project": project, "api_key": "test-key"},
            "states": {
                "implement": {
                    "type": "agent",
                    "prompt": "p.md",
                    "transitions": {"complete": "done"},
                },
                "done": {"type": "terminal"},
            },
        }
        path.write_text(yaml.dump(data))

    def test_scans_dir(self, tmp_path, monkeypatch):
        """Scanning a config dir returns all valid YAMLs."""
        from autosymph.cli import _find_configs

        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        self._make_valid_yaml(config_dir / "a.yaml", "Project A")
        self._make_valid_yaml(config_dir / "b.yaml", "Project B")
        (config_dir / "readme.txt").write_text("not a yaml")

        monkeypatch.setenv("AUTOSYMPH_CONFIG_DIR", str(config_dir))
        configs, warnings = _find_configs(None)
        assert len(configs) == 2
        assert len(warnings) == 0
        slugs = {cfg.project_slug for _, cfg in configs}
        assert slugs == {"project-a", "project-b"}

    def test_single_c(self, tmp_path):
        """Explicit -c path returns exactly 1 config."""
        from autosymph.cli import _find_configs

        yaml_path = tmp_path / "single.yaml"
        self._make_valid_yaml(yaml_path, "Solo")
        configs, warnings = _find_configs(str(yaml_path))
        assert len(configs) == 1
        assert configs[0][1].project_slug == "solo"
        assert warnings == []

    def test_skips_invalid(self, tmp_path, monkeypatch):
        """Invalid YAMLs are skipped with a warning."""
        from autosymph.cli import _find_configs

        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        self._make_valid_yaml(config_dir / "good.yaml", "Good")
        (config_dir / "bad.yaml").write_text("not: valid: yaml: [")

        monkeypatch.setenv("AUTOSYMPH_CONFIG_DIR", str(config_dir))
        configs, warnings = _find_configs(None)
        assert len(configs) == 1
        assert configs[0][1].project_slug == "good"
        assert len(warnings) == 1
        assert "bad.yaml" in warnings[0]

    def test_empty_dir_fallback(self, tmp_path, monkeypatch):
        """Empty config dir with no workflow.yaml raises error."""
        import click
        from autosymph.cli import _find_configs

        config_dir = tmp_path / "empty"
        config_dir.mkdir()
        monkeypatch.setenv("AUTOSYMPH_CONFIG_DIR", str(config_dir))
        monkeypatch.chdir(tmp_path)  # No workflow.yaml here

        with pytest.raises(click.ClickException, match="No configs found"):
            _find_configs(None)

    def test_explicit_missing_file(self, tmp_path):
        """Explicit -c with nonexistent file raises error."""
        import click
        from autosymph.cli import _find_configs

        with pytest.raises(click.ClickException, match="not found"):
            _find_configs(str(tmp_path / "nonexistent.yaml"))


class TestLocalEnv:
    """Tests for config-local local.env loading."""

    def test_loads_local_env_without_overriding_shell(self, tmp_path, monkeypatch):
        from autosymph.cli import _load_local_env

        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        (config_dir / "local.env").write_text(
            "\n".join([
                "# local secrets",
                "PROJECT_E2E_SECRET=file-secret",
                "export LINEAR_API_KEY='file-linear'",
                "IGNORED_LINE",
            ])
        )
        monkeypatch.setenv("LINEAR_API_KEY", "shell-linear")
        monkeypatch.delenv("PROJECT_E2E_SECRET", raising=False)

        loaded = _load_local_env(config_dir)

        assert loaded == config_dir / "local.env"
        assert os.environ["PROJECT_E2E_SECRET"] == "file-secret"
        assert os.environ["LINEAR_API_KEY"] == "shell-linear"

    def test_local_env_can_set_config_dir_before_discovery(self, tmp_path, monkeypatch):
        from autosymph.cli import _find_configs, _load_local_env

        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        TestFindConfigs()._make_valid_yaml(config_dir / "workflow.yaml", "Env Project")

        bootstrap_dir = tmp_path / "bootstrap"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "local.env").write_text(f"AUTOSYMPH_CONFIG_DIR={config_dir}\n")

        monkeypatch.delenv("AUTOSYMPH_CONFIG_DIR", raising=False)
        _load_local_env(bootstrap_dir)

        configs, warnings = _find_configs(None)

        assert warnings == []
        assert len(configs) == 1
        assert configs[0][1].project_slug == "env-project"


# -- Layered config tests --


class TestLayeredConfig:
    """Tests for device + project config merging."""

    def _make_project_yaml(self, path: Path, project: str = "test") -> None:
        data = {
            "tracker": {"project": project, "api_key": "test-key"},
            "states": {
                "implement": {
                    "type": "agent",
                    "prompt": "p.md",
                    "transitions": {"complete": "done"},
                },
                "done": {"type": "terminal"},
            },
        }
        path.write_text(yaml.dump(data))

    def _make_device_yaml(self, path: Path, projects: dict[str, str]) -> None:
        data = {
            "machine_name": "test-machine",
            "resources": {
                "ios_simulator": [{"name": "iPhone 17 Pro"}],
                "dev_port_range": [3001, 3002],
            },
            "agent": {"max_concurrent_agents": 5},
            "projects": {slug: {"repo": repo} for slug, repo in projects.items()},
        }
        path.write_text(yaml.dump(data))

    def test_merge_applies_device_repo(self):
        """Device override sets workspace.repo."""
        project_cfg = WorkflowConfig(
            tracker=TrackerConfig(project="Demo", api_key="k"),
            states={"done": StateConfig(type="terminal")},
        )
        device = DeviceConfig(
            machine_name="my-laptop",
            projects={"demo": DeviceProjectOverride(repo="~/code/demo-app")},
        )
        override = device.projects["demo"]
        merged = merge_device_project(device, project_cfg, override)

        assert merged.workspace.repo == "~/code/demo-app"
        assert merged.machine_name == "my-laptop"
        assert merged.tracker.project == "Demo"

    def test_merge_applies_device_resources(self):
        """Device resources override project defaults."""
        from autosymph.config import ResourcesConfig, SimulatorConfig

        project_cfg = WorkflowConfig(
            tracker=TrackerConfig(project="Test", api_key="k"),
            states={"done": StateConfig(type="terminal")},
        )
        device = DeviceConfig(
            resources=ResourcesConfig(
                ios_simulator=[SimulatorConfig(name="iPhone 17")],
                dev_port_range=[3001],
            ),
            projects={"test": DeviceProjectOverride(repo="/repo")},
        )
        override = device.projects["test"]
        merged = merge_device_project(device, project_cfg, override)

        assert len(merged.resources.ios_simulator) == 1
        assert merged.resources.ios_simulator[0].name == "iPhone 17"

    def test_merge_preserves_project_states(self):
        """Project states survive the merge."""
        project_cfg = WorkflowConfig(
            tracker=TrackerConfig(project="Test", api_key="k"),
            states={
                "implement": StateConfig(type="agent", prompt="p.md"),
                "verify": StateConfig(type="agent", prompt="v.md"),
                "done": StateConfig(type="terminal"),
            },
        )
        device = DeviceConfig(projects={"test": DeviceProjectOverride(repo="/r")})
        override = device.projects["test"]
        merged = merge_device_project(device, project_cfg, override)

        assert set(merged.states.keys()) == {"implement", "verify", "done"}

    def test_layered_discovery(self, tmp_path, monkeypatch):
        """devices/ + projects/ dirs trigger layered config loading."""
        import socket
        from autosymph.cli import _find_configs

        config_dir = tmp_path / "configs"
        devices_dir = config_dir / "devices"
        projects_dir = config_dir / "projects"
        devices_dir.mkdir(parents=True)
        projects_dir.mkdir(parents=True)

        # Mock hostname
        hostname = socket.gethostname().split(".")[0].lower()
        self._make_device_yaml(
            devices_dir / f"{hostname}.yaml",
            projects={"alpha": "/repos/alpha", "beta": "/repos/beta"},
        )
        self._make_project_yaml(projects_dir / "alpha.yaml", "Alpha")
        self._make_project_yaml(projects_dir / "beta.yaml", "Beta")

        monkeypatch.setenv("AUTOSYMPH_CONFIG_DIR", str(config_dir))
        configs, warnings = _find_configs(None)
        assert len(configs) == 2
        slugs = {cfg.project_slug for _, cfg in configs}
        assert slugs == {"alpha", "beta"}
        # Device overrides applied
        repos = {cfg.workspace.repo for _, cfg in configs}
        assert "/repos/alpha" in repos
        assert "/repos/beta" in repos

    def test_layered_missing_project_warns(self, tmp_path, monkeypatch):
        """Missing project file produces a warning, not a crash."""
        import socket
        from autosymph.cli import _find_configs

        config_dir = tmp_path / "configs"
        devices_dir = config_dir / "devices"
        projects_dir = config_dir / "projects"
        devices_dir.mkdir(parents=True)
        projects_dir.mkdir(parents=True)

        hostname = socket.gethostname().split(".")[0].lower()
        self._make_device_yaml(
            devices_dir / f"{hostname}.yaml",
            projects={"exists": "/repos/exists", "missing": "/repos/missing"},
        )
        self._make_project_yaml(projects_dir / "exists.yaml", "Exists")
        # "missing.yaml" deliberately not created

        monkeypatch.setenv("AUTOSYMPH_CONFIG_DIR", str(config_dir))
        configs, warnings = _find_configs(None)
        assert len(configs) == 1
        assert configs[0][1].project_slug == "exists"
        assert any("missing" in w for w in warnings)
