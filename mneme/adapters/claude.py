"""Non-blocking Claude lifecycle adapter.

This module deliberately knows only the normalized adapter envelope.  It does
not import Claude libraries, inspect a transcript, or construct checkpoint
semantics; a host-selected JSON checkpoint is passed as one explicit Core
command after location and shape checks.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path, PureWindowsPath
import stat
import tempfile
from typing import Any

from mneme.adapters.base import AdapterEnvelope, AdapterResult
from mneme.core.contracts import CONTRACT_VERSION, CommandResult, CoreCommand, ObservationResult
from mneme.core.fs import is_symlink_or_reparse, validate_path_chain
from mneme.core.errors import InvalidArtifact


_CHECKPOINT_FIELDS = frozenset(
    {
        "workstream_id",
        "session_id",
        "storage_class",
        "expected_parent",
        "expected_registry_generation",
        "body",
        "relations",
    }
)
_CHECKPOINT_WRAPPER_FIELDS = frozenset({"command", "payload"})
_MAX_CHECKPOINT_BYTES = 256 * 1024
_INVALID_ENVELOPE_WARNING = (
    "Madi could not use this Claude lifecycle event; continue ordinary Claude work."
)
_INVALID_CHECKPOINT_WARNING = (
    "Madi could not use the selected checkpoint; continue ordinary Claude work."
)
_UNAVAILABLE_WARNING = "Madi is unavailable; continue ordinary Claude work."


class ClaudeAdapter:
    """Translate Claude lifecycle input into Task 20 contracts without blocking Claude."""

    def __init__(
        self,
        service: object,
        *,
        project_root: Path | str | None = None,
        temp_root: Path | str | None = None,
    ) -> None:
        self._service = service
        self._project_root = project_root
        self._temp_root = temp_root

    def handle(self, envelope: object) -> AdapterResult:
        """Observe one lifecycle boundary and optionally persist an explicit checkpoint."""
        try:
            normalized = AdapterEnvelope.from_mapping(envelope)
            if normalized.adapter != "claude":
                raise InvalidArtifact("adapter identity is invalid")
        except Exception:
            return self._failure("unknown", _INVALID_ENVELOPE_WARNING)

        try:
            observation = self._service.observe(normalized.as_lifecycle_event())
            if not isinstance(observation, ObservationResult):
                raise TypeError("adapter service returned an invalid observation")
        except Exception:
            return self._failure(normalized.event, _UNAVAILABLE_WARNING)

        if normalized.checkpoint_file is None:
            return AdapterResult(
                CONTRACT_VERSION,
                normalized.event,
                True,
                False,
                observation.checkpoint_required,
                observation.context_required,
            )

        try:
            payload = self._checkpoint_payload(normalized)
            command = CoreCommand(CONTRACT_VERSION, "create_session_revision", payload)
        except Exception:
            return self._failure(
                normalized.event,
                _INVALID_CHECKPOINT_WARNING,
                checkpoint_required=observation.checkpoint_required,
                context_required=observation.context_required,
            )

        try:
            command_result = self._service.execute(command)
            if not isinstance(command_result, CommandResult) or not command_result.ok:
                raise TypeError("adapter service did not complete the checkpoint")
        except Exception:
            return self._failure(
                normalized.event,
                _UNAVAILABLE_WARNING,
                checkpoint_required=observation.checkpoint_required,
                context_required=observation.context_required,
            )

        return AdapterResult(
            CONTRACT_VERSION,
            normalized.event,
            True,
            False,
            observation.checkpoint_required,
            observation.context_required,
            command_result=command_result,
        )

    def _checkpoint_payload(self, envelope: AdapterEnvelope) -> dict[str, Any]:
        """Read a bounded host-composed JSON object without forwarding its path."""
        checkpoint = self._safe_checkpoint_path(envelope.checkpoint_file)
        try:
            if checkpoint.suffix.lower() != ".json" or is_symlink_or_reparse(checkpoint):
                raise InvalidArtifact("checkpoint is not a safe JSON file")
            metadata = checkpoint.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_CHECKPOINT_BYTES:
                raise InvalidArtifact("checkpoint is not a bounded regular file")
            # Repeat the lexical chain check immediately before opening the host file.
            validate_path_chain(checkpoint, allow_missing=False)
            with checkpoint.open("rb") as stream:
                raw = stream.read(_MAX_CHECKPOINT_BYTES + 1)
        except (OSError, ValueError) as exc:
            raise InvalidArtifact("checkpoint cannot be read safely") from exc
        if len(raw) > _MAX_CHECKPOINT_BYTES:
            raise InvalidArtifact("checkpoint is too large")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise InvalidArtifact("checkpoint is not valid UTF-8 JSON") from exc
        payload = _unwrap_checkpoint(value)
        if frozenset(payload) != _CHECKPOINT_FIELDS:
            raise InvalidArtifact("checkpoint command fields are invalid")
        if (
            payload["session_id"] != envelope.session_id
            or payload["workstream_id"] != envelope.workstream_id
        ):
            raise InvalidArtifact("checkpoint does not belong to lifecycle envelope")
        body = payload["body"]
        if not isinstance(body, Mapping) or body.get("adapter_id") != "claude":
            raise InvalidArtifact("checkpoint adapter identity is invalid")
        return dict(payload)

    def _safe_checkpoint_path(self, value: str | None) -> Path:
        if not isinstance(value, str) or "\x00" in value:
            raise InvalidArtifact("checkpoint path is invalid")
        requested = Path(value)
        windows_requested = PureWindowsPath(value)
        project_root = self._configured_root(self._project_root, required=False)
        temp_root = self._configured_root(
            tempfile.gettempdir() if self._temp_root is None else self._temp_root,
            required=True,
        )
        if requested.is_absolute() or windows_requested.is_absolute() or windows_requested.drive:
            candidate = requested
        elif project_root is not None:
            candidate = project_root / requested
        else:
            raise InvalidArtifact("relative checkpoint path has no project root")
        candidate = validate_path_chain(candidate, allow_missing=False)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise InvalidArtifact("checkpoint path cannot be resolved") from exc
        allowed = tuple(root for root in (project_root, temp_root) if root is not None)
        if not any(resolved.is_relative_to(root) for root in allowed):
            raise InvalidArtifact("checkpoint path is outside approved host storage")
        return candidate

    @staticmethod
    def _configured_root(value: Path | str | None, *, required: bool) -> Path | None:
        if value is None:
            if required:
                raise InvalidArtifact("adapter storage root is missing")
            return None
        root = validate_path_chain(Path(value), allow_missing=False)
        if not root.is_dir() or is_symlink_or_reparse(root):
            raise InvalidArtifact("adapter storage root is unsafe")
        try:
            return root.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise InvalidArtifact("adapter storage root cannot be resolved") from exc

    @staticmethod
    def _failure(
        event: str,
        warning: str,
        *,
        checkpoint_required: bool = False,
        context_required: bool = False,
    ) -> AdapterResult:
        return AdapterResult(
            CONTRACT_VERSION,
            event,
            False,
            False,
            checkpoint_required,
            context_required,
            warning,
        )


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object field")
        value[key] = item
    return value


def _unwrap_checkpoint(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidArtifact("checkpoint must be a JSON object")
    if frozenset(value) == _CHECKPOINT_WRAPPER_FIELDS:
        if value["command"] != "create_session_revision":
            raise InvalidArtifact("checkpoint command is invalid")
        value = value["payload"]
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise InvalidArtifact("checkpoint payload must be a JSON object")
    return value


__all__ = ["ClaudeAdapter"]
