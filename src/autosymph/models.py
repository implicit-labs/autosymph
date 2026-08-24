"""Model registry — single source of truth for which Claude models are 'current'.

Updated by `autosymph models refresh`, which queries Anthropic's /v1/models API.
Validated by `autosymph models check` and the validate_config pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autosymph.config import WorkflowConfig

# Latest model id per family. Updated by `autosymph models refresh --apply`.
LATEST: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# Known stale ids → suggested current replacement.
# Refresh keeps this in sync with LATEST: every value here MUST be in LATEST.values().
KNOWN_STALE: dict[str, str] = {
    "claude-opus-4-6": "claude-opus-4-7",
    "claude-opus-4-5": "claude-opus-4-7",
    "claude-opus-4-1": "claude-opus-4-7",
    "claude-opus-4-0": "claude-opus-4-7",
    "claude-sonnet-4-5": "claude-sonnet-4-6",
    "claude-sonnet-4-0": "claude-sonnet-4-6",
    "claude-haiku-4-0": "claude-haiku-4-5-20251001",
}

_FAMILIES: tuple[str, ...] = ("opus", "sonnet", "haiku")

# Bare aliases the `claude` CLI accepts as `--model <alias>`. These auto-float
# to whatever the harness considers current at dispatch time, so they're
# subscription-friendly (no API key needed) and never drift. Recommended over
# pinned ids unless you specifically need reproducibility.
ALIASES: frozenset[str] = frozenset(_FAMILIES)


def family_of(model_id: str) -> str | None:
    """Return 'opus' / 'sonnet' / 'haiku' for the given id, or None if unrecognized.

    Bare aliases resolve to themselves: `family_of("opus") == "opus"`. This lets
    the skill's inventory phase bucket aliases alongside pinned ids by family.
    """
    if not model_id:
        return None
    if model_id in ALIASES:
        return model_id
    for family in _FAMILIES:
        if f"-{family}-" in model_id:
            return family
    return None


def is_alias(model_id: str) -> bool:
    """True if the model id is a bare alias (`opus` / `sonnet` / `haiku`).

    Aliases auto-float at dispatch time and are never stale — `models check`
    skips them, the refresh skill leaves them alone.
    """
    return model_id in ALIASES


def is_stale(model_id: str) -> bool:
    """True if the model id is a known-stale id with a documented replacement.

    Aliases are never stale (they auto-float). Unknown ids are not stale either
    — only ids explicitly in KNOWN_STALE return True.
    """
    return model_id in KNOWN_STALE


def suggested_replacement(model_id: str) -> str | None:
    """Return the suggested current replacement, or None if the id isn't stale."""
    return KNOWN_STALE.get(model_id)


@dataclass(frozen=True)
class StaleModelRef:
    """A reference to a stale model id found inside a WorkflowConfig."""

    state_name: str  # state name, or "<default>" for the workflow-level claude.model
    model_id: str  # the stale id
    suggested: str  # the LATEST replacement
    field: str  # dotted path within the config — e.g. "states.verify.model"


def collect_stale_models(cfg: "WorkflowConfig") -> list[StaleModelRef]:
    """Walk a WorkflowConfig and collect every stale model reference.

    Reports the workflow-level default (claude.model) and every per-state
    override (states.<name>.model). States that inherit (model=None) do NOT
    produce duplicate refs — the default is reported once.
    """
    refs: list[StaleModelRef] = []

    if is_stale(cfg.claude.model):
        replacement = suggested_replacement(cfg.claude.model)
        assert replacement is not None  # is_stale → in KNOWN_STALE → has replacement
        refs.append(
            StaleModelRef(
                state_name="<default>",
                model_id=cfg.claude.model,
                suggested=replacement,
                field="claude.model",
            )
        )

    for name, state in cfg.states.items():
        if state.model and is_stale(state.model):
            replacement = suggested_replacement(state.model)
            assert replacement is not None
            refs.append(
                StaleModelRef(
                    state_name=name,
                    model_id=state.model,
                    suggested=replacement,
                    field=f"states.{name}.model",
                )
            )

    return refs


# -- Refresh: parse Anthropic /v1/models response → LATEST mapping --


def _parse_created_at(value: Any) -> datetime:
    """Parse an Anthropic created_at timestamp. Handles 'Z' suffix and naive ISO strings."""
    if isinstance(value, datetime):
        return value
    s = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def pick_latest_per_family(api_response: dict[str, Any]) -> dict[str, str]:
    """Reduce a /v1/models API response to a {family: latest_id} mapping.

    Picks the model with the most recent `created_at` per family. Models that
    don't match opus/sonnet/haiku are ignored. Families with zero matches are
    omitted from the result.
    """
    by_family: dict[str, tuple[datetime, str]] = {}
    for entry in api_response.get("data", []):
        model_id = entry.get("id")
        if not isinstance(model_id, str):
            continue
        family = family_of(model_id)
        if family is None:
            continue
        created = _parse_created_at(entry.get("created_at"))
        existing = by_family.get(family)
        if existing is None or created > existing[0]:
            by_family[family] = (created, model_id)
    return {family: pair[1] for family, pair in by_family.items()}


@dataclass(frozen=True)
class RegistryUpdate:
    """Describes the diff between current registry and a refreshed one from the API."""

    new_latest: dict[str, str]
    new_stale: dict[str, str]
    bumped_families: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.bumped_families)


def compute_registry_update(
    *,
    current_latest: dict[str, str],
    current_stale: dict[str, str],
    api_latest: dict[str, str],
) -> RegistryUpdate:
    """Compute the new registry given the current one + the API's latest.

    Rules:
    - For each family in api_latest where the id differs from current_latest,
      the family is "bumped." The previous current_latest id moves into stale.
    - Existing stale entries are retargeted: any entry whose replacement was a
      now-bumped family's old latest is updated to point at the new latest.
    - The new latest id never appears in new_stale (idempotency invariant).
    """
    bumped: dict[str, str] = {}  # family → old latest (the one we're demoting)
    new_latest = dict(current_latest)
    for family, new_id in api_latest.items():
        old_id = current_latest.get(family)
        if old_id != new_id:
            if old_id is not None:
                bumped[family] = old_id
            new_latest[family] = new_id

    new_stale: dict[str, str] = {}

    # Retarget existing stale entries first so old replacements get updated
    for stale_id, replacement in current_stale.items():
        stale_family = family_of(stale_id)
        if stale_family and stale_family in api_latest:
            new_stale[stale_id] = api_latest[stale_family]
        else:
            new_stale[stale_id] = replacement

    # Add demoted ids as new stale entries
    for family, old_id in bumped.items():
        new_stale[old_id] = api_latest[family]

    # Invariant: a current id must never be marked stale
    for current_id in new_latest.values():
        new_stale.pop(current_id, None)

    return RegistryUpdate(
        new_latest=new_latest,
        new_stale=new_stale,
        bumped_families=sorted(bumped.keys()),
    )


_MODELS_PY_TEMPLATE = '''"""Model registry — single source of truth for which Claude models are 'current'.

Updated by `autosymph models refresh`, which queries Anthropic's /v1/models API.
Validated by `autosymph models check` and the validate_config pass.

This file is rewritten by `autosymph models refresh --apply`. Hand-edits to
LATEST or KNOWN_STALE will be overwritten on the next refresh — change the
refresh logic in models.py instead, or pin via PR review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autosymph.config import WorkflowConfig


# Latest model id per family. Updated by `autosymph models refresh --apply`.
LATEST: dict[str, str] = {latest_repr}

# Known stale ids → suggested current replacement.
# Refresh keeps this in sync with LATEST: every value here MUST be in LATEST.values().
KNOWN_STALE: dict[str, str] = {stale_repr}

_FAMILIES: tuple[str, ...] = ("opus", "sonnet", "haiku")


def family_of(model_id: str) -> str | None:
    """Return 'opus' / 'sonnet' / 'haiku' for the given id, or None if unrecognized."""
    if not model_id:
        return None
    for family in _FAMILIES:
        if f"-{{family}}-" in model_id:
            return family
    return None


def is_stale(model_id: str) -> bool:
    """True if the model id is a known-stale id with a documented replacement."""
    return model_id in KNOWN_STALE


def suggested_replacement(model_id: str) -> str | None:
    """Return the suggested current replacement, or None if the id isn't stale."""
    return KNOWN_STALE.get(model_id)


@dataclass(frozen=True)
class StaleModelRef:
    """A reference to a stale model id found inside a WorkflowConfig."""

    state_name: str
    model_id: str
    suggested: str
    field: str


def collect_stale_models(cfg: "WorkflowConfig") -> list[StaleModelRef]:
    """Walk a WorkflowConfig and collect every stale model reference."""
    refs: list[StaleModelRef] = []

    if is_stale(cfg.claude.model):
        replacement = suggested_replacement(cfg.claude.model)
        assert replacement is not None
        refs.append(
            StaleModelRef(
                state_name="<default>",
                model_id=cfg.claude.model,
                suggested=replacement,
                field="claude.model",
            )
        )

    for name, state in cfg.states.items():
        if state.model and is_stale(state.model):
            replacement = suggested_replacement(state.model)
            assert replacement is not None
            refs.append(
                StaleModelRef(
                    state_name=name,
                    model_id=state.model,
                    suggested=replacement,
                    field=f"states.{{name}}.model",
                )
            )

    return refs


def _parse_created_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    s = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def pick_latest_per_family(api_response: dict[str, Any]) -> dict[str, str]:
    """Reduce a /v1/models API response to a {{family: latest_id}} mapping."""
    by_family: dict[str, tuple[datetime, str]] = {{}}
    for entry in api_response.get("data", []):
        model_id = entry.get("id")
        if not isinstance(model_id, str):
            continue
        family = family_of(model_id)
        if family is None:
            continue
        created = _parse_created_at(entry.get("created_at"))
        existing = by_family.get(family)
        if existing is None or created > existing[0]:
            by_family[family] = (created, model_id)
    return {{family: pair[1] for family, pair in by_family.items()}}
'''


def _format_dict_literal(d: dict[str, str]) -> str:
    """Render a dict literal with stable key order and 4-space indent."""
    if not d:
        return "{}"
    lines = ["{"]
    for key in sorted(d.keys()):
        lines.append(f'    "{key}": "{d[key]}",')
    lines.append("}")
    return "\n".join(lines)


def write_models_py(target: Path, *, latest: dict[str, str], known_stale: dict[str, str]) -> None:
    """Rewrite models.py at `target` with the given LATEST and KNOWN_STALE constants.

    The rewritten file preserves family_of / is_stale / suggested_replacement /
    collect_stale_models / pick_latest_per_family, but DROPS compute_registry_update
    and write_models_py — those live only in the checked-in source file.
    A subsequent refresh will use the repository version, not the rewritten one.
    """
    contents = _MODELS_PY_TEMPLATE.format(
        latest_repr=_format_dict_literal(latest),
        stale_repr=_format_dict_literal(known_stale),
    )
    target.write_text(contents)
