"""Whole-Vault admission regressions: all durable fields and transport blobs."""

from dataclasses import replace
import subprocess

import pytest

from mneme.core.artifacts import ArtifactDocument, ArtifactFamily, ReferenceManifest, StorageClass
from mneme.core.errors import InvalidArtifact
from mneme.core.memories import MemoryStatus, MemoryStore, memory_semantic_hash
from mneme.core.policy import PolicyStore, Portability, evaluate_portability
from mneme.core.vault import Vault


@pytest.fixture
def vault(tmp_path):
    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-1")


def receipt(vault, body, **semantic):
    policies = PolicyStore(vault)
    return evaluate_portability(Portability.PERSONAL_VAULT,
        (policies.load_rule(policies.load_active("vault-default", StorageClass.PORTABLE)),),
        memory_semantic_hash(body=body, **semantic))


@pytest.mark.parametrize("field", ["body", "rationale", "retirement_reason", "id"])
def test_memory_rejects_secrets_before_portable_mutation(vault, field):
    store = MemoryStore(vault)
    secret = "api_key: FINAL_SENTINEL_12345"
    body = secret if field == "body" else "Safe semantic memory."
    rationale = secret if field == "rationale" else ""
    identifier = "ghp_" + "A" * 36 if field == "id" else "memory-safe"
    before = tuple((vault.root / "memory").glob("*.md"))
    if field == "retirement_reason":
        record = store.submit_candidate("knowledge", {"type": "personal-global"}, "personal",
            "personal-vault", body, receipt(vault, body), memory_id=identifier)
        before_bytes = (vault.root / "memory" / f"{record.id}.md").read_bytes()
        with pytest.raises(InvalidArtifact, match="credential"):
            store.retire(record.id, 0, secret)
        assert (vault.root / "memory" / f"{record.id}.md").read_bytes() == before_bytes
    else:
        with pytest.raises(InvalidArtifact, match="credential"):
            store.submit_candidate("knowledge", {"type": "personal-global"}, "personal",
                "personal-vault", body, receipt(vault, body, rationale=rationale),
                rationale=rationale, memory_id=identifier)
        assert tuple((vault.root / "memory").glob("*.md")) == before


@pytest.mark.parametrize("family,path", [
    (ArtifactFamily.REGISTRY, "projects/safe.yaml"),
    (ArtifactFamily.SESSION, "workstreams/ws/sessions/session/000001.md"),
    (ArtifactFamily.MEMORY, "memory/safe.md"),
])
def test_foundation_rejects_nested_metadata_credentials(vault, family, path):
    document = ArtifactDocument({"nested": {"api_key": "FINAL_SENTINEL_12345"}},
        None if family is ArtifactFamily.REGISTRY else "Safe.", ReferenceManifest.complete())
    with pytest.raises(InvalidArtifact, match="credential"):
        vault.artifacts.write_new(family, storage_class=StorageClass.PORTABLE,
            relative_path=path, document=document)
    assert not (vault.root / path).exists()


def test_policy_rule_and_vault_owner_admission_reject_credentials(vault, tmp_path):
    with pytest.raises(InvalidArtifact, match="credential"):
        PolicyStore(vault).create_revision("p", "1", {"ceiling": "personal-vault",
            "note": "api_key: FINAL_SENTINEL_12345"}, StorageClass.PORTABLE)
    with pytest.raises(InvalidArtifact, match="credential"):
        Vault.initialize(tmp_path / "other", tmp_path / "other-state", "api_key: FINAL_SENTINEL_12345")
    assert not (tmp_path / "other").exists()


def test_raw_local_evidence_remains_allowed(vault):
    path = "memory/evidence.md"
    vault.artifacts.write_new(ArtifactFamily.MEMORY, storage_class=StorageClass.LOCAL_ONLY,
        relative_path=path, document=ArtifactDocument({"raw": "api_key: FINAL_SENTINEL_12345"},
            "api_key: FINAL_SENTINEL_12345"))
    assert "FINAL_SENTINEL_12345" in (vault.local_root / "overlays" / path).read_text(encoding="utf-8")


def test_local_checkpoint_may_retain_confidential_evidence(vault):
    from mneme.core.policy import PolicyRule
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import (
        CheckpointRequest,
        SessionBody,
        SessionStore,
        session_semantic_hash,
    )

    RegistryStore(vault, StorageClass.LOCAL_ONLY).create_workstream(
        "local-evidence", project=None, mode="parallel"
    )
    body = SessionBody(
        "codex",
        "Retain local incident evidence",
        "api_key: FINAL_SENTINEL_12345",
        (),
        (),
        (),
        ("rotate credential",),
        (),
    )
    policies = PolicyStore(vault)
    default = policies.load_active("vault-default", StorageClass.PORTABLE)
    evaluation = evaluate_portability(
        Portability.LOCAL_ONLY,
        (
            PolicyRule(
                default.policy_id,
                default.revision,
                Portability.PERSONAL_VAULT,
                default.digest,
            ),
        ),
        session_semantic_hash(body, ()),
    )
    request = CheckpointRequest(
        "local-evidence",
        "local-session",
        StorageClass.LOCAL_ONLY,
        None,
        0,
        body,
        (),
        evaluation,
    )

    ref = SessionStore(vault, StorageClass.LOCAL_ONLY).create_revision(request)

    assert ref.revision == "000001"


def test_portable_memory_rejects_newline_split_credential_assignment(vault):
    body = "api_key:\nFINAL_SENTINEL_12345"
    with pytest.raises(InvalidArtifact, match="credential"):
        MemoryStore(vault).submit_candidate(
            "knowledge",
            {"type": "personal-global"},
            "personal",
            "personal-vault",
            body,
            receipt(vault, body),
        )


def test_doctor_and_sync_reject_credentials_from_manual_policy_edit(vault):
    from mneme.core.doctor import Doctor
    from mneme.core.git_sync import sync_preflight

    PolicyStore(vault).create_revision("orphan", "1", {"ceiling": "personal-vault", "note": "safe"}, StorageClass.PORTABLE)
    path = vault.root / ".madi/policies/orphan/1.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("note: safe", "note: 'api_key: FINAL_SENTINEL_12345'"), encoding="utf-8")
    assert Doctor(vault).run().status == "invalid"
    assert sync_preflight(vault).allowed is False


def test_staged_git_blob_is_rejected_when_checkout_is_safe(vault):
    from mneme.core.git_sync import _validate_commit_tree

    def git(*args):
        return subprocess.run(["git", *args], cwd=vault.root, check=True,
            capture_output=True, text=True, encoding="utf-8").stdout.strip()

    git("init")
    PolicyStore(vault).create_revision("orphan", "1", {"ceiling": "personal-vault", "note": "safe"}, StorageClass.PORTABLE)
    path = vault.root / ".madi/policies/orphan/1.yaml"
    safe = path.read_text(encoding="utf-8")
    path.write_text(safe.replace("note: safe", "note: 'api_key: FINAL_SENTINEL_12345'"), encoding="utf-8")
    git("add", ".")
    tree = git("write-tree")
    path.write_text(safe, encoding="utf-8")
    assert _validate_commit_tree(vault.root, tree) is False


def test_retired_memory_requires_complete_retirement_envelope(vault):
    record = MemoryStore(vault).submit_candidate("knowledge", {"type": "personal-global"},
        "personal", "personal-vault", "Safe.", receipt(vault, "Safe."))
    with pytest.raises(InvalidArtifact, match="retire"):
        replace(record, status=MemoryStatus.RETIRED)


@pytest.mark.parametrize("operation", ["source", "project", "workstream", "update"])
def test_registry_disclosure_writes_fail_after_vault_policy_tightens(vault, operation):
    from mneme.core.registries import RegistryStore

    registries = RegistryStore(vault)
    workstream = registries.create_workstream("existing", project=None, mode="parallel")
    policies = PolicyStore(vault)
    restrictive = policies.create_revision("vault-default", "2", {"ceiling": "local-only"}, StorageClass.PORTABLE)
    policies.activate(restrictive, 1)
    with pytest.raises(InvalidArtifact, match="policy"):
        if operation == "source":
            registries.register_source("new")
        elif operation == "project":
            registries.register_project("new")
        elif operation == "workstream":
            registries.create_workstream("new", project=None, mode="parallel")
        else:
            registries.update_workstream(workstream.with_status("paused"), expected_generation=0)


def test_registry_admission_records_exact_policy_and_semantic_binding(vault):
    from mneme.core.fs import read_yaml
    from mneme.core.registries import RegistryStore

    RegistryStore(vault).register_source("source")
    metadata = read_yaml(vault.root / "sources/source.yaml")
    assert "admission_receipts" in metadata
    admission = metadata["admission_receipts"][-1]
    assert admission["allowed"] is True
    assert admission["refs"][0]["policy_id"] == "vault-default"
    assert admission["refs"][0]["revision"] == "1"
    assert len(admission["semantic_hash"]) == 64


def test_memory_missing_source_provenance_fails_closed_at_store(vault):
    from mneme.core.artifacts import ArtifactReference, ReferenceKind

    provenance = (ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, "absent-source"),)
    with pytest.raises(InvalidArtifact):
        MemoryStore(vault).submit_candidate("knowledge", {"type": "personal-global"},
            "personal", "personal-vault", "Safe.", receipt(vault, "Safe.", provenance=provenance),
            provenance=provenance)


def test_memory_explicit_source_family_survives_round_trip(vault):
    from mneme.core.artifacts import ArtifactReference, ReferenceKind
    from mneme.core.registries import RegistryStore

    RegistryStore(vault).register_source("source")
    provenance = (ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, "source", target_family="source"),)
    store = MemoryStore(vault)
    record = store.submit_candidate("knowledge", {"type": "personal-global"}, "personal", "personal-vault",
        "Safe.", receipt(vault, "Safe.", provenance=provenance), provenance=provenance)
    assert store.read(record.id).provenance[0].target_family == "source"


def test_memory_persists_legacy_source_reference_with_explicit_family(vault):
    from mneme.core.artifacts import ArtifactReference, ReferenceKind
    from mneme.core.registries import RegistryStore

    RegistryStore(vault).register_source("source")
    provenance = (ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, "source"),)
    store = MemoryStore(vault)
    record = store.submit_candidate(
        "knowledge",
        {"type": "personal-global"},
        "personal",
        "personal-vault",
        "Safe.",
        receipt(vault, "Safe.", provenance=provenance),
        provenance=provenance,
    )
    assert store.read(record.id).provenance[0].target_family == "source"


def test_restrictive_source_policy_assignment_records_control_receipt(vault):
    from mneme.core.fs import read_yaml
    from mneme.core.registries import RegistryStore

    registries = RegistryStore(vault)
    source = registries.register_source("source")
    policies = PolicyStore(vault)
    restrictive = policies.create_revision(
        "source-policy", "1", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    registries.assign_source_policy(source.id, restrictive, expected_generation=0)
    receipt = read_yaml(vault.root / "sources/source.yaml")["admission_receipts"][-1]
    assert receipt["control"] is True
    assert receipt["allowed"] is False


def test_portable_memory_rejects_newline_split_bearer_material(vault):
    body = "Authorization: bearer\nFINAL_SENTINEL_12345"
    with pytest.raises(InvalidArtifact, match="credential"):
        MemoryStore(vault).submit_candidate(
            "knowledge", {"type": "personal-global"}, "personal", "personal-vault",
            body, receipt(vault, body),
        )


def test_pre_wave_accepted_source_provenance_keeps_original_semantic_hash(vault):
    """Typing new provenance must not reinterpret an accepted v1 semantic body."""
    from hashlib import sha256
    import json

    from mneme.core.artifacts import ArtifactReference, ReferenceKind
    from mneme.core.fs import dump_yaml, read_frontmatter
    from mneme.core.registries import RegistryStore
    from mneme.core.security import PolicyArtifactRef, PolicyAuthorizer

    RegistryStore(vault).register_source("source")
    provenance = (ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, "source"),)
    store = MemoryStore(vault)
    record = store.submit_candidate(
        "knowledge", {"type": "personal-global"}, "personal", "personal-vault",
        "Existing lesson.", receipt(vault, "Existing lesson.", provenance=provenance),
        provenance=provenance, memory_id="existing-memory",
    )
    store.promote(record.id, 0, record.policy_receipt)
    path = vault.root / "memory/existing-memory.md"
    metadata, body = read_frontmatter(path)
    legacy_provenance = [{"kind": "id", "storage_class": "portable", "value": "source"}]
    legacy_hash = sha256(json.dumps({
        "kind": "knowledge", "scope": {"type": "personal-global"},
        "authority": "personal", "portability": "personal-vault",
        "body": "Existing lesson.\n", "rationale": "",
        "provenance": legacy_provenance, "supersedes": None,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    metadata["provenance"] = legacy_provenance
    metadata["semantic_hash"] = metadata["accepted_semantic_hash"] = legacy_hash
    metadata["policy_receipt"]["semantic_hash"] = legacy_hash
    for historic in metadata["receipt_history"]:
        historic["semantic_hash"] = legacy_hash
    path.write_text("---\n" + dump_yaml(metadata) + "---\n" + body, encoding="utf-8")
    before = path.read_bytes()

    restored = store.read(record.id)

    assert restored.semantic_hash == legacy_hash
    assert PolicyAuthorizer(vault).authorize_current(PolicyArtifactRef.memory(record.id), "context").allowed
    assert path.read_bytes() == before


@pytest.mark.parametrize("family", ["project", "source", "workstream"])
def test_registry_admission_receipt_requires_retained_immutable_policy(vault, family):
    from mneme.core.doctor import Doctor
    from mneme.core.git_sync import sync_preflight
    from mneme.core.registries import RegistryStore

    registries = RegistryStore(vault)
    if family == "project":
        registries.register_project("admitted")
    elif family == "source":
        registries.register_source("admitted")
    else:
        registries.create_workstream("admitted", project=None, mode="parallel")
    policies = PolicyStore(vault)
    successor = policies.create_revision("vault-default", "2", {"ceiling": "personal-vault"}, StorageClass.PORTABLE)
    policies.activate(successor, 1)
    # Corruption fixture: the active pointer is valid, but the admission basis
    # of the current Registry artifact was removed from the current tree.
    (vault.root / ".madi/policies/vault-default/1.yaml").unlink()

    assert Doctor(vault).run().status == "invalid"
    assert sync_preflight(vault).allowed is False


def test_numeric_credential_metadata_is_rejected_before_policy_write(vault):
    with pytest.raises(InvalidArtifact, match="credential"):
        PolicyStore(vault).create_revision(
            "numeric-secret", "1", {"ceiling": "personal-vault", "password": 123456789},
            StorageClass.PORTABLE,
        )
    assert not (vault.root / ".madi/policies/numeric-secret/1.yaml").exists()


@pytest.mark.parametrize("value", [
    ["FINAL_SENTINEL_12345"],
    {"primary": [123456789, "FINAL_SENTINEL_12345"]},
])
def test_nested_credential_key_values_follow_storage_boundary(vault, value):
    rule = {"ceiling": "personal-vault", "api_key": value}
    with pytest.raises(InvalidArtifact, match="credential"):
        PolicyStore(vault).create_revision("nested-secret", "1", rule, StorageClass.PORTABLE)
    assert not (vault.root / ".madi/policies/nested-secret/1.yaml").exists()

    PolicyStore(vault).create_revision("nested-secret", "1", rule, StorageClass.LOCAL_ONLY)
    assert any(
        "FINAL_SENTINEL_12345" in path.read_text(encoding="utf-8")
        for path in vault.local_root.rglob("*.yaml")
    )
    assert not (vault.root / ".madi/policies/nested-secret/1.yaml").exists()


def test_nested_credential_key_values_allow_only_whole_placeholders(vault):
    policies = PolicyStore(vault)
    for index, value in enumerate((
        ["REDACTED", "placeholder"],
        {"primary": ["redacted", "PLACEHOLDER"]},
    )):
        reference = policies.create_revision(
            f"nested-placeholder-{index}", "1",
            {"ceiling": "personal-vault", "api_key": value}, StorageClass.PORTABLE,
        )
        policies.load_rule(reference)
