from __future__ import annotations

from dataclasses import replace

import pytest


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
    loaded = {head: object() for head in registry.active_heads}

    assert resolve_workstream(registry, loaded.get).state.value == expected


def test_closed_empty_workstream_is_a_resolved_empty_projection(registry_factory):
    """Catches a closed registry with no active heads being rejected as corruption."""
    from mneme.core.resolver import resolve_workstream

    resolved = resolve_workstream(
        registry_factory("single", [], None, status="closed"), lambda _head: None
    )

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
        registry, lambda _head: object(), optional_inputs=(OptionalInputUnavailable("mount unavailable"),)
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
        return object()

    resolved = resolve_workstream(registry, load)

    assert [head.session for head in resolved.heads] == ["session-a", "session-z"]
    assert calls == list(resolved.heads)


def test_tampered_registry_or_revision_is_invalid(registry_factory):
    """Catches bypassing SessionStore validation or trusting a mutated registry object."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.resolver import resolve_workstream

    valid = registry_factory("single", ["a"], None)
    object.__setattr__(valid, "active_heads", (valid.active_heads[0], valid.active_heads[0]))

    assert resolve_workstream(valid, lambda _head: object()).state.value == "invalid"
    intact = registry_factory("single", ["a"], None)
    assert resolve_workstream(
        intact, lambda _head: (_ for _ in ()).throw(InvalidArtifact("forged revision"))
    ).state.value == "invalid"


def test_tampered_registry_identity_or_generation_is_invalid(registry_factory):
    """Catches resolution trusting a registry whose foundational fields were altered."""
    from mneme.core.resolver import resolve_workstream

    registry = registry_factory("single", ["a"], None)
    object.__setattr__(registry, "generation", -1)

    assert resolve_workstream(registry, lambda _head: object()).state.value == "invalid"
