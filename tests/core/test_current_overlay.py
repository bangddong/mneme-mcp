from __future__ import annotations

def _revision(head, *, workstream_id="ws-1", storage_class=None, objective="portable continuity"):
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyEvaluation, Portability
    from mneme.core.resolver import SessionRevision
    from mneme.core.sessions import CheckpointRequest, SessionBody, session_semantic_hash
    from datetime import datetime, timezone

    storage = storage_class or StorageClass.PORTABLE
    body = SessionBody("codex", objective, "working", (), (), (), ("continue",), ())
    receipt = PolicyEvaluation(
        Portability.LOCAL_ONLY if storage is StorageClass.LOCAL_ONLY else Portability.PERSONAL_VAULT,
        Portability.LOCAL_ONLY if storage is StorageClass.LOCAL_ONLY else Portability.PERSONAL_VAULT,
        True, datetime(2026, 1, 1, tzinfo=timezone.utc), "test", (), session_semantic_hash(body, ()),
    )
    return SessionRevision(
        head,
        CheckpointRequest(workstream_id, head.session, storage, None, 0, body, (), receipt, datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )


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
        load_portable_revision=lambda _id, head: _revision(head),
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
        load_portable_revision=lambda _id, head: _revision(head, objective="portable base continuity"),
        load_overlay=lambda _id: LocalOverlay("ws-1", _local_registry()),
        load_overlay_revision=lambda _id, head: _revision(head, workstream_id="local-ws", storage_class=__import__("mneme.core.artifacts", fromlist=["StorageClass"]).StorageClass.LOCAL_ONLY, objective="confidential foreground"),
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
        load_portable_revision=lambda _id, head: _revision(head),
        load_overlay=lambda _id: LocalOverlay("wrong-base", _local_registry()),
        load_overlay_revision=lambda _id, head: _revision(head, storage_class=__import__("mneme.core.artifacts", fromlist=["StorageClass"]).StorageClass.LOCAL_ONLY, objective="must not render"),
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
        load_overlay_revision=lambda _id, head: _revision(head, storage_class=__import__("mneme.core.artifacts", fromlist=["StorageClass"]).StorageClass.LOCAL_ONLY, objective="confidential foreground"),
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
        load_portable_revision=lambda _id, head: _revision(head),
        load_overlay=lambda _id: None,
    )

    view = render_current(readers, "ws-1", "effective-local")

    assert view.overlay_status.value == "degraded"
    assert view.effective_status.value == "degraded"
    assert portable.preferred_head is None


def test_portable_reader_rejects_arbitrary_text_payload_without_rendering_it():
    """Catches portable CURRENT rendering unvalidated duck-typed local payloads."""
    from mneme.core.context import ContextReaders, render_current

    class Payload:
        text = "local-id local-path local-hash local-count"

    view = render_current(
        ContextReaders(lambda _id: _portable_registry(), lambda _id, _head: Payload()),
        "ws-1", "portable",
    )

    assert view.status.value == "invalid"
    assert "local-id" not in view.text


def test_optional_reader_failures_and_malformed_inputs_degrade_a_valid_portable_view():
    """Catches optional source failures turning a safe base into canonical invalidity."""
    from mneme.core.context import ContextReaders, render_current

    readers = ContextReaders(
        load_portable_workstream=lambda _id: _portable_registry(),
        load_portable_revision=lambda _id, head: _revision(head),
        portable_optional_inputs=lambda _id: (_ for _ in ()).throw(FileNotFoundError("mount unavailable")),
    )
    assert render_current(readers, "ws-1", "portable").status.value == "degraded"

    malformed = ContextReaders(
        load_portable_workstream=lambda _id: _portable_registry(),
        load_portable_revision=lambda _id, head: _revision(head),
        portable_optional_inputs=lambda _id: object(),
    )
    assert render_current(malformed, "ws-1", "portable").status.value == "degraded"


def test_optional_overlay_reader_exception_falls_back_to_portable():
    """Catches a missing optional overlay preventing the portable base from rendering."""
    from mneme.core.context import ContextReaders, render_current

    view = render_current(
        ContextReaders(
            load_portable_workstream=lambda _id: _portable_registry(),
            load_portable_revision=lambda _id, head: _revision(head),
            load_overlay=lambda _id: (_ for _ in ()).throw(FileNotFoundError("overlay missing")),
        ),
        "ws-1",
        "effective-local",
    )

    assert view.portable_status.value == "resolved"
    assert view.overlay_status.value == "invalid"
    assert view.effective_status.value == "degraded"
    assert "portable continuity" in view.text


from collections.abc import Sequence
import pytest


class _ExplodingGetItemInputs(Sequence[object]):
    def __len__(self):
        return 1

    def __getitem__(self, _index):
        raise RuntimeError("optional reader failed")


class _ExplodingIterInputs(Sequence[object]):
    def __len__(self):
        return 1

    def __getitem__(self, _index):
        return True

    def __iter__(self):
        raise RuntimeError("optional reader failed")


class _ExplodingLenInputs(Sequence[object]):
    def __len__(self):
        raise RuntimeError("optional reader failed")

    def __getitem__(self, _index):
        return True

    def __iter__(self):
        return iter(range(len(self)))


@pytest.mark.parametrize("sequence_type", [_ExplodingGetItemInputs, _ExplodingIterInputs, _ExplodingLenInputs])
def test_exploding_portable_optional_sequence_degrades_without_crashing(sequence_type):
    """Catches an optional Sequence inspection exception escaping CURRENT rendering."""
    from mneme.core.context import ContextReaders, render_current

    view = render_current(
        ContextReaders(
            lambda _id: _portable_registry(),
            lambda _id, head: _revision(head),
            portable_optional_inputs=lambda _id: sequence_type(),
        ),
        "ws-1", "portable",
    )

    assert view.status.value == "degraded"


@pytest.mark.parametrize("sequence_type", [_ExplodingGetItemInputs, _ExplodingIterInputs, _ExplodingLenInputs])
def test_exploding_overlay_optional_sequence_degrades_without_partial_overlay_render(sequence_type):
    """Catches a local optional Sequence exception crashing effective-local CURRENT."""
    from mneme.core.context import ContextReaders, LocalOverlay, render_current

    overlay = LocalOverlay("ws-1", _local_registry())
    object.__setattr__(overlay, "optional_inputs", sequence_type())
    view = render_current(
        ContextReaders(
            lambda _id: _portable_registry(),
            lambda _id, head: _revision(head),
            load_overlay=lambda _id: overlay,
            load_overlay_revision=lambda _id, head: _revision(
                head,
                workstream_id="local-ws",
                storage_class=__import__("mneme.core.artifacts", fromlist=["StorageClass"]).StorageClass.LOCAL_ONLY,
                objective="confidential foreground",
            ),
        ),
        "ws-1", "effective-local",
    )

    assert view.effective_status.value == "degraded"
    assert "confidential foreground" not in view.text


def test_context_view_does_not_change_when_reader_result_is_mutated_after_rendering():
    """Catches a generated view retaining mutable reader-owned SessionRevision state."""
    from mneme.core.context import ContextReaders, render_current
    from mneme.core.sessions import session_semantic_hash

    head = _portable_registry().active_heads[0]
    reader_result = _revision(head, objective="stable before render")
    view = render_current(
        ContextReaders(lambda _id: _portable_registry(), lambda _id, _head: reader_result),
        "ws-1", "portable",
    )

    object.__setattr__(reader_result.request.body, "objective", "mutable after render")
    object.__setattr__(
        reader_result.request.policy_evaluation,
        "semantic_hash",
        session_semantic_hash(reader_result.request.body, reader_result.request.relations),
    )

    assert "stable before render" in view.text
    assert "mutable after render" not in view.text
    assert view.portable.selected_revision.body.objective == "stable before render"
