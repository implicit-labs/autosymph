"""workflow.yaml schema and parser.

Loads and validates the orchestrator configuration: tracker, polling,
workspace, hooks, agent defaults, prompt paths, and state machine definition.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class ConfigError(Exception):
    """Raised when workflow.yaml is invalid."""


# -- Schema models --


class TrackerConfig(BaseModel):
    kind: Literal["linear"] = "linear"
    project: str  # Project name (e.g. "autosymph") — resolved to ID at startup
    api_key: str = "$LINEAR_API_KEY"
    assignee_filter: str | None = None  # "me" or raw Linear user ID


class PollingConfig(BaseModel):
    interval_ms: int = Field(default=30_000, ge=5_000, le=300_000)


class TracingConfig(BaseModel):
    """Braintrust tracing — optional BYOK integration via BRAINTRUST_API_KEY."""

    project: str = "autosymph"
    api_key: str = "$BRAINTRUST_API_KEY"


class LoggingConfig(BaseModel):
    log_root: str = "~/.autosymph/logs"
    tracing: TracingConfig = Field(default_factory=TracingConfig)

    def resolved_log_root(self) -> Path:
        return Path(self.log_root).expanduser().resolve()


class WorkspaceConfig(BaseModel):
    root: str = "~/.autosymph/workspaces"
    repo: str = "."  # path to the git repo that worktrees are created from


class HooksConfig(BaseModel):
    after_create: str | None = None
    before_run: str | None = None
    after_complete: str | None = None
    on_failure: str | None = None
    timeout_ms: int = 180_000


class ClaudeConfig(BaseModel):
    permission_mode: str = "acceptEdits"
    # Default uses an alias so subscription users (OAuth, no ANTHROPIC_API_KEY)
    # work out of the box — aliases auto-float at dispatch time.
    # Pin to a specific id (e.g. "claude-sonnet-4-6") only when you need
    # reproducibility for a specific run; expect to bump explicitly via the
    # `autosymph-update-configs` skill or `autosymph models refresh`.
    model: str = "sonnet"
    max_turns: int = 30
    turn_timeout_ms: int = 3_600_000
    stall_timeout_ms: int = 300_000


class AgentConfig(BaseModel):
    max_concurrent_agents: int = Field(default=3, ge=1, le=10)
    max_retry_backoff_ms: int = 300_000
    max_concurrent_agents_by_state: dict[str, int] = Field(default_factory=dict)


RUNNER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class RunnerDefinition(BaseModel):
    """A named runner profile.

    Multiple profiles may share one adapter type (for example OMP subscription
    and OMP with an Anthropic API key) while keeping auth/session state isolated.
    Secret values are never stored here; only an environment variable name may
    be declared.
    """

    type: Literal["claude", "codex", "pi", "omp"]
    model: str | None = None
    auth_mode: Literal["subscription", "stored_profile", "environment"] = "subscription"
    auth_env: str | None = None
    profile: str | None = None
    permission_mode: str | None = None
    sandbox: Literal["read-only", "workspace-write"] | None = None
    max_time_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check_profile_contract(self) -> RunnerDefinition:
        if self.auth_mode == "environment" and not self.auth_env:
            raise ValueError("environment auth requires auth_env")
        if self.auth_mode != "environment" and self.auth_env:
            raise ValueError("auth_env is only valid for environment auth")
        if self.type == "omp" and not self.profile:
            raise ValueError("OMP runner profiles require profile")
        return self


class RunnerMatchRule(BaseModel):
    runner: str
    title: str | None = None
    labels: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_matcher_present(self) -> RunnerMatchRule:
        if not self.title and not self.labels:
            raise ValueError("runner auto_match rule must define title and/or labels")
        if self.title:
            try:
                re.compile(self.title)
            except re.error as exc:
                raise ValueError(f"invalid runner auto_match title regex: {exc}") from exc
        return self


class RunnersConfig(BaseModel):
    default: str = "claude"
    available: dict[str, RunnerDefinition] = Field(
        default_factory=lambda: {
            "claude": RunnerDefinition(type="claude"),
            "codex": RunnerDefinition(type="codex"),
        }
    )
    auto_match: list[RunnerMatchRule] = Field(default_factory=list)


class WorkflowCommentsConfig(BaseModel):
    """Workflow-level defaults for Linear stage comments."""

    activity_summary: bool = True


class StateCommentsConfig(BaseModel):
    """Per-state overrides for Linear stage comments. None = inherit workflow default."""

    activity_summary: bool | None = None


def resolve_activity_summary(workflow: WorkflowConfig, state: StateConfig) -> bool:
    """Tri-state resolution: state override wins when not None, else workflow default."""
    if state.comments is not None and state.comments.activity_summary is not None:
        return state.comments.activity_summary
    return workflow.comments.activity_summary


class StateTransitions(BaseModel):
    """Transitions map signal names to target state names."""

    model_config = {"extra": "allow"}

    def items(self) -> list[tuple[str, str]]:
        return list(self.__pydantic_extra__.items()) if self.__pydantic_extra__ else []


class StateScriptDefinition(BaseModel):
    phase: Literal["enter", "run", "validate", "exit", "recover", "guard", "effect"]
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_seconds: int = Field(ge=1)


class StateConfig(BaseModel):
    type: Literal["agent", "gate", "terminal"]
    prompt: str | None = None
    linear_state: str | None = None
    runner: str | None = None
    model: str | None = None
    max_turns: int | None = None
    session: str | None = None
    rework_to: str | None = None
    max_rework: int | None = None
    rework_exhausted: str | None = None
    transitions: StateTransitions | None = None
    permission_mode: str | None = None  # state-specific override (e.g. acceptEdits for verify)
    mcp_config: str | None = None  # path to MCP config JSON (--mcp-config flag)
    allowed_tools: str | None = None  # --allowedTools flag value
    comments: StateCommentsConfig | None = None  # per-state comment rendering overrides
    factory_root: str | None = None
    factory_revision: int | None = Field(default=None, ge=1)
    state_revision: int | None = Field(default=None, ge=1)
    definition_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scripts: list[StateScriptDefinition] = Field(default_factory=list)


class PromptsConfig(BaseModel):
    root: str | None = None
    global_prompt: str | None = None


class ServerConfig(BaseModel):
    port: int = 4200


class LinearStatesConfig(BaseModel):
    todo: str = "Ready"
    autoplan: str | None = None  # Optional — e.g. "Autoplan"; only polled when set
    active: str = "Implementing"
    verifying: str = "Verifying"
    verify_review: str | None = None  # Optional — "Reviewing Evidence"
    investigating: str = "Investigating"
    review: str = "In Review"
    gate_approved: str = "Merging"
    rework: str = "Rework"
    blocked: str = "Blocked"
    terminal: list[str] = Field(default_factory=lambda: ["Done", "Canceled", "Duplicate"])


class SimulatorConfig(BaseModel):
    name: str
    udid: str = "auto"


class RailwayConfig(BaseModel):
    preview_url_pattern: str = "https://{service}-pr-{pr_number}.up.railway.app"


class ResourcesConfig(BaseModel):
    ios_simulator: list[SimulatorConfig] = Field(default_factory=list)
    dev_port_range: list[int] = Field(default_factory=list)
    railway: RailwayConfig | None = None


class WorkflowConfig(BaseModel):
    """Top-level workflow.yaml schema."""

    machine_name: str | None = None
    tracker: TrackerConfig
    linear_states: LinearStatesConfig = Field(default_factory=LinearStatesConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    comments: WorkflowCommentsConfig = Field(default_factory=WorkflowCommentsConfig)
    runners: RunnersConfig = Field(default_factory=RunnersConfig)
    states: dict[str, StateConfig] = Field(default_factory=dict)

    @property
    def project_slug(self) -> str:
        """URL-safe slug derived from tracker.project name."""
        slug = re.sub(r"[^a-z0-9]+", "-", self.tracker.project.lower())
        return slug.strip("-")

    @model_validator(mode="after")
    def check_transition_targets(self) -> WorkflowConfig:
        """Ensure all transition targets reference defined states or special targets."""
        special = {"todo", "planning", "ready"}
        valid_targets = set(self.states.keys()) | special
        for name, state in self.states.items():
            if state.transitions:
                for signal, target in state.transitions.items():
                    if target not in valid_targets:
                        raise ValueError(
                            f"State '{name}' transition '{signal}' targets "
                            f"undefined state '{target}'"
                        )
            if state.rework_to and state.rework_to not in valid_targets:
                raise ValueError(
                    f"State '{name}' rework_to targets undefined state '{state.rework_to}'"
                )
            if state.rework_exhausted and state.rework_exhausted not in valid_targets:
                raise ValueError(
                    f"State '{name}' rework_exhausted targets undefined state "
                    f"'{state.rework_exhausted}'"
                )
        return self


# -- Device config (layered) --


class DeviceProjectOverride(BaseModel):
    """Per-project overrides in a device config."""

    repo: str  # path to the git repo on this device


class DeviceConfig(BaseModel):
    """Device-level config: environment, resources, per-project repo paths.

    Layout:
        ~/.autosymph/config/
          devices/{hostname}.yaml   — this file
          projects/{project}.yaml   — project workflow definitions

    The device config defines WHAT runs WHERE. Project configs define HOW.
    At startup, each project listed in device.projects gets merged with its
    project config to produce a full WorkflowConfig.
    """

    machine_name: str | None = None
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    projects: dict[str, DeviceProjectOverride] = Field(default_factory=dict)


def load_device_config(path: Path) -> DeviceConfig:
    """Parse a device YAML file."""
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error in device config: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("Device config must be a YAML mapping")
    try:
        return DeviceConfig(**raw)
    except Exception as e:
        raise ConfigError(str(e)) from e


def merge_device_project(
    device: DeviceConfig,
    project_cfg: WorkflowConfig,
    project_override: DeviceProjectOverride,
) -> WorkflowConfig:
    """Merge a project config with device-level overrides.

    Device provides: machine_name, resources, agent limits, repo path, logging, claude.
    Project provides: tracker, states, hooks, prompts, polling, linear_states.
    Device fields win when both are set (device is the environment layer).
    """
    # Deep copy project config as base
    merged_data = project_cfg.model_dump()

    # Apply device overrides
    merged_data["machine_name"] = device.machine_name
    merged_data["workspace"]["repo"] = project_override.repo

    # Device-level overrides: use model_fields_set to detect explicitly provided fields.
    # If the device config explicitly sets a section, it overrides the project default —
    # even if the value happens to match the schema default.
    if "resources" in device.model_fields_set:
        merged_data["resources"] = device.resources.model_dump()
    if "agent" in device.model_fields_set:
        merged_data["agent"] = device.agent.model_dump()
    if "logging" in device.model_fields_set:
        merged_data["logging"] = device.logging.model_dump()
    if "claude" in device.model_fields_set:
        merged_data["claude"] = device.claude.model_dump()

    return WorkflowConfig(**merged_data)


# -- Loader --


def load_config(path: Path) -> WorkflowConfig:
    """Parse a workflow.yaml file into a validated WorkflowConfig."""
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError("workflow.yaml must be a YAML mapping")

    try:
        return WorkflowConfig(**raw)
    except Exception as e:
        raise ConfigError(str(e)) from e


def validate_config(cfg: WorkflowConfig) -> None:
    """Run additional validation beyond pydantic schema checks."""
    if not cfg.states:
        raise ConfigError("No states defined — workflow.yaml must have at least one state")

    terminals = [name for name, s in cfg.states.items() if s.type == "terminal"]
    if not terminals:
        raise ConfigError("No terminal state defined — need at least one state with type: terminal")

    agent_states = [name for name, s in cfg.states.items() if s.type == "agent"]
    for name in agent_states:
        state = cfg.states[name]
        if not state.prompt:
            raise ConfigError(f"Agent state '{name}' is missing a prompt path")

    available = set(cfg.runners.available.keys())
    if not available:
        raise ConfigError("runners.available must define at least one runner")

    for name in available:
        if not RUNNER_NAME_RE.fullmatch(name):
            raise ConfigError(
                f"Runner name '{name}' is malformed; use lowercase letters, digits, '_' or '-'"
            )

    if cfg.runners.default not in available:
        raise ConfigError(
            f"runners.default references unknown runner '{cfg.runners.default}'"
        )

    for state_name, state in cfg.states.items():
        if state.runner and state.runner not in available:
            raise ConfigError(
                f"State '{state_name}' references unknown runner '{state.runner}'"
            )

    for idx, rule in enumerate(cfg.runners.auto_match):
        if rule.runner not in available:
            raise ConfigError(
                f"runners.auto_match[{idx}] references unknown runner '{rule.runner}'"
            )
        for label in rule.labels:
            if label.startswith("runner:"):
                runner_label = label.removeprefix("runner:")
                if not RUNNER_NAME_RE.fullmatch(runner_label):
                    raise ConfigError(
                        f"runners.auto_match[{idx}] has malformed runner label '{label}'"
                    )
