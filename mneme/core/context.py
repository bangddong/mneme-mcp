"""Read-only portable and effective-local CURRENT projections."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from mneme.core.registries import HeadRef, WorkstreamRegistry
from mneme.core.resolver import (
    OptionalInputUnavailable,
    ResolutionState,
    ResolvedWorkstream,
    resolve_workstream,
)


@dataclass(frozen=True, slots=True)
class LocalOverlay:
    """A locally authorized overlay whose portable relationship stays local."""

    overlay_of: str
    registry: WorkstreamRegistry
    optional_inputs: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextReaders:
    """The complete read-only dependency surface for generated CURRENT views."""

    load_portable_workstream: Callable[[str], WorkstreamRegistry]
    load_portable_revision: Callable[[str, HeadRef], object]
    load_overlay: Callable[[str], LocalOverlay | None] | None = None
    load_overlay_revision: Callable[[str, HeadRef], object] | None = None
    portable_optional_inputs: Callable[[str], Sequence[object]] | None = None

    def __post_init__(self) -> None:
        if not callable(self.load_portable_workstream) or not callable(self.load_portable_revision):
            raise TypeError("CURRENT requires portable read callables")
        if self.load_overlay is not None and not callable(self.load_overlay):
            raise TypeError("overlay reader must be callable")
        if self.load_overlay_revision is not None and not callable(self.load_overlay_revision):
            raise TypeError("overlay revision reader must be callable")
        if self.portable_optional_inputs is not None and not callable(self.portable_optional_inputs):
            raise TypeError("optional input reader must be callable")


@dataclass(frozen=True, slots=True)
class ContextView:
    """Generated text plus the separate conditions that produced it."""

    workstream_id: str
    mode: Literal["portable", "effective-local"]
    text: str
    portable: ResolvedWorkstream
    overlay: ResolvedWorkstream | None
    portable_status: ResolutionState
    overlay_status: ResolutionState | None
    effective_status: ResolutionState

    @property
    def status(self) -> ResolutionState:
        return self.effective_status


def render_current(
    readers: ContextReaders,
    workstream_id: str,
    mode: Literal["portable", "effective-local"],
) -> ContextView:
    """Return a generated projection; this function never persists a view.

    The portable branch returns before even reading an overlay callable.  This is
    intentional: the portable output must not reveal a local ID, path, hash,
    count, label, or existence.
    """

    if not isinstance(readers, ContextReaders):
        raise TypeError("render_current requires ContextReaders")
    if mode not in {"portable", "effective-local"}:
        raise ValueError("CURRENT mode must be portable or effective-local")

    portable = _resolve_portable(readers, workstream_id)
    portable_text = _render_layer("Current", portable)
    if mode == "portable":
        return ContextView(
            workstream_id, mode, portable_text, portable, None, portable.state, None, portable.state
        )

    overlay = _resolve_overlay(readers, workstream_id)
    effective = _effective_state(portable.state, overlay.state if overlay is not None else ResolutionState.DEGRADED)
    text = _render_effective(portable, overlay, effective)
    return ContextView(
        workstream_id,
        mode,
        text,
        portable,
        overlay,
        portable.state,
        overlay.state if overlay is not None else ResolutionState.DEGRADED,
        effective,
    )


def _resolve_portable(readers: ContextReaders, workstream_id: str) -> ResolvedWorkstream:
    try:
        registry = readers.load_portable_workstream(workstream_id)
        optional = tuple(readers.portable_optional_inputs(workstream_id)) if readers.portable_optional_inputs else ()
        return resolve_workstream(
            registry,
            lambda head: readers.load_portable_revision(workstream_id, head),
            optional_inputs=optional,
        )
    except Exception:
        return _invalid_projection()


def _resolve_overlay(readers: ContextReaders, workstream_id: str) -> ResolvedWorkstream | None:
    if readers.load_overlay is None:
        return None
    try:
        overlay = readers.load_overlay(workstream_id)
    except Exception:
        return _invalid_projection()
    if overlay is None:
        return None
    if not isinstance(overlay, LocalOverlay) or overlay.overlay_of != workstream_id:
        return _invalid_projection()
    if readers.load_overlay_revision is None:
        return _invalid_projection()
    return resolve_workstream(
        overlay.registry,
        lambda head: readers.load_overlay_revision(workstream_id, head),
        optional_inputs=overlay.optional_inputs,
    )


def _effective_state(portable: ResolutionState, overlay: ResolutionState) -> ResolutionState:
    if portable is ResolutionState.INVALID:
        return ResolutionState.INVALID
    if overlay in {ResolutionState.INVALID, ResolutionState.DEGRADED}:
        return ResolutionState.DEGRADED
    if portable is ResolutionState.DEGRADED:
        return ResolutionState.DEGRADED
    if portable is ResolutionState.DIVERGENT or overlay is ResolutionState.DIVERGENT:
        return ResolutionState.DIVERGENT
    return ResolutionState.RESOLVED


def _invalid_projection() -> ResolvedWorkstream:
    return ResolvedWorkstream(ResolutionState.INVALID, None, (), (), None, ("unreadable",))


def _render_effective(
    portable: ResolvedWorkstream,
    overlay: ResolvedWorkstream | None,
    effective: ResolutionState,
) -> str:
    lines = [
        "# CURRENT",
        "",
        f"portable_status: {portable.state.value}",
        f"overlay_status: {(overlay.state if overlay is not None else ResolutionState.DEGRADED).value}",
        f"effective_status: {effective.value}",
        "",
        _render_layer("Portable base", portable),
    ]
    if overlay is not None and overlay.state is not ResolutionState.INVALID:
        lines.extend(("", _render_layer("Local foreground", overlay)))
    return "\n".join(lines) + "\n"


def _render_layer(label: str, resolved: ResolvedWorkstream) -> str:
    lines = [f"# {label}", "", f"status: {resolved.state.value}"]
    if resolved.state is ResolutionState.INVALID:
        lines.append("The selected canonical state requires repair.")
        return "\n".join(lines)
    if not resolved.revisions:
        lines.append("No active heads.")
        return "\n".join(lines)
    for head, revision in resolved.revisions:
        role = "selected" if head == resolved.selected_head else "alternative"
        lines.extend(("", f"## {role} head", _revision_text(revision)))
    return "\n".join(lines)


def _revision_text(revision: object) -> str:
    if isinstance(revision, str):
        return revision
    text = getattr(revision, "text", None)
    if isinstance(text, str):
        return text
    body = getattr(revision, "body", None)
    if body is not None:
        objective = getattr(body, "objective", None)
        current_state = getattr(body, "current_state", None)
        if isinstance(objective, str) and isinstance(current_state, str):
            return f"Objective: {objective}\n\nCurrent state: {current_state}"
    return "Revision content is available."
