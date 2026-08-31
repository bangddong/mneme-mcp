"""Deterministic read-only resolution of explicit workstream heads."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mneme.core.errors import InvalidArtifact
from mneme.core.registries import HeadRef, WorkstreamRegistry


class ResolutionState(str, Enum):
    """The non-canonical condition of one resolved workstream layer."""

    RESOLVED = "resolved"
    DIVERGENT = "divergent"
    DEGRADED = "degraded"
    INVALID = "invalid"


class OptionalInputUnavailable(Exception):
    """An optional mount, index, or equivalent read input is unavailable."""


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
) -> ResolvedWorkstream:
    """Resolve only heads explicitly declared in ``registry``.

    ``load_revision`` is deliberately a read boundary.  In production it should
    be ``SessionStore.read_revision`` (or an equivalent validating reader), so a
    forged revision fails before it enters a projection.  This resolver never
    searches a session directory, reads a timestamp, or mutates canonical state.
    """

    if not callable(load_revision):
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
        if revision is None:
            return _invalid(registry, heads, "active revision is missing")
        loaded.append((head, revision))

    state = (
        ResolutionState.DIVERGENT
        if registry.mode == "parallel" and len(heads) > 1
        else ResolutionState.RESOLVED
    )
    if any(_optional_unavailable(item) for item in optional_inputs):
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
    return isinstance(value, OptionalInputUnavailable) or value is False


def _invalid(
    registry: WorkstreamRegistry | None, heads: tuple[HeadRef, ...], diagnostic: str
) -> ResolvedWorkstream:
    return ResolvedWorkstream(ResolutionState.INVALID, registry, heads, (), None, (diagnostic,))
