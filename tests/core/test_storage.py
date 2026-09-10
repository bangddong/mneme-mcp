import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest


def _directory_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def _directory_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"directory junctions unavailable: {result.stderr or result.stdout}")


def _directory_link(link: Path, target: Path, kind: str) -> None:
    if kind == "junction":
        if os.name != "nt":
            pytest.skip("Windows junction regression")
        _directory_junction(link, target)
        return
    _directory_symlink(link, target)


@pytest.fixture
def storage(tmp_path):
    from mneme.core.storage import ArtifactReader, ArtifactStore, StorageRouter

    portable_root = tmp_path / "vault"
    local_root = tmp_path / "local"
    portable_root.mkdir()
    local_root.mkdir()
    router = StorageRouter(portable_root=portable_root, local_root=local_root)
    return router, ArtifactStore(router), ArtifactReader(router)


@pytest.mark.parametrize(
    ("family", "relative_path"),
    [
        ("registry", ".madi/policy-index.yaml"),
        ("session", "workstreams/ws-1/sessions/ses-1/000001.md"),
        ("memory", "memory/mem-1.md"),
    ],
)
def test_portable_and_local_routes_share_family_codec_and_only_roots_differ(
    storage, family, relative_path
):
    from mneme.core.artifacts import ArtifactFamily
    from mneme.core.storage import StorageClass

    router, _, _ = storage
    artifact_family = ArtifactFamily(family)
    portable = router.location(artifact_family, StorageClass.PORTABLE, relative_path)
    local = router.location(artifact_family, StorageClass.LOCAL_ONLY, relative_path)

    assert portable.storage_class is StorageClass.PORTABLE
    assert local.storage_class is StorageClass.LOCAL_ONLY
    assert portable.root == router.portable_root
    assert local.root == router.local_root / "overlays"
    assert portable.relative_path == local.relative_path == PurePosixPath(relative_path)
    assert router.codec(artifact_family, StorageClass.PORTABLE) is router.codec(
        artifact_family, StorageClass.LOCAL_ONLY
    )
    assert router.codec(artifact_family, StorageClass.PORTABLE).schema == f"madi.{family}.v1"


@pytest.mark.parametrize(
    ("family", "relative_path", "body"),
    [
        ("registry", "projects/proj-1.yaml", None),
        ("session", "workstreams/ws-1/sessions/ses-1/000001.md", "checkpoint 한글"),
        ("memory", "memory/mem-1.md", "remember 한글"),
    ],
)
def test_canonical_store_round_trips_same_document_in_both_storage_classes(
    storage, family, relative_path, body
):
    from mneme.core.artifacts import ArtifactDocument, ArtifactFamily, ReferenceManifest
    from mneme.core.storage import StorageClass

    _, store, reader = storage
    artifact_family = ArtifactFamily(family)
    document = ArtifactDocument(
        metadata={"generation": 0, "id": "artifact-1"},
        body=body,
        references=ReferenceManifest.complete(),
    )

    for storage_class in StorageClass:
        location = store.write_new(
            artifact_family,
            storage_class=storage_class,
            relative_path=relative_path,
            document=document,
        )
        loaded = reader.read(artifact_family, location=location)
        assert loaded.metadata == document.metadata
        assert loaded.body == document.body
        assert loaded.references == ReferenceManifest()


def test_canonical_writer_requires_explicit_storage_class(storage):
    from mneme.core.artifacts import ArtifactDocument, ArtifactFamily, ReferenceManifest

    _, store, _ = storage
    document = ArtifactDocument(
        metadata={"generation": 0}, references=ReferenceManifest.complete()
    )
    with pytest.raises(TypeError):
        store.write_new(
            ArtifactFamily.REGISTRY,
            relative_path="projects/proj-1.yaml",
            document=document,
        )


@pytest.mark.parametrize("kind", ["id", "path", "hash", "count", "label", "existence"])
def test_portable_write_rejects_each_typed_local_only_reference_before_creation(
    storage, kind
):
    from mneme.core.artifacts import (
        ArtifactDocument,
        ArtifactFamily,
        ArtifactReference,
        ReferenceKind,
        ReferenceManifest,
    )
    from mneme.core.errors import PortabilityViolation
    from mneme.core.storage import StorageClass

    router, store, _ = storage
    relative_path = "memory/portable.md"
    reference_value = (
        1 if kind == "count" else True if kind == "existence" else "confidential-value"
    )
    document = ArtifactDocument(
        metadata={"generation": 0, "id": "portable"},
        body="sanitized body",
        references=ReferenceManifest.complete(
            body=(
                    ArtifactReference(
                        kind=ReferenceKind(kind),
                        storage_class=StorageClass.LOCAL_ONLY,
                        value=reference_value,
                ),
            )
        ),
    )

    with pytest.raises(PortabilityViolation):
        store.write_new(
            ArtifactFamily.MEMORY,
            storage_class=StorageClass.PORTABLE,
            relative_path=relative_path,
            document=document,
        )
    assert not router.location(
        ArtifactFamily.MEMORY, StorageClass.PORTABLE, relative_path
    ).path.exists()


def test_portable_write_fails_closed_without_complete_reference_manifest(storage):
    from mneme.core.artifacts import ArtifactDocument, ArtifactFamily
    from mneme.core.errors import PortabilityViolation
    from mneme.core.storage import StorageClass

    router, store, _ = storage
    relative_path = "memory/unclassified.md"
    with pytest.raises(PortabilityViolation):
        store.write_new(
            ArtifactFamily.MEMORY,
            storage_class=StorageClass.PORTABLE,
            relative_path=relative_path,
            document=ArtifactDocument(metadata={"generation": 0}, body="body"),
        )
    assert not router.location(
        ArtifactFamily.MEMORY, StorageClass.PORTABLE, relative_path
    ).path.exists()


@pytest.mark.parametrize(
    ("kind", "storage_class", "value"),
    [
        ("raw-id", "enum", "secret"),
        ("enum", "raw-local", "secret"),
        ("enum", "enum", ""),
    ],
)
def test_malformed_reference_types_fail_before_any_artifact_path_is_created(
    storage, kind, storage_class, value
):
    from mneme.core.artifacts import ArtifactReference, ReferenceKind
    from mneme.core.errors import InvalidArtifact
    from mneme.core.storage import StorageClass

    router, _, _ = storage
    with pytest.raises(InvalidArtifact):
        ArtifactReference(
            ReferenceKind.ID if kind == "enum" else kind,
            StorageClass.LOCAL_ONLY if storage_class == "enum" else storage_class,
            value,
        )
    assert not (router.portable_root / "memory").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metadata": [], "is_complete": True},
        {"metadata": ("not-a-reference",), "is_complete": True},
        {"is_complete": "yes"},
    ],
)
def test_malformed_reference_manifest_fails_before_path_creation(storage, kwargs):
    from mneme.core.artifacts import ReferenceManifest
    from mneme.core.errors import InvalidArtifact

    router, _, _ = storage
    with pytest.raises(InvalidArtifact):
        ReferenceManifest(**kwargs)
    assert not (router.portable_root / "memory").exists()


@pytest.mark.parametrize(
    ("kind", "value"),
    [("count", True), ("count", -1), ("existence", "yes"), ("path", 42)],
)
def test_reference_values_are_validated_by_disclosure_kind(storage, kind, value):
    from mneme.core.artifacts import ArtifactReference, ReferenceKind
    from mneme.core.errors import InvalidArtifact
    from mneme.core.storage import StorageClass

    router, _, _ = storage
    with pytest.raises(InvalidArtifact):
        ArtifactReference(ReferenceKind(kind), StorageClass.LOCAL_ONLY, value)
    assert not (router.portable_root / "memory").exists()


def test_local_artifact_may_reference_portable_artifact(storage):
    from mneme.core.artifacts import (
        ArtifactDocument,
        ArtifactFamily,
        ArtifactReference,
        ReferenceKind,
        ReferenceManifest,
    )
    from mneme.core.storage import StorageClass

    _, store, reader = storage
    document = ArtifactDocument(
        metadata={"generation": 0, "id": "local-memory"},
        body="private consequence",
        references=ReferenceManifest.complete(
            metadata=(
                ArtifactReference(
                    kind=ReferenceKind.ID,
                    storage_class=StorageClass.PORTABLE,
                    value="portable-memory",
                ),
            )
        ),
    )
    location = store.write_new(
        ArtifactFamily.MEMORY,
        storage_class=StorageClass.LOCAL_ONLY,
        relative_path="memory/local-memory.md",
        document=document,
    )
    loaded = reader.read(ArtifactFamily.MEMORY, location=location)
    assert loaded.metadata == document.metadata
    assert loaded.body == document.body


def test_decoded_local_artifact_requires_fresh_manifest_before_portable_rewrite(
    storage,
):
    from mneme.core.artifacts import (
        ArtifactDocument,
        ArtifactFamily,
        ArtifactReference,
        ReferenceKind,
        ReferenceManifest,
    )
    from mneme.core.errors import PortabilityViolation
    from mneme.core.storage import StorageClass

    router, store, reader = storage
    local_document = ArtifactDocument(
        metadata={"generation": 0, "id": "local-memory"},
        body="local body naming confidential evidence",
        references=ReferenceManifest.complete(
            body=(
                ArtifactReference(
                    ReferenceKind.ID,
                    StorageClass.LOCAL_ONLY,
                    "local-evidence-1",
                ),
            )
        ),
    )
    local_location = store.write_new(
        ArtifactFamily.MEMORY,
        storage_class=StorageClass.LOCAL_ONLY,
        relative_path="memory/local-memory.md",
        document=local_document,
    )

    decoded = reader.read(ArtifactFamily.MEMORY, location=local_location)
    assert decoded.references == ReferenceManifest()
    with pytest.raises(PortabilityViolation):
        store.write_new(
            ArtifactFamily.MEMORY,
            storage_class=StorageClass.PORTABLE,
            relative_path="memory/copied-local-memory.md",
            document=decoded,
        )
    assert not (router.portable_root / "memory/copied-local-memory.md").exists()


@pytest.mark.parametrize(
    ("family", "relative_path", "body"),
    [
        ("registry", "projects/proj-1.yaml", None),
        ("memory", "memory/mem-1.md", "candidate v1"),
    ],
)
def test_canonical_cas_updates_exact_generation_and_rejects_stale_write(
    storage, family, relative_path, body
):
    from mneme.core.artifacts import ArtifactDocument, ArtifactFamily, ReferenceManifest
    from mneme.core.errors import ConcurrentWrite
    from mneme.core.storage import StorageClass

    _, store, reader = storage
    artifact_family = ArtifactFamily(family)
    initial = ArtifactDocument(
        metadata={"generation": 0, "id": "artifact-1"},
        body=body,
        references=ReferenceManifest.complete(),
    )
    store.write_new(
        artifact_family,
        storage_class=StorageClass.PORTABLE,
        relative_path=relative_path,
        document=initial,
    )
    updated = ArtifactDocument(
        metadata={"generation": 1, "id": "artifact-1"},
        body=None if body is None else "candidate v2",
        references=ReferenceManifest.complete(),
    )

    location = store.write_cas(
        artifact_family,
        storage_class=StorageClass.PORTABLE,
        relative_path=relative_path,
        document=updated,
        expected_generation=0,
    )
    loaded = reader.read(artifact_family, location=location)
    assert loaded.metadata == updated.metadata
    assert loaded.body == updated.body
    assert loaded.references == ReferenceManifest()
    lock_files = list((store.router.local_root / "locks").glob("*.lock"))
    assert len(lock_files) == 1
    assert not list(store.router.portable_root.rglob("*.lock"))

    stale = ArtifactDocument(
        metadata={"generation": 1, "id": "artifact-1"},
        body=None if body is None else "stale candidate",
        references=ReferenceManifest.complete(),
    )
    with pytest.raises(ConcurrentWrite):
        store.write_cas(
            artifact_family,
            storage_class=StorageClass.PORTABLE,
            relative_path=relative_path,
            document=stale,
            expected_generation=0,
        )
    loaded = reader.read(artifact_family, location=location)
    assert loaded.metadata == updated.metadata
    assert loaded.body == updated.body
    assert loaded.references == ReferenceManifest()


def test_canonical_cas_rejects_local_reference_before_mutating_portable_file(storage):
    from mneme.core.artifacts import (
        ArtifactDocument,
        ArtifactFamily,
        ArtifactReference,
        ReferenceKind,
        ReferenceManifest,
    )
    from mneme.core.errors import PortabilityViolation
    from mneme.core.storage import StorageClass

    _, store, reader = storage
    relative_path = "memory/mem-1.md"
    initial = ArtifactDocument(
        metadata={"generation": 0, "id": "mem-1"},
        body="portable body",
        references=ReferenceManifest.complete(),
    )
    location = store.write_new(
        ArtifactFamily.MEMORY,
        storage_class=StorageClass.PORTABLE,
        relative_path=relative_path,
        document=initial,
    )
    leaking = ArtifactDocument(
        metadata={"generation": 1, "id": "mem-1"},
        body="body naming local evidence",
        references=ReferenceManifest.complete(
            body=(
                ArtifactReference(
                    kind=ReferenceKind.EXISTENCE,
                    storage_class=StorageClass.LOCAL_ONLY,
                    value=True,
                ),
            )
        ),
    )

    with pytest.raises(PortabilityViolation):
        store.write_cas(
            ArtifactFamily.MEMORY,
            storage_class=StorageClass.PORTABLE,
            relative_path=relative_path,
            document=leaking,
            expected_generation=0,
        )
    loaded = reader.read(ArtifactFamily.MEMORY, location=location)
    assert loaded.metadata == initial.metadata
    assert loaded.body == initial.body
    assert loaded.references == ReferenceManifest()


@pytest.mark.parametrize(
    ("family", "relative_path"),
    [
        ("registry", "memory/not-registry.yaml"),
        ("session", "workstreams/ws/sessions/ses/../../escape.md"),
        ("memory", "../memory/escape.md"),
        ("memory", "memory/not-markdown.yaml"),
    ],
)
def test_router_rejects_paths_outside_the_selected_canonical_family(
    storage, family, relative_path
):
    from mneme.core.artifacts import ArtifactFamily
    from mneme.core.errors import UnsafePath
    from mneme.core.storage import StorageClass

    router, _, _ = storage
    with pytest.raises(UnsafePath):
        router.location(ArtifactFamily(family), StorageClass.PORTABLE, relative_path)


def test_store_rejects_existing_parent_symlink_escape(storage, tmp_path):
    from mneme.core.artifacts import ArtifactDocument, ArtifactFamily, ReferenceManifest
    from mneme.core.errors import UnsafePath
    from mneme.core.storage import StorageClass

    router, store, _ = storage
    outside = tmp_path / "outside"
    outside.mkdir()
    projects = router.portable_root / "projects"
    try:
        projects.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(UnsafePath):
        store.write_new(
            ArtifactFamily.REGISTRY,
            storage_class=StorageClass.PORTABLE,
            relative_path="projects/proj-1.yaml",
            document=ArtifactDocument(
                metadata={"generation": 0}, references=ReferenceManifest.complete()
            ),
        )
    assert not (outside / "proj-1.yaml").exists()


def test_store_rejects_symlinked_ancestor_before_creating_nested_parent(
    storage, tmp_path
):
    """Catches parent mkdir creating canonical descendants through an alias."""
    from mneme.core.artifacts import ArtifactDocument, ArtifactFamily, ReferenceManifest
    from mneme.core.errors import UnsafePath
    from mneme.core.storage import StorageClass

    router, store, _ = storage
    target = router.portable_root / "workstreams-target"
    target.mkdir()
    workstreams = router.portable_root / "workstreams"
    try:
        workstreams.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(UnsafePath):
        store.write_new(
            ArtifactFamily.SESSION,
            storage_class=StorageClass.PORTABLE,
            relative_path="workstreams/ws-1/sessions/ses-1/000001.md",
            document=ArtifactDocument(
                metadata={"generation": 0},
                body="checkpoint",
                references=ReferenceManifest.complete(),
            ),
        )

    assert not (target / "ws-1").exists()


def test_view_store_only_targets_named_generated_views_under_local_root(tmp_path):
    from mneme.core.errors import UnsafePath
    from mneme.core.storage import StorageRouter, ViewStore

    portable_root = tmp_path / "vault"
    local_root = tmp_path / "local"
    router = StorageRouter(portable_root, local_root)
    for forbidden_root in (
        portable_root / "memory",
        portable_root / "projects",
        portable_root / "workstreams",
        local_root,
    ):
        with pytest.raises(TypeError):
            ViewStore(forbidden_root)

    views = router.view_store()
    current = views.write("CURRENT.md", "generated\r\nprojection")
    assert current == local_root / "views" / "CURRENT.md"
    assert current.read_text(encoding="utf-8") == "generated\nprojection\n"

    for path in (
        "../overlays/memory/mem-1.md",
        "../../vault/memory/mem-1.md",
        "nested/CURRENT.md",
        "memory.md",
    ):
        with pytest.raises(UnsafePath):
            views.write(path, "forbidden")


def test_view_store_never_loads_a_view_from_a_denied_policy_decision(tmp_path):
    """Catches an opaque cache hash being usable after the common gate has denied it."""
    from mneme.core.security import PolicyDecision
    from mneme.core.storage import StorageRouter

    views = StorageRouter(tmp_path / "vault", tmp_path / "local").view_store()
    allowed = PolicyDecision(True, (), "a" * 64)
    denied = PolicyDecision(False, ("policy-current-denied",))
    views.write("CURRENT.md", "sensitive generated current", authorization=allowed)

    assert views.load("CURRENT.md", authorization=denied) is None


@pytest.mark.parametrize("aliased_root", ["portable", "local"])
def test_router_rejects_supplied_root_symlink_before_laundering_it(
    tmp_path, aliased_root
):
    """Catches resolving a caller-supplied root before inspecting its link entry."""
    from mneme.core.errors import UnsafePath
    from mneme.core.storage import StorageRouter

    real_portable = tmp_path / "real-portable"
    real_local = tmp_path / "real-local"
    real_portable.mkdir()
    real_local.mkdir()
    portable = real_portable
    local = real_local
    alias = tmp_path / f"{aliased_root}-alias"
    _directory_symlink(alias, real_portable if aliased_root == "portable" else real_local)
    if aliased_root == "portable":
        portable = alias
    else:
        local = alias

    with pytest.raises(UnsafePath, match="link|reparse"):
        StorageRouter(portable, local)

    assert not (real_local / "overlays").exists()
    assert not (real_portable / "memory").exists()


def test_artifact_location_rejects_a_supplied_root_symlink(tmp_path):
    """Catches ArtifactLocation erasing an unsafe raw boundary with resolve()."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import UnsafePath
    from mneme.core.storage import ArtifactLocation

    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    _directory_symlink(alias, target)

    with pytest.raises(UnsafePath, match="link|reparse"):
        ArtifactLocation(
            StorageClass.PORTABLE,
            alias,
            PurePosixPath("memory/no-leak.md"),
        )

    assert not (target / "memory").exists()


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_local_memory_rejects_linked_overlays_to_portable_vault(
    tmp_path, link_kind
):
    """Catches LOCAL_ONLY MemoryStore writes crossing into portable Git."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import UnsafePath
    from mneme.core.memories import MemoryStore, memory_semantic_hash
    from mneme.core.policy import Portability, evaluate_portability
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    overlays = vault.local_root / "overlays"
    overlays.rmdir()
    _directory_link(overlays, vault.root, link_kind)
    leaked = vault.root / "memory/junction-leak.md"
    try:
        receipt = evaluate_portability(
            Portability.LOCAL_ONLY,
            (),
            memory_semantic_hash(
                body="confidential local memory",
                portability=Portability.LOCAL_ONLY,
            ),
        )
        with pytest.raises(UnsafePath, match="link|reparse"):
            MemoryStore(vault, StorageClass.LOCAL_ONLY).submit_candidate(
                "knowledge",
                {"type": "personal-global"},
                "personal",
                Portability.LOCAL_ONLY,
                "confidential local memory",
                receipt,
                memory_id="junction-leak",
            )
        assert not leaked.exists()
    finally:
        overlays.rmdir()


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_view_store_rejects_linked_views_without_external_creation(
    tmp_path, link_kind
):
    """Catches generated view publication through an existing local junction."""
    from mneme.core.errors import UnsafePath
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    views_store = vault.views
    views = vault.local_root / "views"
    external = tmp_path / "external-views"
    views.rmdir()
    external.mkdir()
    _directory_link(views, external, link_kind)
    try:
        with pytest.raises(UnsafePath, match="link|reparse"):
            views_store.write("CURRENT.md", "must stay local")
        assert not (external / "CURRENT.md").exists()
    finally:
        views.rmdir()
