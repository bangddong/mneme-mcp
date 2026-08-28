from pathlib import Path

import pytest


PORTABLE_DIRECTORIES = {
    ".madi",
    ".madi/policies",
    "projects",
    "sources",
    "workstreams",
    "memory",
}
PORTABLE_FILES = {
    ".madi/schema-version",
    ".madi/vault.yaml",
    ".madi/policy-index.yaml",
}
LOCAL_DIRECTORIES = {
    "bindings",
    "overlays",
    "evidence",
    "pending",
    "views",
    "index",
    "cache",
    "locks",
    "logs",
}


def _relative_entries(root: Path) -> tuple[set[str], set[str]]:
    directories = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir()
    }
    files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    return directories, files


def test_initialize_creates_exact_portable_and_local_layout(tmp_path):
    from mneme.core.fs import read_yaml
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")

    assert (vault.root / ".madi/schema-version").read_text(encoding="utf-8") == "1\n"
    assert vault.local_root == tmp_path / "state" / "vaults" / vault.id
    assert _relative_entries(vault.root) == (PORTABLE_DIRECTORIES, PORTABLE_FILES)
    assert _relative_entries(vault.local_root) == (LOCAL_DIRECTORIES, set())
    assert read_yaml(vault.root / ".madi/vault.yaml") == {
        "generation": 0,
        "id": vault.id,
        "owner": {"id": "person-01", "type": "person"},
        "schema_version": 1,
    }
    assert read_yaml(vault.root / ".madi/policy-index.yaml") == {
        "generation": 0,
        "policies": {},
    }
    assert not (vault.root / "CURRENT.md").exists()
    assert not (vault.root / "PROFILE.md").exists()
    assert not (vault.root / "state.db").exists()
    assert not (vault.local_root / "index/state.db").exists()


def test_open_reads_identity_from_complete_portable_and_local_layout(tmp_path):
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    opened = Vault.open(initialized.root, initialized.state_home)

    assert opened.id == initialized.id
    assert opened.owner == {"id": "person-01", "type": "person"}
    assert opened.local_root == initialized.local_root


def test_open_rejects_missing_local_state_instead_of_creating_it(tmp_path):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state-a", "person-01")
    missing_state_home = tmp_path / "state-b"

    with pytest.raises(InvalidArtifact):
        Vault.open(initialized.root, missing_state_home)
    assert not missing_state_home.exists()


@pytest.mark.parametrize(
    "relative_path",
    [
        ".madi/policy-index.yaml",
        ".madi/policies",
        "projects",
        "sources",
        "workstreams",
        "memory",
    ],
)
def test_open_rejects_each_missing_required_portable_layout_entry(
    tmp_path, relative_path
):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    missing = initialized.root / relative_path
    missing.unlink() if missing.is_file() else missing.rmdir()

    with pytest.raises(InvalidArtifact):
        Vault.open(initialized.root, initialized.state_home)


def test_open_rejects_required_portable_parent_symlink_escape(tmp_path):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    madi = initialized.root / ".madi"
    outside = tmp_path / "outside-madi"
    madi.rename(outside)
    try:
        madi.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(InvalidArtifact):
        Vault.open(initialized.root, initialized.state_home)


@pytest.mark.parametrize("relative_path", sorted(LOCAL_DIRECTORIES))
def test_open_rejects_each_missing_required_local_layout_entry(tmp_path, relative_path):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    (initialized.local_root / relative_path).rmdir()

    with pytest.raises(InvalidArtifact):
        Vault.open(initialized.root, initialized.state_home)


def test_failed_local_initialization_leaves_no_openable_or_partial_vault(
    tmp_path, monkeypatch
):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    vault_root = tmp_path / "vault"
    state_home = tmp_path / "state"
    state_home.mkdir()
    sentinel = state_home / "unrelated.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    real_mkdir = Path.mkdir

    def fail_on_local_evidence(path, *args, **kwargs):
        if path.name == "evidence" and "vaults" in path.parts:
            raise OSError("injected local layout failure")
        return real_mkdir(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", fail_on_local_evidence)
        with pytest.raises(OSError, match="injected local layout failure"):
            Vault.initialize(vault_root, state_home, "person-01")

    assert not vault_root.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep me"
    vaults_root = state_home / "vaults"
    assert not vaults_root.exists() or list(vaults_root.iterdir()) == []
    with pytest.raises(InvalidArtifact):
        Vault.open(vault_root, state_home)


def test_open_accepts_a_cas_advanced_vault_registry_generation(tmp_path):
    from mneme.core.fs import read_yaml, write_yaml_cas
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    metadata_path = initialized.root / ".madi/vault.yaml"
    metadata = read_yaml(metadata_path)
    metadata["generation"] = 1
    write_yaml_cas(metadata_path, metadata, expected_generation=0)

    opened = Vault.open(initialized.root, initialized.state_home)
    assert opened.id == initialized.id
    assert read_yaml(metadata_path)["generation"] == 1


def test_initialize_refuses_existing_or_nested_state_root(tmp_path):
    from mneme.core.errors import ArtifactExists, UnsafePath
    from mneme.core.vault import Vault

    vault_root = tmp_path / "vault"
    Vault.initialize(vault_root, tmp_path / "state", "person-01")
    with pytest.raises(ArtifactExists):
        Vault.initialize(vault_root, tmp_path / "other-state", "person-01")

    with pytest.raises(UnsafePath):
        Vault.initialize(tmp_path / "nested-vault", tmp_path / "nested-vault/local", "person-01")


@pytest.mark.parametrize(
    "owner_id",
    [
        "did:example:alice",
        "Alice Example",
        "앨리스 사용자",
        "../person",
        "person/local",
        "person\\local",
        " ",
    ],
)
def test_owner_id_is_preserved_as_opaque_data_with_a_separate_safe_vault_id(
    tmp_path, owner_id
):
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", owner_id)
    reopened = Vault.open(vault.root, vault.state_home)

    assert reopened.owner == {"type": "person", "id": owner_id}
    assert reopened.id == vault.id
    assert reopened.local_root.name == vault.id
    assert vault.id and all(character.isascii() and (
        character.isalnum() or character in "._-"
    ) for character in vault.id)


@pytest.mark.parametrize("owner_id", ["", None, 7])
def test_initialize_rejects_empty_or_non_string_owner_id_without_writing(
    tmp_path, owner_id
):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    root = tmp_path / "vault"
    with pytest.raises(InvalidArtifact):
        Vault.initialize(root, tmp_path / "state", owner_id)
    assert not root.exists()
