from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import pytest


def _rule(*, ceiling: str = "personal-vault") -> dict[str, str]:
    return {"ceiling": ceiling, "purpose": "test-policy"}


def _store(tmp_path):
    from mneme.core.policy import PolicyStore
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "owner / opaque")
    return vault, PolicyStore(vault)


def test_create_activate_and_preserve_immutable_policy_revisions(tmp_path):
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import ArtifactExists, ConcurrentWrite

    vault, store = _store(tmp_path)
    first = store.create_revision(
        "vault-policy", "1", _rule(), storage_class=StorageClass.PORTABLE
    )
    index = store.activate(first, expected_generation=1)
    second = store.create_revision(
        "vault-policy", "2", _rule(ceiling="local-only"), StorageClass.PORTABLE
    )

    with pytest.raises(ConcurrentWrite):
        store.activate(second, expected_generation=1)
    assert store.load_active("vault-policy", StorageClass.PORTABLE) == first

    advanced = store.activate(second, expected_generation=index.generation)
    assert advanced.generation == 3
    assert store.load_active("vault-policy", StorageClass.PORTABLE) == second
    assert (vault.root / ".madi/policies/vault-policy/1.yaml").is_file()

    with pytest.raises(ArtifactExists):
        store.create_revision("vault-policy", "1", _rule(), StorageClass.PORTABLE)
    with pytest.raises(ArtifactExists):
        store.create_revision(
            "vault-policy", "1", _rule(ceiling="shareable"), StorageClass.PORTABLE
        )


def test_local_policy_lifecycle_uses_the_identical_schema_under_overlay(tmp_path):
    from mneme.core.artifacts import StorageClass
    from mneme.core.fs import read_yaml

    vault, store = _store(tmp_path)
    local = store.create_revision(
        "private-policy", "1", _rule(ceiling="local-only"), StorageClass.LOCAL_ONLY
    )
    index = store.activate(local, expected_generation=0)

    assert index.generation == 1
    assert store.load_active("private-policy", StorageClass.LOCAL_ONLY) == local
    portable_revision = vault.root / ".madi/policies/private-policy/1.yaml"
    local_revision = vault.local_root / "overlays/.madi/policies/private-policy/1.yaml"
    assert not portable_revision.exists()
    assert read_yaml(local_revision) == {
        "policy_id": "private-policy",
        "revision": "1",
        "rule": _rule(ceiling="local-only"),
        "schema": "madi.policy-revision.v1",
    }
    assert read_yaml(vault.local_root / "overlays/.madi/policy-index.yaml") == {
        "generation": 1,
        "policies": {
            "private-policy": {"digest": local.digest, "revision": "1"}
        },
        "schema": "madi.policy-index.v1",
    }


def test_concurrent_activation_has_one_winner_and_never_overwrites_a_pointer(tmp_path):
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import ConcurrentWrite

    _, store = _store(tmp_path)
    revision = store.create_revision("parallel", "1", _rule(), StorageClass.PORTABLE)

    def activate_once():
        try:
            return store.activate(revision, expected_generation=1)
        except ConcurrentWrite:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: activate_once(), range(2)))

    assert sum(result is not None for result in results) == 1
    assert store.load_active("parallel", StorageClass.PORTABLE) == revision


def test_policy_index_is_an_immutable_snapshot_not_a_mutable_activation_bypass(tmp_path):
    from mneme.core.artifacts import StorageClass

    _, store = _store(tmp_path)
    revision = store.create_revision("immutable-index", "1", _rule(), StorageClass.PORTABLE)
    index = store.activate(revision, expected_generation=1)

    with pytest.raises(TypeError):
        index.policies["other"] = revision


def test_policy_store_rejects_tampered_revision_or_cross_storage_reference(tmp_path):
    from mneme.core.artifacts import (
        ArtifactDocument,
        ArtifactFamily,
        ArtifactReference,
        ReferenceKind,
        ReferenceManifest,
        StorageClass,
    )
    from mneme.core.errors import InvalidArtifact, PortabilityViolation

    vault, store = _store(tmp_path)
    local = store.create_revision("secret-policy", "1", _rule(), StorageClass.LOCAL_ONLY)
    store.activate(local, expected_generation=0)

    with pytest.raises(PortabilityViolation):
        vault.artifacts.write_cas(
            ArtifactFamily.REGISTRY,
            storage_class=StorageClass.PORTABLE,
            relative_path=".madi/policy-index.yaml",
            document=ArtifactDocument(
                metadata={
                    "generation": 2,
                    "policies": {
                        "secret-policy": {"revision": "1", "digest": local.digest}
                    },
                    "schema": "madi.policy-index.v1",
                },
                references=ReferenceManifest.complete(
                    metadata=(
                        ArtifactReference(
                            ReferenceKind.ID, StorageClass.LOCAL_ONLY, "secret-policy"
                        ),
                    )
                ),
            ),
            expected_generation=1,
        )

    revision_path = vault.local_root / "overlays/.madi/policies/secret-policy/1.yaml"
    revision_path.write_text("schema: madi.policy-revision.v1\n", encoding="utf-8")
    with pytest.raises(InvalidArtifact):
        store.load_active("secret-policy", StorageClass.LOCAL_ONLY)


def test_bootstrap_is_idempotent_only_for_matching_content_and_pointer(tmp_path):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.fs import read_yaml
    from mneme.core.policy import PolicyStore
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    store = PolicyStore(vault)
    before_index = read_yaml(vault.root / ".madi/policy-index.yaml")
    before_revision = next((vault.root / ".madi/policies").rglob("*.yaml"))
    before_bytes = before_revision.read_bytes()

    assert store.bootstrap_default() == store.bootstrap_default()
    assert read_yaml(vault.root / ".madi/policy-index.yaml") == before_index
    assert before_revision.read_bytes() == before_bytes

    before_revision.write_bytes(b"schema: madi.policy-revision.v1\n")
    with pytest.raises(InvalidArtifact):
        store.bootstrap_default()


def test_failed_bootstrap_leaves_no_partial_vault_initialization(tmp_path, monkeypatch):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.policy import PolicyStore
    from mneme.core.vault import Vault

    def fail_bootstrap(self):
        raise InvalidArtifact("injected bootstrap failure")

    monkeypatch.setattr(PolicyStore, "bootstrap_default", fail_bootstrap)
    vault_root = tmp_path / "vault"
    state_home = tmp_path / "state"

    with pytest.raises(InvalidArtifact, match="injected bootstrap failure"):
        Vault.initialize(vault_root, state_home, "person-01")
    assert not vault_root.exists()
    assert not (state_home / "vaults").exists() or not list(
        (state_home / "vaults").iterdir()
    )


@pytest.mark.parametrize("value", ["../escape", "a/b", "a\\b", "with space", ""])
def test_policy_store_rejects_unsafe_policy_path_tokens_before_writing(tmp_path, value):
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import InvalidArtifact

    vault, store = _store(tmp_path)
    before = list((vault.root / ".madi/policies").rglob("*.yaml"))
    with pytest.raises(InvalidArtifact):
        store.create_revision(value, "1", _rule(), StorageClass.PORTABLE)
    with pytest.raises(InvalidArtifact):
        store.create_revision("safe", value, _rule(), StorageClass.PORTABLE)
    assert list((vault.root / ".madi/policies").rglob("*.yaml")) == before


@pytest.mark.parametrize("rule", [{"ceiling": object()}, {1: "not-a-string-key"}])
def test_policy_store_rejects_noncanonical_rule_data_before_writing(tmp_path, rule):
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import InvalidArtifact

    vault, store = _store(tmp_path)
    before = list((vault.root / ".madi/policies").rglob("*.yaml"))

    with pytest.raises(InvalidArtifact):
        store.create_revision("safe", "1", rule, StorageClass.PORTABLE)
    assert list((vault.root / ".madi/policies").rglob("*.yaml")) == before
