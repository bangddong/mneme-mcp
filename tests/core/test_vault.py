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


def test_open_reads_identity_without_creating_missing_local_state(tmp_path):
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state-a", "person-01")
    opened = Vault.open(initialized.root, tmp_path / "state-b")

    assert opened.id == initialized.id
    assert opened.owner == {"id": "person-01", "type": "person"}
    assert opened.local_root == tmp_path / "state-b" / "vaults" / initialized.id
    assert not opened.local_root.exists()


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


@pytest.mark.parametrize("owner_id", ["", "../person", "person/local", "person\\local"])
def test_initialize_rejects_unsafe_owner_id_without_writing(tmp_path, owner_id):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    root = tmp_path / "vault"
    with pytest.raises(InvalidArtifact):
        Vault.initialize(root, tmp_path / "state", owner_id)
    assert not root.exists()
