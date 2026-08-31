from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Revision:
    text: str


def _portable_registry():
    from mneme.core.registries import HeadRef, WorkstreamRegistry

    return WorkstreamRegistry(
        "ws-1", 0, None, "active", "single", (HeadRef("portable", "000001"),)
    )


def _local_registry():
    from mneme.core.registries import HeadRef, WorkstreamRegistry

    local = WorkstreamRegistry(
        "local-ws", 0, None, "active", "preferred",
        (HeadRef("local-secret", "000001"),), HeadRef("local-secret", "000001")
    )
    return local


def test_portable_mode_never_opens_or_mentions_local_overlay_data():
    """Catches portable CURRENT probing, or disclosing facts about, local-only state."""
    from mneme.core.context import ContextReaders, render_current

    def forbidden_overlay(_workstream_id):
        raise AssertionError("portable rendering must not read an overlay")

    readers = ContextReaders(
        load_portable_workstream=lambda _id: _portable_registry(),
        load_portable_revision=lambda _id, _head: Revision("portable continuity"),
        load_overlay=forbidden_overlay,
    )

    view = render_current(readers, "ws-1", "portable")

    assert view.status.value == "resolved"
    assert "portable continuity" in view.text
    for forbidden in ("local-secret", "confidential", "overlay", "hash", "count", "exists"):
        assert forbidden not in view.text.lower()


def test_effective_local_labels_base_and_foreground_without_rewriting_portable_selection():
    """Catches a local preferred head replacing portable canonical state."""
    from mneme.core.context import ContextReaders, LocalOverlay, render_current

    portable = _portable_registry()
    readers = ContextReaders(
        load_portable_workstream=lambda _id: portable,
        load_portable_revision=lambda _id, _head: Revision("portable base continuity"),
        load_overlay=lambda _id: LocalOverlay("ws-1", _local_registry()),
        load_overlay_revision=lambda _id, _head: Revision("confidential foreground"),
    )

    view = render_current(readers, "ws-1", "effective-local")

    assert view.portable_status.value == "resolved"
    assert view.overlay_status.value == "resolved"
    assert view.effective_status.value == "resolved"
    assert "Portable base" in view.text
    assert "Local foreground" in view.text
    assert "portable base continuity" in view.text
    assert "confidential foreground" in view.text
    assert view.portable.selected_head == portable.active_heads[0]
    assert view.overlay.selected_head.session == "local-secret"


def test_invalid_optional_overlay_falls_back_to_portable_and_degrades():
    """Catches invalid local state leaking into an otherwise usable portable view."""
    from mneme.core.context import ContextReaders, LocalOverlay, render_current

    readers = ContextReaders(
        load_portable_workstream=lambda _id: _portable_registry(),
        load_portable_revision=lambda _id, _head: Revision("portable continuity"),
        load_overlay=lambda _id: LocalOverlay("wrong-base", _local_registry()),
        load_overlay_revision=lambda _id, _head: Revision("must not render"),
    )

    view = render_current(readers, "ws-1", "effective-local")

    assert view.portable_status.value == "resolved"
    assert view.overlay_status.value == "invalid"
    assert view.effective_status.value == "degraded"
    assert "portable continuity" in view.text
    assert "must not render" not in view.text


def test_invalid_portable_base_remains_invalid_even_with_a_valid_local_overlay():
    """Catches a local layer repairing a missing portable canonical revision."""
    from mneme.core.context import ContextReaders, LocalOverlay, render_current

    readers = ContextReaders(
        load_portable_workstream=lambda _id: _portable_registry(),
        load_portable_revision=lambda _id, _head: None,
        load_overlay=lambda _id: LocalOverlay("ws-1", _local_registry()),
        load_overlay_revision=lambda _id, _head: Revision("confidential foreground"),
    )

    view = render_current(readers, "ws-1", "effective-local")

    assert view.portable_status.value == "invalid"
    assert view.effective_status.value == "invalid"


def test_absent_optional_overlay_degrades_without_mutating_portable_registry():
    """Catches effective rendering persisting local preference into portable state."""
    from mneme.core.context import ContextReaders, render_current

    portable = _portable_registry()
    readers = ContextReaders(
        load_portable_workstream=lambda _id: portable,
        load_portable_revision=lambda _id, _head: Revision("portable continuity"),
        load_overlay=lambda _id: None,
    )

    view = render_current(readers, "ws-1", "effective-local")

    assert view.overlay_status.value == "degraded"
    assert view.effective_status.value == "degraded"
    assert portable.preferred_head is None
