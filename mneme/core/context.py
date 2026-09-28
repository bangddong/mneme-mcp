"""Read-only portable and effective-local CURRENT projections."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal
import json

from mneme.core.registries import HeadRef, WorkstreamRegistry
from mneme.core.resolver import (
    OptionalInputUnavailable,
    ResolutionState,
    ResolvedWorkstream,
    SessionRevision,
    resolve_workstream,
)
from mneme.core.artifacts import StorageClass


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
    authorize_revision: Callable[[str, HeadRef, StorageClass], bool] | None = None
    load_accepted_memories: Callable[[str, StorageClass], Sequence[object]] | None = None

    def __post_init__(self) -> None:
        if not callable(self.load_portable_workstream) or not callable(self.load_portable_revision):
            raise TypeError("CURRENT requires portable read callables")
        if self.load_overlay is not None and not callable(self.load_overlay):
            raise TypeError("overlay reader must be callable")
        if self.load_overlay_revision is not None and not callable(self.load_overlay_revision):
            raise TypeError("overlay revision reader must be callable")
        if self.portable_optional_inputs is not None and not callable(self.portable_optional_inputs):
            raise TypeError("optional input reader must be callable")
        if self.authorize_revision is not None and not callable(self.authorize_revision):
            raise TypeError("CURRENT revision authorizer must be callable")


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
    if portable.state is not ResolutionState.INVALID:
        portable_text += _render_memories(readers, workstream_id, StorageClass.PORTABLE)
    if mode == "portable":
        return ContextView(
            workstream_id, mode, portable_text, portable, None, portable.state, None, portable.state
        )

    overlay = _resolve_overlay(readers, workstream_id)
    effective = _effective_state(portable.state, overlay.state if overlay is not None else ResolutionState.DEGRADED)
    text = _render_effective(portable, overlay, effective)
    if portable.state is not ResolutionState.INVALID:
        text += _render_memories(readers, workstream_id, StorageClass.PORTABLE)
        if overlay is not None and overlay.registry is not None and overlay.state is not ResolutionState.INVALID:
            text += _render_memories(readers, overlay.registry.id, StorageClass.LOCAL_ONLY)
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
    except Exception:
        return _invalid_projection()
    optional = _read_optional_inputs(readers, workstream_id)
    try:
        return resolve_workstream(
            registry,
            lambda head: _load_authorized_revision(
                readers, workstream_id, head, StorageClass.PORTABLE
            ),
            optional_inputs=optional,
            expected_storage_class=StorageClass.PORTABLE,
        )
    except Exception:
        return _invalid_projection()


def _load_authorized_revision(
    readers: ContextReaders,
    workstream_id: str,
    head: HeadRef,
    storage_class: StorageClass,
) -> object:
    if readers.authorize_revision is None:
        raise PermissionError("current policy authorization is unavailable")
    if readers.authorize_revision(workstream_id, head, storage_class) is not True:
        raise PermissionError("current policy withheld revision")
    if storage_class is StorageClass.PORTABLE:
        return readers.load_portable_revision(workstream_id, head)
    if readers.load_overlay_revision is None:
        raise PermissionError("current policy authorization is unavailable")
    return readers.load_overlay_revision(workstream_id, head)


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
    if not _optional_inputs_safe(overlay.optional_inputs):
        return _degraded_projection()
    try:
        return resolve_workstream(
            overlay.registry,
            lambda head: _load_authorized_revision(
                readers,
                overlay.registry.id,
                head,
                StorageClass.LOCAL_ONLY,
            ),
            optional_inputs=overlay.optional_inputs,
            expected_storage_class=StorageClass.LOCAL_ONLY,
        )
    except Exception:
        return _invalid_projection()


def _read_optional_inputs(readers: ContextReaders, workstream_id: str) -> Sequence[object]:
    if readers.portable_optional_inputs is None:
        return ()
    try:
        optional = readers.portable_optional_inputs(workstream_id)
    except Exception:
        return (OptionalInputUnavailable("optional input unavailable"),)
    if isinstance(optional, (str, bytes)) or not isinstance(optional, Sequence):
        return (OptionalInputUnavailable("optional input is malformed"),)
    return optional


def _optional_inputs_safe(value: object) -> bool:
    try:
        return (
            not isinstance(value, (str, bytes))
            and isinstance(value, Sequence)
            and all(item is True for item in value)
        )
    except Exception:
        return False


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


def _degraded_projection() -> ResolvedWorkstream:
    return ResolvedWorkstream(ResolutionState.DEGRADED, None, (), (), None, ("optional input unavailable",))


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
    if overlay is not None and overlay.state in {ResolutionState.RESOLVED, ResolutionState.DIVERGENT}:
        lines.extend(("", _render_layer("Local foreground", overlay)))
    return "\n".join(lines) + "\n"


def _render_layer(label: str, resolved: ResolvedWorkstream) -> str:
    lines = [f"# {label}", "", f"status: {resolved.state.value}"]
    if resolved.state is ResolutionState.INVALID:
        lines.append("The selected canonical state requires repair.")
        return "\n".join(lines)
    if resolved.registry is not None:
        lines.append(f"workstream: {resolved.registry.id}; project: {resolved.registry.project or 'none'}")
    if resolved.diagnostics:
        lines.extend(f"diagnostic: {_bounded(item)}" for item in resolved.diagnostics[:8])
    if not resolved.revisions:
        lines.append("No active heads.")
        return "\n".join(lines)
    ordered = sorted(resolved.revisions, key=lambda pair: (pair[0] != resolved.selected_head, pair[0].session, pair[0].revision))
    for head, revision in ordered[:4]:
        role = "selected" if head == resolved.selected_head else "alternative"
        lines.extend(("", f"## {role} head: {head.session}@{head.revision}", _revision_text(revision)))
    if len(ordered) > 4:
        lines.append("Additional declared heads omitted from bounded CURRENT; inspect the registry.")
    return "\n".join(lines)


def _revision_text(revision: object) -> str:
    if not isinstance(revision, SessionRevision):
        return "Validated revision content is unavailable."
    body = revision.body
    request = revision.request
    lines = [f"session: {request.session_id}; workstream: {request.workstream_id}; revision: {revision.head.revision}",
        f"adapter: {_bounded(body.adapter_id)}; timestamp: {request.revision_timestamp.isoformat()}",
        f"predecessor: {request.expected_parent}",
        f"Objective: {_bounded(body.objective)}", f"Current state: {_bounded(body.current_state)}"]
    for label, values in (("Verified facts / decisions", body.verified_facts), ("Completed work / verification", body.completed_work),
                          ("Blockers / risks", body.blockers), ("Next actions", body.next_actions)):
        lines.append(f"### {label}")
        lines.extend(f"- {_bounded(value)}" for value in values[:4])
        if not values:
            lines.append("- none")
        elif len(values) > 4:
            lines.append("- additional items available in the revision")
    from mneme.core.sessions import _serialize_reference, _serialize_relation
    lines.append("### Source references")
    lines.extend(_bounded(json.dumps(_serialize_reference(item), sort_keys=True)) for item in body.source_refs[:4])
    lines.append("### Relations / handoffs")
    for relation in request.relations[:4]:
        # Bound individual fields so next actions remain visible after long context.
        for key, value in _serialize_relation(relation).items():
            lines.append(f"{key}: {_bounded(str(value))}")
    lines.append(_receipt_text(request.policy_evaluation))
    return "\n".join(lines)


def _bounded(value: str) -> str:
    return value if len(value) <= 256 else value[:256] + " … [bounded; inspect artifact]"


def _receipt_text(receipt: object) -> str:
    from mneme.core.memories import _profile_receipt

    return "provenance / policy_receipt: " + _bounded(_profile_receipt(receipt))


def _render_memories(readers: ContextReaders, workstream_id: str, storage_class: StorageClass) -> str:
    if readers.load_accepted_memories is None:
        return ""
    try:
        records = readers.load_accepted_memories(workstream_id, storage_class)
        lines = ["", f"## Accepted memories ({storage_class.value})"]
        from mneme.core.memories import _profile_provenance
        for record in records[:5]:
            lines.extend((f"### {record.id} ({record.kind.value}; {record.authority.value})", _bounded(record.body),
                "provenance: " + _bounded(_profile_provenance(record.provenance)), _receipt_text(record.policy_receipt)))
        return "\n".join(lines) + "\n"
    except Exception:
        return "\nAccepted memories unavailable.\n"
