"""Deterministic read-only resolution of explicit workstream heads."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mneme.core.artifacts import StorageClass
from mneme.core.errors import InvalidArtifact
from mneme.core.registries import HeadRef, WorkstreamRegistry
from mneme.core.sessions import CheckpointRequest, SessionBody, SessionRevisionRef, validate_historic_session_revision


class ResolutionState(str, Enum):
    """The non-canonical condition of one resolved workstream layer."""

    RESOLVED = "resolved"
    DIVERGENT = "divergent"
    DEGRADED = "degraded"
    INVALID = "invalid"


class OptionalInputUnavailable(Exception):
    """An optional mount, index, or equivalent read input is unavailable."""


@dataclass(frozen=True, slots=True)
class SessionRevision:
    """A resolver-safe result from a validating Session reader.

    The reader boundary is responsible for decoding and validating immutable
    storage.  This value repeats the requested identity at the projection
    boundary so a result from another head, workstream, or storage class cannot
    be substituted into CURRENT.
    """

    head: HeadRef
    request: CheckpointRequest

    def __post_init__(self) -> None:
        if not isinstance(self.head, HeadRef):
            raise InvalidArtifact("resolved revision requires a HeadRef")
        canonical_head = HeadRef(self.head.session, self.head.revision)
        canonical_request = validate_historic_session_revision(
            self.request,
            SessionRevisionRef(canonical_head.session, canonical_head.revision),
            workstream_id=self.request.workstream_id,
            storage_class=self.request.storage_class,
        )
        object.__setattr__(self, "head", canonical_head)
        object.__setattr__(self, "request", canonical_request)

    @property
    def workstream_id(self) -> str:
        return self.request.workstream_id

    @property
    def storage_class(self) -> StorageClass:
        return self.request.storage_class

    @property
    def body(self) -> SessionBody:
        return self.request.body


@dataclass(frozen=True, slots=True)
class ResolvedWorkstream:
    """A projection of registry-declared heads and already-validated revisions."""

    state: ResolutionState
    registry: WorkstreamRegistry | None
    heads: tuple[HeadRef, ...]
    revisions: tuple[tuple[HeadRef, object], ...]
    selected_head: HeadRef | None
    diagnostics: tuple[str, ...] = ()

    @property
    def selected_revision(self) -> object | None:
        if self.selected_head is None:
            return None
        for head, revision in self.revisions:
            if head == self.selected_head:
                return revision
        return None


def resolve_workstream(
    registry: object,
    load_revision: Callable[[HeadRef], object],
    *,
    optional_inputs: Sequence[object] = (),
    expected_storage_class: StorageClass = StorageClass.PORTABLE,
) -> ResolvedWorkstream:
    """Resolve only heads explicitly declared in ``registry``.

    ``load_revision`` is deliberately a read boundary.  In production it should
    be ``SessionStore.read_revision`` (or an equivalent validating reader), so a
    forged revision fails before it enters a projection.  This resolver never
    searches a session directory, reads a timestamp, or mutates canonical state.
    """

    if not callable(load_revision) or not isinstance(expected_storage_class, StorageClass):
        return _invalid(None, (), "revision reader is invalid")
    validation = _validate_registry(registry)
    if validation is not None:
        return _invalid(registry if isinstance(registry, WorkstreamRegistry) else None, (), validation)
    assert isinstance(registry, WorkstreamRegistry)
    heads = tuple(sorted(registry.active_heads, key=lambda item: (item.session, item.revision)))

    # Paused and closed workstreams deliberately have a resolved empty view.
    if not heads:
        return ResolvedWorkstream(ResolutionState.RESOLVED, registry, (), (), None)

    loaded: list[tuple[HeadRef, object]] = []
    for head in heads:
        try:
            revision = load_revision(head)
        except Exception as exc:  # Reader validation includes malformed/tampered revisions.
            return _invalid(registry, heads, f"active revision is unreadable: {type(exc).__name__}")
        canonical_revision = _canonical_revision(
            revision, head, registry.id, expected_storage_class
        )
        if canonical_revision is None:
            return _invalid(registry, heads, "active revision is invalid")
        loaded.append((head, canonical_revision))

    state = (
        ResolutionState.DIVERGENT
        if registry.mode == "parallel" and len(heads) > 1
        else ResolutionState.RESOLVED
    )
    if _optional_inputs_degraded(optional_inputs):
        state = ResolutionState.DEGRADED
    selected = registry.preferred_head if registry.mode == "preferred" else (
        heads[0] if len(heads) == 1 else None
    )
    return ResolvedWorkstream(state, registry, heads, tuple(loaded), selected)


def _validate_registry(registry: object) -> str | None:
    """Re-check decoded data so manually tampered registry objects fail closed."""

    if not isinstance(registry, WorkstreamRegistry):
        return "workstream registry is invalid"
    try:
        WorkstreamRegistry(
            registry.id,
            registry.generation,
            registry.project,
            registry.status,
            registry.mode,
            registry.active_heads,
            registry.preferred_head,
            registry.policy_refs,
        )
    except (InvalidArtifact, TypeError):
        return "workstream registry is invalid"
    if registry.status not in {"active", "paused", "closed"}:
        return "workstream status is invalid"
    if registry.mode not in {"single", "preferred", "parallel"}:
        return "workstream mode is invalid"
    if not isinstance(registry.active_heads, tuple) or not all(
        isinstance(head, HeadRef) for head in registry.active_heads
    ):
        return "workstream heads are invalid"
    if len(set(registry.active_heads)) != len(registry.active_heads) or len(
        {head.session for head in registry.active_heads}
    ) != len(registry.active_heads):
        return "workstream heads are contradictory"
    heads = registry.active_heads
    if not heads:
        if registry.status in {"paused", "closed"} and registry.preferred_head is None:
            return None
        return "active workstream has no heads"
    if registry.mode == "single" and len(heads) != 1:
        return "single workstream has an invalid head count"
    if registry.mode == "preferred":
        if registry.preferred_head not in heads:
            return "preferred head is not active"
    elif registry.preferred_head is not None:
        return "non-preferred workstream declares a preferred head"
    return None


def _optional_unavailable(value: object) -> bool:
    return isinstance(value, OptionalInputUnavailable) or value is not True


def _optional_inputs_degraded(optional_inputs: object) -> bool:
    try:
        if isinstance(optional_inputs, (str, bytes)) or not isinstance(optional_inputs, Sequence):
            return True
        return any(_optional_unavailable(item) for item in optional_inputs)
    except Exception:
        return True


def _canonical_revision(
    revision: object,
    head: HeadRef,
    workstream_id: str,
    storage_class: StorageClass,
) -> SessionRevision | None:
    if not isinstance(revision, SessionRevision):
        return None
    try:
        canonical = SessionRevision(revision.head, revision.request)
    except Exception:
        return None
    if (
        canonical.head != head
        or canonical.workstream_id != workstream_id
        or canonical.storage_class is not storage_class
    ):
        return None
    return canonical


def _invalid(
    registry: WorkstreamRegistry | None, heads: tuple[HeadRef, ...], diagnostic: str
) -> ResolvedWorkstream:
    return ResolvedWorkstream(ResolutionState.INVALID, registry, heads, (), None, (diagnostic,))
