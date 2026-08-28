from __future__ import annotations

from dataclasses import replace

import pytest


@pytest.fixture
def vault(tmp_path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


def test_workstream_update_requires_expected_generation(vault):
    """Catches a stale writer silently overwriting a lifecycle transition."""
    from mneme.core.registries import RegistryConflict, RegistryStore

    store = RegistryStore(vault)
    ws = store.create_workstream("ws-1", project=None, mode="single")

    changed = store.update_workstream(ws.with_status("paused"), expected_generation=0)

    assert changed.generation == 1
    assert changed.status == "paused"
    with pytest.raises(RegistryConflict):
        store.update_workstream(changed.with_status("active"), expected_generation=0)


def test_project_and_source_registries_share_codec_but_portable_rejects_local_data(
    vault, tmp_path
):
    """Catches a portable registry leaking a machine path or credential-like locator."""
    from mneme.core.artifacts import ArtifactFamily, StorageClass
    from mneme.core.errors import InvalidArtifact
    from mneme.core.registries import RegistryStore

    portable = RegistryStore(vault, storage_class=StorageClass.PORTABLE)
    local = RegistryStore(vault, storage_class=StorageClass.LOCAL_ONLY)
    external_checkout = (tmp_path / "checkout").resolve()

    with pytest.raises(InvalidArtifact):
        portable.register_project("project-path", locator=str(external_checkout))
    with pytest.raises(InvalidArtifact):
        portable.register_source(
            "source-secret", locator="https://token:secret@example.test/repo"
        )

    local_project = local.register_project(
        "project-private", locator=str(external_checkout)
    )
    portable_project = portable.register_project(
        "project-safe", locator="https://example.test/owner/repo"
    )

    assert local_project.id == "project-private"
    assert not (vault.root / "projects" / "project-private.yaml").exists()
    assert vault.router.codec(ArtifactFamily.REGISTRY, StorageClass.PORTABLE) is vault.router.codec(
        ArtifactFamily.REGISTRY, StorageClass.LOCAL_ONLY
    )
    assert vault.reader.read(
        ArtifactFamily.REGISTRY,
        storage_class=StorageClass.PORTABLE,
        relative_path="projects/project-safe.yaml",
    ).metadata["schema"] == vault.reader.read(
        ArtifactFamily.REGISTRY,
        storage_class=StorageClass.LOCAL_ONLY,
        relative_path="projects/project-private.yaml",
    ).metadata["schema"] == "madi.project-registry.v1"
    assert portable_project.id == "project-safe"


def test_source_binding_is_local_and_accepts_an_external_absolute_checkout(vault, tmp_path):
    """Catches source-path bindings being written into the portable Vault."""
    from mneme.core.registries import RegistryStore

    store = RegistryStore(vault)
    store.register_source("source-1", locator="docs/readme.md")
    checkout = (tmp_path / "outside-checkout").resolve()

    binding = store.bind_source("source-1", checkout)

    assert binding.source_id == "source-1"
    assert binding.path == checkout
    binding_file = vault.local_root / "bindings" / "sources.yaml"
    assert binding_file.is_file()
    assert str(checkout) in binding_file.read_text(encoding="utf-8")
    assert str(checkout) not in (vault.root / "sources" / "source-1.yaml").read_text(
        encoding="utf-8"
    )


def test_source_binding_rejects_a_relative_or_vault_internal_path(vault, tmp_path):
    """Catches a local binding resolving an ambiguous or Vault-contained checkout."""
    from mneme.core.errors import InvalidArtifact, UnsafePath
    from mneme.core.registries import RegistryStore

    store = RegistryStore(vault)
    with pytest.raises(InvalidArtifact):
        store.bind_source("source-1", "relative-checkout")
    with pytest.raises(UnsafePath):
        store.bind_source("source-1", vault.root / "projects")


def test_policy_assignment_uses_immutable_ref_and_cas_updates_only_subject(vault):
    """Catches policy assignment changing a different registry or crossing storage."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import InvalidArtifact
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryConflict, RegistryStore

    policies = PolicyStore(vault)
    portable_ref = policies.create_revision(
        "project-policy", "1", {"ceiling": "personal-vault"}, StorageClass.PORTABLE
    )
    local_ref = policies.create_revision(
        "private-policy", "1", {"ceiling": "local-only"}, StorageClass.LOCAL_ONLY
    )
    store = RegistryStore(vault)
    project = store.register_project("project-1")
    source = store.register_source("source-1", project="project-1")

    assigned_project = store.assign_project_policy(
        project.id, portable_ref, expected_generation=0
    )
    assigned_source = store.assign_source_policy(
        source.id, portable_ref, expected_generation=0
    )

    assert assigned_project.generation == assigned_source.generation == 1
    assert assigned_project.policy_ref == assigned_source.policy_ref == portable_ref
    with pytest.raises(RegistryConflict):
        store.assign_project_policy(project.id, portable_ref, expected_generation=0)
    with pytest.raises(InvalidArtifact):
        store.assign_source_policy(source.id, local_ref, expected_generation=1)
    assert store.load_project(project.id).policy_ref == portable_ref


def test_disjoint_head_addition_reloads_and_retries_from_observed_base(vault):
    """Catches a stale disjoint writer dropping the first writer's valid head."""
    from mneme.core.registries import HeadRef, RegistryStore

    first = RegistryStore(vault)
    second = RegistryStore(vault)
    observed_base = first.create_workstream("ws-1", project=None, mode="parallel")
    valid = lambda head: head.revision == "000001"

    first.add_active_head(
        "ws-1", HeadRef("session-a", "000001"), observed_base, valid
    )
    merged = second.add_active_head(
        "ws-1", HeadRef("session-b", "000001"), observed_base, valid
    )

    assert merged.generation == 2
    assert set(merged.active_heads) == {
        HeadRef("session-a", "000001"),
        HeadRef("session-b", "000001"),
    }


def test_head_retry_rejects_a_concurrent_workstream_policy_change(vault):
    """Catches the structural retry treating a policy change as an add-only merge."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import HeadRef, RegistryConflict, RegistryStore

    store = RegistryStore(vault)
    observed_base = store.create_workstream("ws-1", project=None, mode="parallel")
    policy = PolicyStore(vault).create_revision(
        "workstream-policy", "1", {"ceiling": "personal-vault"}, StorageClass.PORTABLE
    )
    changed = observed_base.with_policy_refs((policy,))
    store.update_workstream(changed, expected_generation=0)

    with pytest.raises(RegistryConflict):
        store.add_active_head(
            "ws-1", HeadRef("session-a", "000001"), observed_base, lambda _head: True
        )


def test_head_retry_rejects_an_invalid_referenced_revision(vault):
    """Catches a head being attached without an immutable revision validation."""
    from mneme.core.registries import HeadRef, RegistryConflict, RegistryStore

    store = RegistryStore(vault)
    observed_base = store.create_workstream("ws-1", project=None, mode="parallel")

    with pytest.raises(RegistryConflict) as error:
        store.add_active_head(
            "ws-1", HeadRef("session-a", "000001"), observed_base, lambda _head: False
        )

    assert error.value.code == "invalid-head"
    assert store.load_workstream("ws-1") == observed_base


def test_head_retry_rejects_same_session_revision_change(vault):
    """Catches two writers attempting distinct revisions of one session lineage."""
    from mneme.core.registries import HeadRef, RegistryConflict, RegistryStore

    store = RegistryStore(vault)
    observed_base = store.create_workstream("ws-1", project=None, mode="parallel")
    store.add_active_head(
        "ws-1", HeadRef("session-a", "000001"), observed_base, lambda _head: True
    )

    with pytest.raises(RegistryConflict):
        store.add_active_head(
            "ws-1", HeadRef("session-a", "000002"), observed_base, lambda _head: True
        )


def test_head_retry_rejects_lifecycle_preferred_and_removal_changes(vault):
    """Catches structural retry crossing a non-head decision or a head removal."""
    from mneme.core.registries import HeadRef, RegistryConflict, RegistryStore

    store = RegistryStore(vault)
    lifecycle_base = store.create_workstream("ws-lifecycle", project=None, mode="parallel")
    store.update_workstream(lifecycle_base.with_status("paused"), expected_generation=0)
    with pytest.raises(RegistryConflict):
        store.add_active_head(
            "ws-lifecycle", HeadRef("session-b", "000001"), lifecycle_base, lambda _head: True
        )

    observed = store.create_workstream("ws-heads", project=None, mode="parallel")
    with_head = store.add_active_head(
        "ws-heads", HeadRef("session-a", "000001"), observed, lambda _head: True
    )
    store.update_workstream(
        replace(with_head, mode="preferred", preferred_head=with_head.active_heads[0]),
        expected_generation=with_head.generation,
    )
    with pytest.raises(RegistryConflict):
        store.add_active_head(
            "ws-heads", HeadRef("session-b", "000001"), with_head, lambda _head: True
        )

    removal_base = store.create_workstream("ws-removal", project=None, mode="parallel")
    with_head = store.add_active_head(
        "ws-removal", HeadRef("session-a", "000001"), removal_base, lambda _head: True
    )
    store.update_workstream(replace(with_head, active_heads=()), expected_generation=1)
    with pytest.raises(RegistryConflict):
        store.add_active_head(
            "ws-removal", HeadRef("session-b", "000001"), with_head, lambda _head: True
        )


def test_head_retry_stops_after_three_cas_conflicts(vault, monkeypatch):
    """Catches unbounded retry under repeated concurrent registry writes."""
    from mneme.core.registries import HeadRef, RegistryConflict, RegistryStore

    store = RegistryStore(vault)
    observed_base = store.create_workstream("ws-1", project=None, mode="parallel")
    attempts = 0

    def conflict(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RegistryConflict("injected CAS conflict")

    monkeypatch.setattr(store, "update_workstream", conflict)
    with pytest.raises(RegistryConflict, match="exhausted three CAS attempts"):
        store.add_active_head(
            "ws-1", HeadRef("session-a", "000001"), observed_base, lambda _head: True
        )
    assert attempts == 3
