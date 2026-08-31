from __future__ import annotations

import pytest


def _revision(head, *, workstream_id="ws-1", storage_class=None):
    from mneme.core.artifacts import StorageClass
    from mneme.core.resolver import SessionRevision
    from mneme.core.sessions import SessionBody

    return SessionRevision(
        head=head,
        workstream_id=workstream_id,
        storage_class=storage_class or StorageClass.PORTABLE,
        body=SessionBody(
            adapter_id="codex",
            objective="Validated objective",
            current_state="Validated state",
            verified_facts=("Validated fact",),
            completed_work=(),
            blockers=(),
            next_actions=("Continue",),
            source_refs=(),
        ),
    )


@pytest.fixture
def registry_factory():
    from mneme.core.registries import HeadRef, WorkstreamRegistry

    def build(mode: str, heads: list[str], preferred: str | None, *, status: str = "active"):
        refs = tuple(HeadRef(f"session-{head}", "000001") for head in heads)
        preferred_head = next(
            (head for head in refs if head.session == f"session-{preferred}"), None
        )
        if mode == "preferred" and preferred_head is None:
            # Build a valid record first so resolver validation can exercise a
            # registry that was tampered after it was decoded.
            registry = WorkstreamRegistry("ws-1", 0, None, status, "parallel", refs)
            object.__setattr__(registry, "mode", "preferred")
            return registry
        return WorkstreamRegistry(
            "ws-1", 0, None, status, mode, refs, preferred_head
        )

    return build


@pytest.mark.parametrize(("mode", "heads", "preferred", "expected"), [
    ("single", ["a"], None, "resolved"),
    ("preferred", ["a", "b"], "b", "resolved"),
    ("parallel", ["a", "b"], None, "divergent"),
    ("preferred", ["a", "b"], None, "invalid"),
    ("single", [], None, "invalid"),
])
def test_resolution_states(registry_factory, mode, heads, preferred, expected):
    """Catches a resolver treating registry mode as a session/timestamp heuristic."""
    from mneme.core.resolver import resolve_workstream

    registry = registry_factory(mode, heads, preferred)
    loaded = {head: _revision(head) for head in registry.active_heads}

    assert resolve_workstream(registry, loaded.get).state.value == expected


def test_closed_empty_workstream_is_a_resolved_empty_projection(registry_factory):
    """Catches a closed registry with no active heads being rejected as corruption."""
    from mneme.core.resolver import resolve_workstream

    resolved = resolve_workstream(registry_factory("single", [], None, status="closed"), lambda _head: None)

    assert resolved.state.value == "resolved"
    assert resolved.heads == ()
    assert resolved.selected_head is None


def test_missing_active_revision_is_invalid(registry_factory):
    """Catches rendering a registry-selected head whose immutable revision vanished."""
    from mneme.core.resolver import resolve_workstream

    resolved = resolve_workstream(registry_factory("single", ["a"], None), lambda _head: None)

    assert resolved.state.value == "invalid"


def test_unavailable_optional_input_degrades_a_valid_resolution(registry_factory):
    """Catches an optional mount failure being treated as canonical head loss."""
    from mneme.core.resolver import OptionalInputUnavailable, resolve_workstream

    registry = registry_factory("single", ["a"], None)
    resolved = resolve_workstream(
        registry, lambda head: _revision(head), optional_inputs=(OptionalInputUnavailable("mount unavailable"),)
    )

    assert resolved.state.value == "degraded"
    assert resolved.selected_head == registry.active_heads[0]


def test_explicit_heads_are_sorted_and_unlisted_revisions_are_never_loaded(registry_factory):
    """Catches a resolver scanning a session directory or accepting registry order as time."""
    from mneme.core.resolver import resolve_workstream

    registry = registry_factory("parallel", ["z", "a"], None)
    calls = []

    def load(head):
        calls.append(head)
        return _revision(head)

    resolved = resolve_workstream(registry, load)

    assert [head.session for head in resolved.heads] == ["session-a", "session-z"]
    assert calls == list(resolved.heads)


def test_tampered_registry_or_revision_is_invalid(registry_factory):
    """Catches bypassing SessionStore validation or trusting a mutated registry object."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.resolver import resolve_workstream

    valid = registry_factory("single", ["a"], None)
    object.__setattr__(valid, "active_heads", (valid.active_heads[0], valid.active_heads[0]))

    assert resolve_workstream(valid, lambda head: _revision(head)).state.value == "invalid"
    intact = registry_factory("single", ["a"], None)
    assert resolve_workstream(
        intact, lambda _head: (_ for _ in ()).throw(InvalidArtifact("forged revision"))
    ).state.value == "invalid"


def test_tampered_registry_identity_or_generation_is_invalid(registry_factory):
    """Catches resolution trusting a registry whose foundational fields were altered."""
    from mneme.core.resolver import resolve_workstream

    registry = registry_factory("single", ["a"], None)
    object.__setattr__(registry, "generation", -1)

    assert resolve_workstream(registry, lambda head: _revision(head)).state.value == "invalid"


@pytest.mark.parametrize(
    ("returned", "expected"),
    [
        (lambda head: object(), "invalid"),
        (lambda head: _revision(head, workstream_id="other-workstream"), "invalid"),
        (lambda head: _revision(head, storage_class=__import__("mneme.core.artifacts", fromlist=["StorageClass"]).StorageClass.LOCAL_ONLY), "invalid"),
        (lambda head: _revision(__import__("mneme.core.registries", fromlist=["HeadRef"]).HeadRef("other-session", head.revision)), "invalid"),
        (lambda head: _revision(head), "resolved"),
    ],
)
def test_revision_reader_requires_a_typed_exact_portable_session(registry_factory, returned, expected):
    """Catches a foreign, local, or arbitrary reader value entering canonical CURRENT."""
    from mneme.core.resolver import resolve_workstream

    assert resolve_workstream(registry_factory("single", ["a"], None), returned).state.value == expected


def test_required_reader_exception_is_invalid_but_malformed_optional_inputs_degrade(registry_factory):
    """Catches treating required canonical loss like an optional mount failure."""
    from mneme.core.resolver import resolve_workstream

    registry = registry_factory("single", ["a"], None)
    assert resolve_workstream(
        registry, lambda _head: (_ for _ in ()).throw(FileNotFoundError("revision missing"))
    ).state.value == "invalid"
    assert resolve_workstream(registry, lambda head: _revision(head), optional_inputs=object()).state.value == "degraded"
