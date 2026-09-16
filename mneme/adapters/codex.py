"""Codex-native lifecycle translation at the agent-neutral Core boundary.

Only this adapter knows the pinned native ``PreCompact`` shape.  Native host
paths and identities are validated in memory, never opened, and never copied
into the plain :class:`LifecycleEvent` passed to Core.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from mneme.adapters.base import AdapterResult
from mneme.adapters.claude import ClaudeAdapter
from mneme.core.contracts import CONTRACT_VERSION, LifecycleEvent
from mneme.core.errors import InvalidArtifact


SCHEMA_ID: Final = "openai-codex-hooks/pre-compact@2026-08-26+a26f1806"
_REQUIRED_NATIVE_FIELDS: Final = frozenset(
    {
        "session_id",
        "transcript_path",
        "cwd",
        "hook_event_name",
        "model",
        "turn_id",
        "trigger",
    }
)
_OPTIONAL_NATIVE_FIELDS: Final = frozenset({"agent_id", "agent_type"})
_BINDING_FIELDS: Final = frozenset({"workstream_id"})
_TRIGGERS: Final = frozenset({"manual", "auto"})
_INVALID_ENVELOPE_WARNING: Final = (
    "Madi could not use this Codex lifecycle event; continue ordinary Codex work."
)
_INVALID_CHECKPOINT_WARNING: Final = (
    "Madi could not use the selected checkpoint; continue ordinary Codex work."
)
_UNAVAILABLE_WARNING: Final = "Madi is unavailable; continue ordinary Codex work."


@dataclass(frozen=True, slots=True)
class CodexLifecycleEvent(LifecycleEvent):
    """Adapter-local event with native metadata excluded from Core serialization."""

    trigger: str = ""
    turn_id: str = ""

    def __post_init__(self) -> None:
        LifecycleEvent.__post_init__(self)
        if self.trigger not in _TRIGGERS or not isinstance(self.turn_id, str):
            raise InvalidArtifact("Codex PreCompact metadata is invalid")

    @property
    def native_metadata(self) -> dict[str, str]:
        return {"trigger": self.trigger, "turn_id": self.turn_id}

    def as_core_event(self) -> LifecycleEvent:
        """Drop adapter-local metadata before crossing the Core boundary."""
        return LifecycleEvent(
            version=self.version,
            name=self.name,
            adapter=self.adapter,
            session_id=self.session_id,
            workstream_id=self.workstream_id,
        )


class CodexAdapter(ClaudeAdapter):
    """Use the shared explicit-checkpoint path for normalized Codex events."""

    _adapter_id = "codex"
    _invalid_envelope_warning = _INVALID_ENVELOPE_WARNING
    _invalid_checkpoint_warning = _INVALID_CHECKPOINT_WARNING
    _unavailable_warning = _UNAVAILABLE_WARNING

    def handle_native(
        self, native_payload: object, local_binding: object
    ) -> AdapterResult:
        """Translate native input, then discard its metadata before observation."""
        try:
            translated = translate_pre_compact(native_payload, local_binding)
            if not isinstance(translated, CodexLifecycleEvent):
                raise InvalidArtifact("Codex translator returned an invalid event")
        except Exception:
            return self._failure("unknown", self._invalid_envelope_warning)
        core_event = translated.as_core_event()
        return self.handle(
            {
                "version": core_event.version,
                "event": core_event.name,
                "adapter": core_event.adapter,
                "session_id": core_event.session_id,
                "workstream_id": core_event.workstream_id,
            }
        )


def translate_pre_compact(
    native_payload: object, local_binding: object
) -> LifecycleEvent:
    """Validate one pinned native payload and bind it to a local workstream.

    ``transcript_path`` is treated only as a schema field.  It is never resolved
    or opened, and neither it nor other native host details enter the returned
    Core serialization.
    """
    payload = _native_payload(native_payload)
    binding = _local_binding(local_binding)
    return CodexLifecycleEvent(
        version=CONTRACT_VERSION,
        name="pre_compact",
        adapter="codex",
        session_id=payload["session_id"],
        workstream_id=binding["workstream_id"],
        trigger=payload["trigger"],
        turn_id=payload["turn_id"],
    )


def _native_payload(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise InvalidArtifact("Codex PreCompact payload must be a JSON object")
    fields = frozenset(value)
    if not _REQUIRED_NATIVE_FIELDS.issubset(fields) or not fields.issubset(
        _REQUIRED_NATIVE_FIELDS | _OPTIONAL_NATIVE_FIELDS
    ):
        raise InvalidArtifact("Codex PreCompact payload fields are invalid")
    for field in (
        "session_id",
        "cwd",
        "hook_event_name",
        "model",
        "turn_id",
        "trigger",
    ):
        if not isinstance(value[field], str):
            raise InvalidArtifact("Codex PreCompact payload field type is invalid")
    transcript_path = value["transcript_path"]
    if transcript_path is not None and not isinstance(transcript_path, str):
        raise InvalidArtifact("Codex PreCompact transcript path type is invalid")
    if value["hook_event_name"] != "PreCompact":
        raise InvalidArtifact("Codex hook event is invalid")
    if value["trigger"] not in _TRIGGERS:
        raise InvalidArtifact("Codex PreCompact trigger is invalid")
    return value


def _local_binding(value: object) -> Mapping[str, str]:
    if (
        not isinstance(value, Mapping)
        or frozenset(value) != _BINDING_FIELDS
        or not isinstance(value.get("workstream_id"), str)
    ):
        raise InvalidArtifact("Codex local binding is invalid")
    return value


__all__ = [
    "CodexAdapter",
    "CodexLifecycleEvent",
    "SCHEMA_ID",
    "translate_pre_compact",
]
