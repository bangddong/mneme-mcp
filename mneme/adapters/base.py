"""Small, host-neutral types shared by optional Madi adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mneme.core.contracts import (
    CONTRACT_VERSION,
    LIFECYCLE_NAMES,
    CommandResult,
    DomainEvent,
    LifecycleEvent,
    OperationalEvent,
)
from mneme.core.errors import InvalidArtifact


_REQUIRED_ENVELOPE_FIELDS = frozenset(
    {"version", "event", "adapter", "session_id", "workstream_id"}
)
_OPTIONAL_ENVELOPE_FIELDS = frozenset({"checkpoint_file"})


@dataclass(frozen=True, slots=True)
class AdapterEnvelope:
    """Normalized host input; its optional file is never a Core payload field."""

    version: int
    event: str
    adapter: str
    session_id: str
    workstream_id: str
    checkpoint_file: str | None = None

    @classmethod
    def from_mapping(cls, value: object) -> "AdapterEnvelope":
        if not isinstance(value, Mapping) or not all(
            isinstance(key, str) for key in value
        ):
            raise InvalidArtifact("adapter envelope must be a JSON object")
        fields = frozenset(value)
        if not _REQUIRED_ENVELOPE_FIELDS.issubset(fields) or not fields.issubset(
            _REQUIRED_ENVELOPE_FIELDS | _OPTIONAL_ENVELOPE_FIELDS
        ):
            raise InvalidArtifact("adapter envelope fields are invalid")
        checkpoint_file = value.get("checkpoint_file")
        if checkpoint_file is not None and (
            not isinstance(checkpoint_file, str) or not checkpoint_file.strip()
        ):
            raise InvalidArtifact("checkpoint file must be non-empty text or null")
        if not isinstance(value["workstream_id"], str):
            raise InvalidArtifact("workstream id must be text")
        envelope = cls(
            version=value["version"],
            event=value["event"],
            adapter=value["adapter"],
            session_id=value["session_id"],
            workstream_id=value["workstream_id"],
            checkpoint_file=checkpoint_file,
        )
        # Reuse the Core contract's closed lifecycle validation without allowing
        # host-native names into Core itself.
        envelope.as_lifecycle_event()
        return envelope

    def as_lifecycle_event(self) -> LifecycleEvent:
        return LifecycleEvent(
            self.version,
            self.event,
            self.adapter,
            self.session_id,
            self.workstream_id,
        )


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """A closed, non-blocking outcome for a host-agent lifecycle boundary."""

    version: int
    event: str
    ok: bool
    block_host: bool
    checkpoint_required: bool
    context_required: bool
    warning: str | None = None
    command_result: CommandResult | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != CONTRACT_VERSION:
            raise InvalidArtifact("adapter result has an invalid version")
        if self.event not in LIFECYCLE_NAMES | {"unknown"}:
            raise InvalidArtifact("adapter result event is invalid")
        if not isinstance(self.ok, bool) or self.block_host is not False:
            raise InvalidArtifact("adapter result must never block the host")
        if not isinstance(self.checkpoint_required, bool) or not isinstance(
            self.context_required, bool
        ):
            raise InvalidArtifact("adapter result guidance must be boolean")
        if self.ok:
            if self.warning is not None:
                raise InvalidArtifact("successful adapter result cannot have a warning")
        elif not isinstance(self.warning, str) or not self.warning:
            raise InvalidArtifact("failed adapter result requires a closed warning")
        if self.command_result is not None and not isinstance(
            self.command_result, CommandResult
        ):
            raise InvalidArtifact("adapter result command result is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "block_host": False,
            "checkpoint_required": self.checkpoint_required,
            "command_result": (
                None
                if self.command_result is None
                else self.command_result.as_dict()
            ),
            "context_required": self.context_required,
            "event": self.event,
            "ok": self.ok,
            "version": self.version,
            "warning": self.warning,
        }

    @property
    def domain_events(self) -> tuple[DomainEvent, ...]:
        """Expose successful mutation facts without widening the adapter envelope."""
        if self.command_result is None:
            return ()
        return self.command_result.domain_events

    @property
    def operational_events(self) -> tuple[OperationalEvent, ...]:
        if self.command_result is None:
            return ()
        return self.command_result.operational_events


__all__ = ["AdapterEnvelope", "AdapterResult"]
