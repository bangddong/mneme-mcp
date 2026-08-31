"""Immutable, self-contained Session checkpoint contracts."""

from dataclasses import replace
from datetime import datetime, timezone
from threading import Barrier, Event, local, Thread

import pytest


@pytest.fixture
def vault(tmp_path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


@pytest.fixture
def valid_checkpoint(vault):
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyRule, PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import CheckpointRequest, SessionBody, session_semantic_hash

    RegistryStore(vault).create_workstream("ws-1", project=None, mode="parallel")
    current_policy = PolicyStore(vault).load_active("vault-default", StorageClass.PORTABLE)

    def build(workstream_id="ws-1", session_id="ses-1", **changes):
        body = SessionBody(
            adapter_id="codex",
            objective="Persist session continuity",
            current_state="checkpointing",
            verified_facts=("Core writes immutable revisions.",),
            completed_work=("Validated request.",),
            blockers=(),
            next_actions=("Advance the active head.",),
            source_refs=(),
        )
        relations = ()
        semantic_hash = session_semantic_hash(body, relations)
        evaluation = evaluate_portability(
            Portability.PERSONAL_VAULT,
            (PolicyRule(current_policy.policy_id, current_policy.revision, Portability.PERSONAL_VAULT, current_policy.digest),),
            semantic_hash,
        )
        request = CheckpointRequest(
            workstream_id=workstream_id,
            session_id=session_id,
            storage_class=StorageClass.PORTABLE,
            expected_parent=None,
            expected_registry_generation=0,
            body=body,
            relations=relations,
            policy_evaluation=evaluation,
        )
        candidate = replace(request, **changes)
        if "body" in changes or "relations" in changes:
            candidate = replace(
                candidate,
                policy_evaluation=evaluate_portability(
                    Portability.PERSONAL_VAULT,
                    (PolicyRule(current_policy.policy_id, current_policy.revision, Portability.PERSONAL_VAULT, current_policy.digest),),
                    session_semantic_hash(candidate.body, candidate.relations),
                ),
            )
        return candidate

    return build


def test_checkpoint_writes_revision_then_advances_head(vault, valid_checkpoint):
    """Catches an implementation advancing a head before an immutable revision exists."""
    from mneme.core.sessions import SessionStore

    store = SessionStore(vault)
    ref = store.create_revision(valid_checkpoint())

    assert ref.revision == "000001"
    assert (vault.root / "workstreams/ws-1/sessions/ses-1/000001.md").exists()
    assert store.registries.load_workstream("ws-1").active_heads == (ref.as_head(),)


def test_portable_checkpoint_rejects_local_reference_before_writing(vault, valid_checkpoint):
    """Catches a portable revision disclosing that a local relation exists."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.errors import PortabilityViolation
    from mneme.core.sessions import SessionRelation, SessionStore

    relation = SessionRelation(
        kind="handoff",
        target="agent-b",
        purpose="continue",
        required_context="checkpoint",
        next_action="resume",
        provenance_refs=(
            ArtifactReference(ReferenceKind.ID, StorageClass.LOCAL_ONLY, "local-1"),
        ),
    )

    with pytest.raises(PortabilityViolation):
        SessionStore(vault).create_revision(valid_checkpoint(relations=(relation,)))

    assert not (vault.root / "workstreams/ws-1/sessions/ses-1/000001.md").exists()


def test_portable_checkpoint_rejects_local_source_reference_before_writing(
    vault, valid_checkpoint
):
    """Catches portable source provenance revealing a local-only source ID."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.errors import PortabilityViolation
    from mneme.core.policy import PolicyRule, PolicyStore, Portability, evaluate_portability
    from mneme.core.sessions import SessionStore, session_semantic_hash

    request = valid_checkpoint()
    body = replace(
        request.body,
        source_refs=(
            ArtifactReference(ReferenceKind.ID, StorageClass.LOCAL_ONLY, "local-source"),
        ),
    )
    policy = PolicyStore(vault).load_active("vault-default", StorageClass.PORTABLE)
    request = replace(
        request,
        body=body,
        policy_evaluation=evaluate_portability(
            Portability.PERSONAL_VAULT,
            (PolicyRule(policy.policy_id, policy.revision, Portability.PERSONAL_VAULT, policy.digest),),
            session_semantic_hash(body, ()),
        ),
    )

    with pytest.raises(PortabilityViolation):
        SessionStore(vault).create_revision(request)

    assert not (vault.root / "workstreams/ws-1/sessions/ses-1/000001.md").exists()


def test_policy_receipt_must_bind_exact_semantic_body_before_writing(vault, valid_checkpoint):
    """Catches a stale policy receipt authorizing different checkpoint semantics."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.sessions import SessionStore

    request = valid_checkpoint()
    stale = replace(request, body=replace(request.body, objective="Changed objective"))

    with pytest.raises(InvalidArtifact, match="semantic hash"):
        SessionStore(vault).create_revision(stale)

    assert not (vault.root / "workstreams/ws-1/sessions/ses-1/000001.md").exists()


def test_write_derives_restrictive_workstream_policy_not_present_in_caller_receipt(
    vault, valid_checkpoint
):
    """Catches a caller omitting the workstream's restrictive policy input."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import InvalidArtifact
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import SessionStore

    policy = PolicyStore(vault).create_revision(
        "workstream-ceiling", "1", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    registries = RegistryStore(vault)
    workstream = registries.load_workstream("ws-1")
    registries.update_workstream(
        workstream.with_policy_refs((policy,)), expected_generation=workstream.generation
    )

    with pytest.raises(InvalidArtifact, match="policy receipt"):
        SessionStore(vault).create_revision(valid_checkpoint(expected_registry_generation=1))

    assert not (vault.root / "workstreams/ws-1/sessions/ses-1/000001.md").exists()


def test_write_derives_restrictive_project_policy_not_present_in_caller_receipt(
    vault, valid_checkpoint
):
    """Catches a caller omitting the associated project's restrictive policy input."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import InvalidArtifact
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import SessionStore

    policies = PolicyStore(vault)
    policy = policies.create_revision(
        "project-ceiling", "1", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    registries = RegistryStore(vault)
    project = registries.register_project("project-1")
    registries.assign_project_policy(project.id, policy, expected_generation=0)
    workstream = registries.load_workstream("ws-1")
    registries.update_workstream(
        replace(workstream, project=project.id), expected_generation=workstream.generation
    )

    with pytest.raises(InvalidArtifact, match="policy receipt"):
        SessionStore(vault).create_revision(valid_checkpoint(expected_registry_generation=1))

    assert not (vault.root / "workstreams/ws-1/sessions/ses-1/000001.md").exists()


def test_write_derives_restrictive_source_policy_not_present_in_caller_receipt(
    vault, valid_checkpoint
):
    """Catches a portable source reference bypassing its local-only ceiling."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.errors import InvalidArtifact
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import SessionStore

    policies = PolicyStore(vault)
    policy = policies.create_revision(
        "source-ceiling", "1", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    registries = RegistryStore(vault)
    source = registries.register_source("source-1")
    registries.assign_source_policy(source.id, policy, expected_generation=0)
    request = valid_checkpoint(
        body=replace(
            valid_checkpoint().body,
            source_refs=(
                ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, source.id),
            ),
        )
    )

    with pytest.raises(InvalidArtifact, match="policy receipt"):
        SessionStore(vault).create_revision(request)

    assert not (vault.root / "workstreams/ws-1/sessions/ses-1/000001.md").exists()


def test_write_accepts_receipt_covering_all_applicable_source_policy_inputs(
    vault, valid_checkpoint
):
    """Catches rejecting a complete receipt that names each canonical policy input."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.policy import PolicyRule, PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import SessionStore, session_semantic_hash

    policies = PolicyStore(vault)
    source_policy = policies.create_revision(
        "source-permitted", "1", {"ceiling": "personal-vault"}, StorageClass.PORTABLE
    )
    source = RegistryStore(vault).register_source("source-1")
    RegistryStore(vault).assign_source_policy(source.id, source_policy, expected_generation=0)
    body = replace(
        valid_checkpoint().body,
        source_refs=(
            ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, source.id),
        ),
    )
    default = policies.load_active("vault-default", StorageClass.PORTABLE)
    request = replace(
        valid_checkpoint(body=body),
        policy_evaluation=evaluate_portability(
            Portability.PERSONAL_VAULT,
            (
                PolicyRule(default.policy_id, default.revision, Portability.PERSONAL_VAULT, default.digest),
                PolicyRule(source_policy.policy_id, source_policy.revision, Portability.PERSONAL_VAULT, source_policy.digest),
            ),
            session_semantic_hash(body, ()),
        ),
    )

    assert SessionStore(vault).create_revision(request).revision == "000001"


def test_write_derives_restrictive_typed_source_relation_provenance(vault, valid_checkpoint):
    """Catches relation provenance bypassing a referenced source's policy ceiling."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import InvalidArtifact
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import SessionProvenanceRef, SessionRelation, SessionStore

    policy = PolicyStore(vault).create_revision(
        "relation-source-ceiling", "1", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    source = RegistryStore(vault).register_source("source-relation")
    RegistryStore(vault).assign_source_policy(source.id, policy, expected_generation=0)
    relation = SessionRelation(
        "handoff", "agent-b", "continue", "checkpoint", "resume",
        (SessionProvenanceRef("source", StorageClass.PORTABLE, source.id),),
    )

    with pytest.raises(InvalidArtifact, match="policy receipt"):
        SessionStore(vault).create_revision(valid_checkpoint(relations=(relation,)))

    assert not (vault.root / "workstreams/ws-1/sessions/ses-1/000001.md").exists()


def test_portable_checkpoint_rejects_untyped_relation_provenance(vault, valid_checkpoint):
    """Catches Core guessing whether an untyped portable provenance ID is a source."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.errors import PortabilityViolation
    from mneme.core.sessions import SessionRelation, SessionStore

    relation = SessionRelation(
        "handoff", "agent-b", "continue", "checkpoint", "resume",
        (ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, "ambiguous"),),
    )

    with pytest.raises(PortabilityViolation, match="typed"):
        SessionStore(vault).create_revision(valid_checkpoint(relations=(relation,)))


def test_typed_non_source_relation_provenance_does_not_require_source_registry(
    vault, valid_checkpoint
):
    """Catches a typed non-source provenance relation being guessed as a source."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.sessions import SessionProvenanceRef, SessionRelation, SessionStore

    relation = SessionRelation(
        "handoff", "agent-b", "continue", "checkpoint", "resume",
        (SessionProvenanceRef("memory", StorageClass.PORTABLE, "memory-1"),),
    )

    assert SessionStore(vault).create_revision(valid_checkpoint(relations=(relation,))).revision == "000001"


def test_checkpoint_publication_holds_gate_against_default_policy_tightening(
    vault, valid_checkpoint, monkeypatch
):
    """Catches a default-policy activation linearizing between admission and write."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.sessions import SessionStore

    policies = PolicyStore(vault)
    tightening = policies.create_revision(
        "vault-default", "2", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    started, finished = Event(), Event()
    workers = []

    def activate():
        started.set()
        policies.activate(tightening, expected_generation=1)
        finished.set()

    store = SessionStore(vault)
    original = store._document

    def publish_after_check(*args):
        if workers:
            return original(*args)
        writer = Thread(target=activate)
        workers.append(writer)
        writer.start()
        assert started.wait(1)
        assert not finished.wait(0.1)
        return original(*args)

    monkeypatch.setattr(store, "_document", publish_after_check)
    ref = store.create_revision(valid_checkpoint())
    workers[0].join(timeout=5)

    assert ref.revision == "000001"
    assert finished.is_set()
    assert policies.load_active("vault-default", StorageClass.PORTABLE) == tightening


@pytest.mark.parametrize("mutation", ["source", "project", "workstream"])
def test_applicability_registry_mutations_share_policy_admission_gate(vault, mutation):
    """Catches an assignment or workstream change bypassing checkpoint admission lock."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore, policy_admission_gate
    from mneme.core.registries import RegistryStore

    policies = PolicyStore(vault)
    policy = policies.create_revision(
        f"{mutation}-policy", "1", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    registries = RegistryStore(vault)
    if mutation == "source":
        source = registries.register_source("source-gated")
        action = lambda: registries.assign_source_policy(source.id, policy, expected_generation=0)
    elif mutation == "project":
        project = registries.register_project("project-gated")
        action = lambda: registries.assign_project_policy(project.id, policy, expected_generation=0)
    else:
        workstream = registries.create_workstream("ws-gated", project=None, mode="parallel")
        action = lambda: registries.update_workstream(
            workstream.with_policy_refs((policy,)), expected_generation=0
        )
    finished = Event()

    def mutate():
        action()
        finished.set()

    with policy_admission_gate(vault):
        worker = Thread(target=mutate)
        worker.start()
        assert not finished.wait(0.1)
    worker.join(timeout=5)

    assert finished.is_set()


def test_policy_admission_gate_releases_after_checkpoint_validation_exception(
    vault, valid_checkpoint
):
    """Catches a failed admission leaving later canonical mutations deadlocked."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import InvalidArtifact
    from mneme.core.policy import PolicyStore
    from mneme.core.sessions import SessionStore

    with pytest.raises(InvalidArtifact):
        SessionStore(vault).create_revision(
            replace(valid_checkpoint(), body=replace(valid_checkpoint().body, objective="mismatch"))
        )

    created = PolicyStore(vault).create_revision(
        "post-failure", "1", {"ceiling": "personal-vault"}, StorageClass.PORTABLE
    )
    assert created.revision == "1"


def test_revision_timestamp_is_canonical_utc_and_decoded(vault, valid_checkpoint):
    """Catches a checkpoint omitting its D3 UTC boundary timestamp."""
    from mneme.core.sessions import SessionStore

    timestamp = datetime(2026, 8, 31, 12, 34, 56, 123456, tzinfo=timezone.utc)
    store = SessionStore(vault, clock=lambda: timestamp)
    ref = store.create_revision(valid_checkpoint())
    metadata = vault.reader.read(
        __import__("mneme.core.artifacts", fromlist=["ArtifactFamily"]).ArtifactFamily.SESSION,
        storage_class=__import__("mneme.core.artifacts", fromlist=["StorageClass"]).StorageClass.PORTABLE,
        relative_path="workstreams/ws-1/sessions/ses-1/000001.md",
    ).metadata

    assert metadata["timestamp"] == "2026-08-31T12:34:56.123456Z"
    assert store.read_revision(ref, workstream_id="ws-1").revision_timestamp == timestamp


def test_tampered_revision_timestamp_is_rejected(vault, valid_checkpoint):
    """Catches a non-canonical timestamp accepted by the self-contained decoder."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.sessions import SessionStore

    store = SessionStore(vault)
    ref = store.create_revision(valid_checkpoint())
    path = vault.root / "workstreams/ws-1/sessions/ses-1/000001.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("timestamp:", "timestamp: not-a-time #"),
        encoding="utf-8",
    )

    with pytest.raises(InvalidArtifact, match="timestamp"):
        store.read_revision(ref, workstream_id="ws-1")


def test_historic_revision_reads_after_policy_active_pointer_advances(vault, valid_checkpoint):
    """Catches historic audit reads requiring the old policy to remain active."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.sessions import SessionStore

    store = SessionStore(vault)
    ref = store.create_revision(valid_checkpoint())
    policies = PolicyStore(vault)
    replacement = policies.create_revision(
        "vault-default", "2", {"ceiling": "personal-vault"}, StorageClass.PORTABLE
    )
    policies.activate(replacement, expected_generation=1)

    assert store.read_revision(ref, workstream_id="ws-1").policy_evaluation.refs[0].revision == "1"


@pytest.mark.parametrize("failure", ["missing", "tampered"])
def test_historic_read_rejects_missing_or_tampered_immutable_policy_revision(
    vault, valid_checkpoint, failure
):
    """Catches audit reads trusting a receipt whose immutable policy is unavailable."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.sessions import SessionStore

    store = SessionStore(vault)
    ref = store.create_revision(valid_checkpoint())
    policy = vault.root / ".madi/policies/vault-default/1.yaml"
    if failure == "missing":
        policy.unlink()
    else:
        policy.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(InvalidArtifact):
        store.read_revision(ref, workstream_id="ws-1")


def test_revision_parent_must_match_contiguous_existing_lineage(vault, valid_checkpoint):
    """Catches a writer skipping a predecessor or silently healing a revision gap."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.sessions import SessionRevisionRef, SessionStore

    store = SessionStore(vault)
    first = store.create_revision(valid_checkpoint())
    stale_parent = SessionRevisionRef("ses-1", "000000")

    with pytest.raises(InvalidArtifact, match="expected parent"):
        store.create_revision(
            valid_checkpoint(
                expected_parent=stale_parent,
                expected_registry_generation=1,
            )
        )

    assert not (vault.root / "workstreams/ws-1/sessions/ses-1/000002.md").exists()
    assert first.revision == "000001"


def test_second_revision_advances_its_exact_parent_head(vault, valid_checkpoint):
    """Catches repeated checkpoints leaving an ordinary linear session stale."""
    from mneme.core.sessions import SessionStore

    store = SessionStore(vault)
    first = store.create_revision(valid_checkpoint())
    second = store.create_revision(
        valid_checkpoint(expected_parent=first, expected_registry_generation=1)
    )

    assert second.revision == "000002"
    assert store.registries.load_workstream("ws-1").active_heads == (second.as_head(),)


def test_revision_gap_is_rejected_without_creating_a_new_revision(vault, valid_checkpoint):
    """Catches a writer filling a gap in an immutable session lineage."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.sessions import SessionStore

    store = SessionStore(vault)
    first = store.create_revision(valid_checkpoint())
    revisions = vault.root / "workstreams/ws-1/sessions/ses-1"
    (revisions / "000003.md").write_text(
        (revisions / "000001.md").read_text(encoding="utf-8"), encoding="utf-8"
    )

    with pytest.raises(InvalidArtifact, match="revision gap"):
        store.create_revision(valid_checkpoint(expected_parent=first, expected_registry_generation=1))

    assert not (revisions / "000002.md").exists()


def test_same_session_cas_conflict_keeps_written_revision_as_doctor_orphan(
    vault, valid_checkpoint, monkeypatch
):
    """Catches auto-merging a raced same-session parent-to-child replacement."""
    from mneme.core.registries import RegistryConflict, RegistryStore
    from mneme.core.sessions import SessionStore

    store = SessionStore(vault)
    first = store.create_revision(valid_checkpoint())
    original = store.registries.update_workstream

    def raced_update(registry, *, expected_generation):
        current = RegistryStore(vault).load_workstream("ws-1")
        RegistryStore(vault).update_workstream(
            current.with_status("paused"), expected_generation=current.generation
        )
        return original(registry, expected_generation=expected_generation)

    monkeypatch.setattr(store.registries, "update_workstream", raced_update)

    with pytest.raises(RegistryConflict):
        store.create_revision(
            valid_checkpoint(
                expected_parent=first,
                expected_registry_generation=1,
            )
        )

    assert (vault.root / "workstreams/ws-1/sessions/ses-1/000002.md").exists()
    assert store.registries.load_workstream("ws-1").active_heads == (first.as_head(),)


def test_disjoint_session_checkpoint_race_retries_to_preserve_both_heads(
    vault, valid_checkpoint, monkeypatch
):
    """Catches a stale second session update orphaning a proven-disjoint checkpoint."""
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import SessionStore

    barrier = Barrier(2)
    thread_state = local()
    original = RegistryStore.add_active_head

    def synchronized_add(self, *args, **kwargs):
        if not getattr(thread_state, "waited", False):
            thread_state.waited = True
            barrier.wait(timeout=5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RegistryStore, "add_active_head", synchronized_add)
    results, errors = [], []

    def checkpoint(session_id):
        try:
            results.append(SessionStore(vault).create_revision(valid_checkpoint(session_id=session_id)))
        except Exception as exc:  # the test asserts both independent writes succeeded
            errors.append(exc)

    first = Thread(target=checkpoint, args=("ses-a",))
    second = Thread(target=checkpoint, args=("ses-b",))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not errors
    assert {result.session for result in results} == {"ses-a", "ses-b"}
    assert {head.session for head in RegistryStore(vault).load_workstream("ws-1").active_heads} == {"ses-a", "ses-b"}


def test_local_checkpoint_uses_same_codec_and_self_contained_decoder(vault):
    """Catches local revisions bypassing the Session codec or accepting tampered files."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import InvalidArtifact
    from mneme.core.policy import PolicyRule, PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import CheckpointRequest, SessionBody, SessionStore, session_semantic_hash

    RegistryStore(vault, StorageClass.LOCAL_ONLY).create_workstream(
        "ws-local", project=None, mode="parallel"
    )
    body = SessionBody(
        adapter_id="codex", objective="Local state", current_state="working",
        verified_facts=("local evidence retained",), completed_work=(), blockers=(),
        next_actions=("resume",), source_refs=(),
    )
    default = PolicyStore(vault).load_active("vault-default", StorageClass.PORTABLE)
    request = CheckpointRequest(
        "ws-local", "ses-local", StorageClass.LOCAL_ONLY, None, 0, body, (),
        evaluate_portability(
            Portability.LOCAL_ONLY,
            (PolicyRule(default.policy_id, default.revision, Portability.PERSONAL_VAULT, default.digest),),
            session_semantic_hash(body, ()),
        ),
    )
    store = SessionStore(vault, storage_class=StorageClass.LOCAL_ONLY)
    ref = store.create_revision(request)
    location = vault.router.location(
        __import__("mneme.core.artifacts", fromlist=["ArtifactFamily"]).ArtifactFamily.SESSION,
        StorageClass.LOCAL_ONLY,
        "workstreams/ws-local/sessions/ses-local/000001.md",
    )

    assert vault.reader.read(
        __import__("mneme.core.artifacts", fromlist=["ArtifactFamily"]).ArtifactFamily.SESSION,
        location=location,
    ).metadata["schema"] == "madi.session-revision.v1"
    location.path.write_text("---\nschema: madi.session-revision.v1\n---\ntampered\n", encoding="utf-8")
    with pytest.raises(InvalidArtifact):
        store.read_revision(ref, workstream_id="ws-local")
