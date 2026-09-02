from dataclasses import replace
from threading import Barrier, Thread

import pytest


@pytest.fixture
def vault(tmp_path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


@pytest.fixture
def memory_store(vault):
    from mneme.core.memories import MemoryStore

    return MemoryStore(vault)


@pytest.fixture
def receipt(vault):
    from mneme.core.memories import memory_semantic_hash
    from mneme.core.policy import PolicyStore, evaluate_portability

    default = PolicyStore(vault).load_active("vault-default", __import__("mneme.core.artifacts", fromlist=["StorageClass"]).StorageClass.PORTABLE)
    return lambda body, **semantic: evaluate_portability(
        __import__("mneme.core.policy", fromlist=["Portability"]).Portability.PERSONAL_VAULT,
        (PolicyStore(vault).load_rule(default),),
        memory_semantic_hash(body=body, **semantic),
    )


def test_portable_candidate_is_not_implicitly_accepted(memory_store, receipt):
    record = memory_store.submit_candidate(
        kind="knowledge", scope={"type": "personal-global"}, authority="personal",
        portability="personal-vault", body="FTS first remains useful without a model.",
        receipt=receipt("FTS first remains useful without a model."),
    )

    assert record.status.value == "candidate"
    assert (memory_store.vault.root / f"memory/{record.id}.md").exists()


def test_candidate_body_is_cas_mutable_then_promotion_freezes_semantics(memory_store, receipt):
    from mneme.core.errors import ConcurrentWrite, InvalidArtifact

    candidate = memory_store.submit_candidate(
        kind="lesson", scope={"type": "personal-global"}, authority="personal",
        portability="personal-vault", body="Start with FTS.", receipt=receipt("Start with FTS.", kind="lesson"),
    )
    edited = memory_store.edit_candidate(
        candidate.id, expected_generation=0, body="Start with FTS before embeddings.",
        receipt=receipt("Start with FTS before embeddings.", kind="lesson"),
    )
    assert edited.generation == 1
    with pytest.raises(ConcurrentWrite):
        memory_store.edit_candidate(candidate.id, expected_generation=0, body="stale", receipt=receipt("stale", kind="lesson"))

    accepted = memory_store.promote(candidate.id, expected_generation=1, receipt=receipt("Start with FTS before embeddings.", kind="lesson"))
    assert accepted.status.value == "accepted"
    assert accepted.semantic_hash == edited.semantic_hash
    with pytest.raises(InvalidArtifact, match="candidate"):
        memory_store.edit_candidate(candidate.id, expected_generation=2, body="cannot change", receipt=receipt("cannot change", kind="lesson"))


def test_accepted_change_creates_successor_and_reverse_link_is_derived(memory_store, receipt):
    candidate = memory_store.submit_candidate(
        kind="preference", scope={"type": "personal-global"}, authority="personal",
        portability="personal-vault", body="Prefer concise reviews.", receipt=receipt("Prefer concise reviews.", kind="preference"),
    )
    accepted = memory_store.promote(candidate.id, 0, receipt=receipt(candidate.body, kind="preference"))
    successor = memory_store.supersede(
        accepted.id, expected_generation=1, body="Prefer concise reviews with evidence.",
        receipt=receipt("Prefer concise reviews with evidence.", kind="preference", supersedes=accepted.id),
    )

    assert successor.id != accepted.id
    assert successor.supersedes == accepted.id
    assert memory_store.read(accepted.id).superseded_by == successor.id
    assert "superseded_by" not in (memory_store.vault.root / f"memory/{accepted.id}.md").read_text(encoding="utf-8")


def test_retirement_changes_only_lifecycle_envelope(memory_store, receipt):
    from mneme.core.errors import ConcurrentWrite

    candidate = memory_store.submit_candidate(
        kind="knowledge", scope={"type": "personal-global"}, authority="personal",
        portability="personal-vault", body="A fact.", receipt=receipt("A fact."),
    )
    accepted = memory_store.promote(candidate.id, 0, receipt=receipt(candidate.body))
    retired = memory_store.retire(accepted.id, expected_generation=1, reason="obsolete")
    assert retired.status.value == "retired"
    assert retired.semantic_hash == accepted.semantic_hash
    with pytest.raises(ConcurrentWrite):
        memory_store.retire(accepted.id, expected_generation=1, reason="stale")


def test_portable_rejects_local_reference_before_file_creation(memory_store, receipt):
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.errors import PortabilityViolation

    with pytest.raises(PortabilityViolation):
        memory_store.submit_candidate(
            kind="knowledge", scope={"type": "personal-global"}, authority="personal",
            portability="personal-vault", body="No local disclosure.",
            receipt=receipt("No local disclosure."),
            provenance=(ArtifactReference(ReferenceKind.PATH, StorageClass.LOCAL_ONLY, "C:/secret"),),
        )
    assert list((memory_store.vault.root / "memory").glob("*.md")) == []


def test_local_memory_uses_same_codec_and_may_reference_portable(vault):
    from mneme.core.artifacts import ArtifactReference, ArtifactFamily, ReferenceKind, StorageClass
    from mneme.core.memories import MemoryStore
    from mneme.core.policy import Portability, evaluate_portability

    store = MemoryStore(vault, StorageClass.LOCAL_ONLY)
    record = store.submit_candidate(
        kind="knowledge", scope={"type": "personal-global"}, authority="personal",
        portability=Portability.LOCAL_ONLY, body="Local note.",
        receipt=evaluate_portability(Portability.LOCAL_ONLY, (), __import__("mneme.core.memories", fromlist=["memory_semantic_hash"]).memory_semantic_hash(body="Local note.", portability=Portability.LOCAL_ONLY, provenance=(ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, "portable-memory"),))),
        provenance=(ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, "portable-memory"),),
    )
    # A local write recalculates its receipt; its codec is the canonical memory codec.
    assert vault.reader.read(ArtifactFamily.MEMORY, storage_class=StorageClass.LOCAL_ONLY, relative_path=f"memory/{record.id}.md").metadata["schema"] == "madi.memory.v1"


def test_tampered_memory_and_unsafe_id_are_rejected(memory_store, receipt):
    from mneme.core.errors import InvalidArtifact

    record = memory_store.submit_candidate(
        kind="knowledge", scope={"type": "personal-global"}, authority="personal",
        portability="personal-vault", body="Canonical.", receipt=receipt("Canonical."),
    )
    path = memory_store.vault.root / f"memory/{record.id}.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Canonical.", "tampered"), encoding="utf-8")
    with pytest.raises(InvalidArtifact, match="hash|canonical"):
        memory_store.read(record.id)
    with pytest.raises(InvalidArtifact):
        memory_store.read("../unsafe")


def test_candidate_edit_conflict_race_has_one_winner(memory_store, receipt):
    from mneme.core.errors import ConcurrentWrite

    record = memory_store.submit_candidate(
        kind="knowledge", scope={"type": "personal-global"}, authority="personal",
        portability="personal-vault", body="Before.", receipt=receipt("Before."),
    )
    barrier, results = Barrier(2), []

    def edit(body):
        barrier.wait()
        try:
            results.append(memory_store.edit_candidate(record.id, 0, body=body, receipt=receipt(body)))
        except ConcurrentWrite:
            results.append("conflict")

    left, right = Thread(target=edit, args=("Left.",)), Thread(target=edit, args=("Right.",))
    left.start(); right.start(); left.join(timeout=5); right.join(timeout=5)
    assert len([item for item in results if item != "conflict"]) == 1
    assert results.count("conflict") == 1


def test_accepted_lifecycle_rejects_a_recomputed_raw_semantic_overwrite(memory_store, receipt):
    """Accepted semantics are bound to their in-tree promotion baseline, not Git."""
    from mneme.core.fs import dump_frontmatter, read_frontmatter
    from mneme.core.memories import memory_semantic_hash
    from mneme.core.errors import InvalidArtifact

    candidate = memory_store.submit_candidate(
        "knowledge", {"type": "personal-global"}, "personal", "personal-vault",
        "Original accepted fact.", receipt("Original accepted fact."),
    )
    accepted = memory_store.promote(candidate.id, 0, receipt(candidate.body))
    path = memory_store.vault.root / f"memory/{accepted.id}.md"
    metadata, _ = read_frontmatter(path)
    replacement = "Raw replacement with a recomputed receipt."
    digest = memory_semantic_hash(body=replacement)
    metadata["semantic_hash"] = digest
    metadata["policy_receipt"]["semantic_hash"] = digest
    path.write_text(dump_frontmatter(metadata, replacement), encoding="utf-8")

    with pytest.raises(InvalidArtifact, match="acceptance baseline|immutable"):
        memory_store.retire(accepted.id, 1, "attempt lifecycle mutation")


def test_concurrent_supersedes_reserve_one_successor(memory_store, receipt):
    from mneme.core.errors import InvalidArtifact

    candidate = memory_store.submit_candidate(
        "preference", {"type": "personal-global"}, "personal", "personal-vault",
        "Original preference.", receipt("Original preference.", kind="preference"),
    )
    accepted = memory_store.promote(candidate.id, 0, receipt(candidate.body, kind="preference"))
    barrier, results = Barrier(2), []

    def supersede(body):
        barrier.wait()
        try:
            results.append(memory_store.supersede(
                accepted.id, 1, body=body,
                receipt=receipt(body, kind="preference", supersedes=accepted.id),
            ))
        except InvalidArtifact:
            results.append("reserved")

    left = Thread(target=supersede, args=("First successor.",))
    right = Thread(target=supersede, args=("Second successor.",))
    left.start(); right.start(); left.join(timeout=5); right.join(timeout=5)

    assert len([item for item in results if item != "reserved"]) == 1
    assert results.count("reserved") == 1
    assert memory_store.read(accepted.id).status.value == "accepted"


@pytest.mark.parametrize("field", ["provenance", "violations", "approved_rules"])
def test_memory_decode_rejects_malformed_nested_entries(memory_store, receipt, field):
    from mneme.core.fs import dump_frontmatter, read_frontmatter
    from mneme.core.errors import InvalidArtifact

    record = memory_store.submit_candidate(
        "knowledge", {"type": "personal-global"}, "personal", "personal-vault",
        "Strict nested decoding.", receipt("Strict nested decoding."),
    )
    path = memory_store.vault.root / f"memory/{record.id}.md"
    metadata, body = read_frontmatter(path)
    if field == "provenance":
        metadata[field] = [{"kind": "id", "storage_class": "portable", "value": "valid"}, "malformed"]
    else:
        metadata["policy_receipt"][field] = ["malformed"]
    path.write_text(dump_frontmatter(metadata, body), encoding="utf-8")

    with pytest.raises(InvalidArtifact):
        memory_store.read(record.id)
