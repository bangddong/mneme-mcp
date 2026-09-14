"""Agent-neutral lifecycle, command, domain, and operational contracts.

The four vocabularies are deliberately separate.  Lifecycle and operational
events are observations, commands are explicit requests, and Domain events are
in-memory facts returned only after a canonical transition succeeds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from types import MappingProxyType
from typing import Any, Final

from mneme.core.artifacts import StorageClass
from mneme.core.errors import (
    ArtifactExists,
    ConcurrentWrite,
    InvalidArtifact,
    MadiError,
    PortabilityViolation,
)
from mneme.core.policy import InvalidPolicy, PolicyError, UnknownPolicy
from mneme.core.registries import RegistryConflict
from mneme.core.validation.vault import validate_identifier


CONTRACT_VERSION: Final = 1
LIFECYCLE_NAMES: Final = frozenset(
    {
        "session_started",
        "session_resumed",
        "pre_compact",
        "milestone_reached",
        "session_ended",
        "agent_switched",
    }
)
CORE_COMMAND_NAMES: Final = frozenset(
    {
        "get_context",
        "open_session",
        "create_session_revision",
        "submit_memory",
        "promote_memory",
        "retire_memory",
        "set_active_heads",
        "set_preferred_head",
        "register_source",
        "create_policy_revision",
        "activate_policy_revision",
        "assign_project_policy",
        "assign_source_policy",
    }
)
DOMAIN_EVENT_NAMES: Final = frozenset(
    {
        "session_revision_created",
        "handoff_recorded",
        "memory_submitted",
        "memory_accepted",
        "memory_retired",
        "active_heads_changed",
        "preferred_head_changed",
        "source_registered",
        "policy_revision_created",
        "policy_revision_activated",
        "project_policy_assigned",
        "source_policy_assigned",
    }
)
OPERATIONAL_EVENT_NAMES: Final = frozenset(
    {
        "index_rebuilt",
        "source_unavailable",
        "sync_failed",
        "policy_reevaluation_failed",
        "doctor_issue_found",
    }
)
_RESULT_COMMAND_NAMES: Final = CORE_COMMAND_NAMES | frozenset(
    {"madi_recall", "unknown"}
)
_RESULT_STATUS_NAMES: Final = frozenset(
    {"degraded", "divergent", "error", "invalid", "resolved"}
)

_ERROR_MESSAGES: Final = {
    "policy-denied": (
        "Policy evaluation rejected the request; reduce portability or review "
        "the active policy."
    ),
    "policy-error": (
        "Policy evaluation could not be completed safely; run doctor and review "
        "the active policy."
    ),
    "registry-conflict": (
        "Canonical state changed; reload it and retry with explicit expected versions."
    ),
    "concurrent-write": (
        "Canonical state changed; reload it and retry with explicit expected versions."
    ),
    "artifact-conflict": (
        "The request conflicts with canonical state; choose a new identity or "
        "reload current state."
    ),
    "portability-violation": (
        "The request violates the portable/local boundary; reduce portability "
        "or remove local-only references."
    ),
    "invalid-artifact": (
        "The request or Vault artifact failed validation; run doctor and retry "
        "with valid input."
    ),
    "core-error": "The Core request failed safely; run doctor and review the command.",
    "internal-error": "The command failed safely; run doctor and retry.",
}


class PolicyDenied(MadiError):
    """A current canonical policy explicitly rejected an otherwise valid request."""


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    version: int
    name: str
    adapter: str
    session_id: str
    workstream_id: str | None = None

    def __post_init__(self) -> None:
        _validate_version(self.version)
        if self.name not in LIFECYCLE_NAMES:
            raise InvalidArtifact("lifecycle event name is invalid")
        validate_identifier(self.adapter, label="adapter id")
        validate_identifier(self.session_id, label="session id")
        if self.workstream_id is not None:
            validate_identifier(self.workstream_id, label="workstream id")

    def as_dict(self) -> dict[str, object]:
        return {
            "adapter": self.adapter,
            "name": self.name,
            "session_id": self.session_id,
            "version": self.version,
            "workstream_id": self.workstream_id,
        }


@dataclass(frozen=True, slots=True)
class CoreCommand:
    version: int
    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_version(self.version)
        if self.name not in CORE_COMMAND_NAMES:
            raise InvalidArtifact("Core command name is invalid")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "command payload"))


@dataclass(frozen=True, slots=True)
class DomainEvent:
    version: int
    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_version(self.version)
        if self.name not in DOMAIN_EVENT_NAMES:
            raise InvalidArtifact("Domain event name is invalid")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "Domain event payload"))

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "payload": _thaw_json(self.payload),
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class OperationalEvent:
    version: int
    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_version(self.version)
        if self.name not in OPERATIONAL_EVENT_NAMES:
            raise InvalidArtifact("Operational event name is invalid")
        object.__setattr__(
            self, "payload", _freeze_mapping(self.payload, "Operational event payload")
        )

    @property
    def storage_class(self) -> StorageClass:
        return StorageClass.LOCAL_ONLY

    @property
    def local_only(self) -> bool:
        return True

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "payload": _thaw_json(self.payload),
            "storage_class": self.storage_class.value,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class CommandError:
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.code not in _ERROR_MESSAGES or self.message != _ERROR_MESSAGES[self.code]:
            raise InvalidArtifact("command error must use a closed portable-safe error")

    @classmethod
    def from_exception(cls, error: Exception) -> CommandError:
        if isinstance(error, PolicyDenied):
            code = "policy-denied"
        elif isinstance(error, (UnknownPolicy, InvalidPolicy, PolicyError)):
            code = "policy-error"
        elif isinstance(error, RegistryConflict):
            code = "registry-conflict"
        elif isinstance(error, ConcurrentWrite):
            code = "concurrent-write"
        elif isinstance(error, ArtifactExists):
            code = "artifact-conflict"
        elif isinstance(error, PortabilityViolation):
            code = "portability-violation"
        elif isinstance(error, InvalidArtifact):
            code = "invalid-artifact"
        elif isinstance(error, MadiError):
            code = "core-error"
        else:
            code = "internal-error"
        return cls(code, _ERROR_MESSAGES[code])

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class CommandResult:
    version: int
    command: str
    ok: bool
    status: str
    result: Mapping[str, Any] | None = None
    domain_events: tuple[DomainEvent, ...] = ()
    operational_events: tuple[OperationalEvent, ...] = ()
    error: CommandError | None = None

    def __post_init__(self) -> None:
        _validate_version(self.version)
        if self.command not in _RESULT_COMMAND_NAMES:
            raise InvalidArtifact("command result name is invalid")
        if not isinstance(self.ok, bool):
            raise InvalidArtifact("command result ok must be a boolean")
        if self.status not in _RESULT_STATUS_NAMES:
            raise InvalidArtifact("command result status is invalid")
        if not isinstance(self.domain_events, tuple) or not all(
            isinstance(event, DomainEvent) for event in self.domain_events
        ):
            raise InvalidArtifact("command result Domain events are invalid")
        if not isinstance(self.operational_events, tuple) or not all(
            isinstance(event, OperationalEvent) for event in self.operational_events
        ):
            raise InvalidArtifact("command result Operational events are invalid")
        if self.ok:
            if self.error is not None or self.result is None or self.status == "error":
                raise InvalidArtifact("successful command result has an invalid envelope")
            object.__setattr__(self, "result", _freeze_mapping(self.result, "command result"))
        elif (
            not isinstance(self.error, CommandError)
            or self.result is not None
            or self.status != "error"
            or self.domain_events
            or self.operational_events
        ):
            raise InvalidArtifact("failed command result has an invalid envelope")

    @classmethod
    def success(
        cls,
        command: str,
        result: Mapping[str, Any],
        *,
        status: str = "resolved",
        domain_events: tuple[DomainEvent, ...] = (),
        operational_events: tuple[OperationalEvent, ...] = (),
    ) -> CommandResult:
        return cls(
            CONTRACT_VERSION,
            command,
            True,
            status,
            result,
            domain_events,
            operational_events,
            None,
        )

    @classmethod
    def failure(cls, command: str, error: Exception) -> CommandResult:
        return cls(
            CONTRACT_VERSION,
            command,
            False,
            "error",
            None,
            (),
            (),
            CommandError.from_exception(error),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "domain_events": [event.as_dict() for event in self.domain_events],
            "error": None if self.error is None else self.error.as_dict(),
            "ok": self.ok,
            "operational_events": [
                event.as_dict() for event in self.operational_events
            ],
            "result": None if self.result is None else _thaw_json(self.result),
            "status": self.status,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ObservationResult:
    version: int
    event: str
    checkpoint_required: bool
    context_required: bool
    domain_events: tuple[DomainEvent, ...] = ()

    def __post_init__(self) -> None:
        _validate_version(self.version)
        if self.event not in LIFECYCLE_NAMES:
            raise InvalidArtifact("observation result event is invalid")
        if not isinstance(self.checkpoint_required, bool) or not isinstance(
            self.context_required, bool
        ):
            raise InvalidArtifact("observation actions must be booleans")
        if self.domain_events:
            raise InvalidArtifact("lifecycle observations cannot return Domain events")

    @property
    def ok(self) -> bool:
        return True

    def as_dict(self) -> dict[str, object]:
        return {
            "checkpoint_required": self.checkpoint_required,
            "context_required": self.context_required,
            "domain_events": [],
            "event": self.event,
            "ok": True,
            "version": self.version,
        }


def _validate_version(version: object) -> None:
    if version != CONTRACT_VERSION or isinstance(version, bool):
        raise InvalidArtifact(f"contract version must be {CONTRACT_VERSION}")


def _freeze_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise InvalidArtifact(f"{label} must be a JSON object")
    return MappingProxyType({key: _freeze_json(item, label) for key, item in value.items()})


def _freeze_json(value: object, label: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidArtifact(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        return _freeze_mapping(value, label)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, label) for item in value)
    raise InvalidArtifact(f"{label} must contain only JSON values")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "CONTRACT_VERSION",
    "CORE_COMMAND_NAMES",
    "DOMAIN_EVENT_NAMES",
    "LIFECYCLE_NAMES",
    "OPERATIONAL_EVENT_NAMES",
    "CommandError",
    "CommandResult",
    "CoreCommand",
    "DomainEvent",
    "LifecycleEvent",
    "ObservationResult",
    "OperationalEvent",
]
