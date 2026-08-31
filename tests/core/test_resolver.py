from __future__ import annotations

import pytest


def _revision(head, *, workstream_id="ws-1", storage_class=None, relations=()):
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyEvaluation, Portability
    from mneme.core.resolver import SessionRevision
    from mneme.core.sessions import CheckpointRequest, SessionBody, session_semantic_hash
    from datetime import datetime, timezone

    storage = storage_class or StorageClass.PORTABLE
    body = SessionBody(
            adapter_id="codex",
            objective="Validated objective",
            current_state="Validated state",
            verified_facts=("Validated fact",),
            completed_work=(),
            blockers=(),
            next_actions=("Continue",),
            source_refs=(),
    )
    receipt = PolicyEvaluation(
        Portability.LOCAL_ONLY if storage is StorageClass.LOCAL_ONLY else Portability.PERSONAL_VAULT,
        Portability.LOCAL_ONLY if storage is StorageClass.LOCAL_ONLY else Portability.PERSONAL_VAULT,
        True, datetime(2026, 1, 1, tzinfo=timezone.utc), "test", (), session_semantic_hash(body, relations),
    )
    request = CheckpointRequest(
        workstream_id, head.session, storage, None, 0, body, relations, receipt,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return SessionRevision(head, request)


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


def test_invalid_registry_projection_does_not_retain_the_caller_object(registry_factory):
    """Catches an invalid projection exposing a mutable uncanonicalized registry."""
    from mneme.core.resolver import resolve_workstream

    registry = registry_factory("single", ["a"], None)
    object.__setattr__(registry, "generation", -1)

    resolved = resolve_workstream(registry, lambda head: _revision(head))

    assert resolved.state.value == "invalid"
    assert resolved.registry is None


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


@pytest.mark.parametrize("mutation", ["body", "relation_provenance", "head", "receipt"])
def test_nested_session_mutation_is_rejected_before_rendering(registry_factory, mutation):
    """Catches a frozen outer reader result hiding mutable historic session fields."""
    from mneme.core.resolver import resolve_workstream

    registry = registry_factory("single", ["a"], None)
    from mneme.core.artifacts import StorageClass
    from mneme.core.sessions import SessionProvenanceRef, SessionRelation

    relations = (
        SessionRelation(
            "handoff", "agent-b", "continue", "context", "resume",
            (SessionProvenanceRef("source", StorageClass.PORTABLE, "source-1"),),
        ),
    )
    revision = _revision(registry.active_heads[0], relations=relations)
    if mutation == "body":
        object.__setattr__(revision.request.body, "objective", "forged local payload")
    elif mutation == "relation_provenance":
        object.__setattr__(revision.request.relations[0].provenance_refs[0], "id", "forged-source")
    elif mutation == "head":
        object.__setattr__(revision.head, "revision", "000002")
    else:
        object.__setattr__(revision.request.policy_evaluation, "semantic_hash", "0" * 64)

    assert resolve_workstream(registry, lambda _head: revision).state.value == "invalid"


def test_resolver_owns_a_canonical_snapshot_not_the_mutable_reader_result(registry_factory):
    """Catches CURRENT retaining a caller object after historic validation copied it."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.resolver import resolve_workstream
    from mneme.core.sessions import SessionProvenanceRef, SessionRelation, session_semantic_hash

    registry = registry_factory("single", ["a"], None)
    relation = SessionRelation(
        "handoff", "agent-b", "continue", "context", "resume",
        (SessionProvenanceRef("source", StorageClass.PORTABLE, "source-1"),),
    )
    reader_result = _revision(registry.active_heads[0], relations=(relation,))
    object.__setattr__(reader_result.request.body, "objective", "pre-resolution objective")
    object.__setattr__(reader_result.request.relations[0], "target", "pre-resolution target")
    object.__setattr__(
        reader_result.request.policy_evaluation,
        "semantic_hash",
        session_semantic_hash(reader_result.request.body, reader_result.request.relations),
    )

    resolved = resolve_workstream(registry, lambda _head: reader_result)
    snapshot = resolved.selected_revision

    assert resolved.state.value == "resolved"
    assert snapshot is not reader_result
    assert snapshot.body.objective == "pre-resolution objective"
    assert snapshot.request.relations[0].target == "pre-resolution target"

    object.__setattr__(reader_result.request.body, "objective", "post-resolution payload")
    object.__setattr__(reader_result.request.relations[0], "target", "post-resolution target")
    object.__setattr__(
        reader_result.request.policy_evaluation,
        "semantic_hash",
        session_semantic_hash(reader_result.request.body, reader_result.request.relations),
    )

    assert snapshot.body.objective == "pre-resolution objective"
    assert snapshot.request.relations[0].target == "pre-resolution target"


def test_resolver_owns_registry_heads_selection_and_policy_refs_after_resolution():
    """Catches caller registry aliases changing any resolved projection field."""
    from mneme.core.policy import PolicyRef
    from mneme.core.registries import HeadRef, WorkstreamRegistry
    from mneme.core.resolver import resolve_workstream

    first = HeadRef("session-a", "000001")
    second = HeadRef("session-b", "000001")
    preferred = HeadRef("session-b", "000001")
    policy = PolicyRef("policy-1", "000001", "a" * 64)
    registry = WorkstreamRegistry(
        "ws-1", 7, "project-1", "active", "preferred",
        (first, second), preferred, (policy,),
    )

    resolved = resolve_workstream(registry, _revision)

    assert resolved.state.value == "resolved"
    assert resolved.registry is not registry
    assert resolved.registry.active_heads[0] is resolved.heads[0]
    assert resolved.registry.active_heads[1] is resolved.heads[1]
    assert resolved.registry.preferred_head is resolved.selected_head
    assert resolved.revisions[0][0] is resolved.heads[0]
    assert resolved.revisions[1][0] is resolved.heads[1]
    assert resolved.selected_head is resolved.heads[1]
    assert resolved.registry.policy_refs[0] is not policy

    object.__setattr__(registry, "id", "mutated-workstream")
    object.__setattr__(registry, "active_heads", ())
    object.__setattr__(registry, "preferred_head", None)
    object.__setattr__(registry, "policy_refs", ())
    object.__setattr__(first, "revision", "000002")
    object.__setattr__(second, "revision", "000002")
    object.__setattr__(preferred, "revision", "000002")
    object.__setattr__(policy, "revision", "000002")
    object.__setattr__(policy, "digest", "b" * 64)

    assert resolved.registry.id == "ws-1"
    assert [(head.session, head.revision) for head in resolved.registry.active_heads] == [
        ("session-a", "000001"),
        ("session-b", "000001"),
    ]
    assert [(head.session, head.revision) for head in resolved.heads] == [
        ("session-a", "000001"),
        ("session-b", "000001"),
    ]
    assert [(head.session, head.revision) for head, _revision_value in resolved.revisions] == [
        ("session-a", "000001"),
        ("session-b", "000001"),
    ]
    assert (resolved.selected_head.session, resolved.selected_head.revision) == (
        "session-b", "000001"
    )
    assert resolved.selected_revision.head == resolved.selected_head
    assert resolved.registry.policy_refs[0] == PolicyRef(
        "policy-1", "000001", "a" * 64
    )


def test_coherent_pre_resolution_registry_mutation_is_canonicalized():
    """Catches valid tampered values bypassing validation or remaining caller-owned."""
    from mneme.core.registries import HeadRef, WorkstreamRegistry
    from mneme.core.resolver import resolve_workstream

    caller_head = HeadRef("session-a", "000001")
    registry = WorkstreamRegistry(
        "ws-1", 0, None, "active", "single", (caller_head,)
    )
    object.__setattr__(caller_head, "revision", "000002")

    resolved = resolve_workstream(registry, _revision)

    assert resolved.state.value == "resolved"
    assert resolved.heads[0] == HeadRef("session-a", "000002")
    assert resolved.heads[0] is not caller_head
    assert resolved.registry.active_heads[0] is resolved.heads[0]
