from pathlib import Path
import os
import shutil
import subprocess
from hashlib import sha256

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


def _directory_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def _directory_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


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


def _portable_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_initialize_creates_exact_portable_and_local_layout(tmp_path):
    from mneme.core.fs import read_yaml
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")

    assert (vault.root / ".madi/schema-version").read_text(encoding="utf-8") == "1\n"
    assert vault.local_root == tmp_path / "state" / "vaults" / vault.id
    assert _relative_entries(vault.root) == (
        PORTABLE_DIRECTORIES | {".madi/policies/vault-default"},
        PORTABLE_FILES | {".madi/policies/vault-default/1.yaml"},
    )
    local_directories, local_files = _relative_entries(vault.local_root)
    assert local_directories == LOCAL_DIRECTORIES
    assert all(path.startswith("locks/") for path in local_files)
    assert read_yaml(vault.root / ".madi/vault.yaml") == {
        "generation": 0,
        "id": vault.id,
        "owner": {"id": "person-01", "type": "person"},
        "schema_version": 1,
    }
    assert read_yaml(vault.root / ".madi/policy-index.yaml") == {
        "generation": 1,
        "policies": {
            "vault-default": {
                "digest": sha256(
                    (vault.root / ".madi/policies/vault-default/1.yaml").read_bytes()
                ).hexdigest(),
                "revision": "1",
            },
        },
        "schema": "madi.policy-index.v1",
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


@pytest.mark.parametrize(
    "index",
    [
        {"generation": 9, "policies": {}},
        {
            "schema": "madi.policy-index.v1",
            "generation": 1,
            "policies": {},
            "unexpected": True,
        },
        {
            "schema": "madi.policy-index.v1",
            "generation": True,
            "policies": {},
        },
        {
            "schema": "madi.policy-index.v1",
            "generation": 1,
            "policies": [],
        },
        {
            "schema": "madi.policy-index.v1",
            "generation": 1,
            "policies": {"policy": {"revision": "1"}},
        },
        {
            "schema": "madi.policy-index.v1",
            "generation": 1,
            "policies": {"policy": {"revision": "1", "digest": "not-a-digest"}},
        },
        {
            "schema": "madi.policy-index.v1",
            "generation": 1,
            "policies": {
                "policy": {
                    "revision": "1",
                    "digest": "0" * 64,
                    "storage_class": "local-only",
                }
            },
        },
        {
            "schema": "madi.policy-index.v1",
            "generation": 1,
            "policies": {"missing-revision": {"revision": "1", "digest": "0" * 64}},
        },
    ],
)
def test_open_rejects_noncanonical_or_unresolvable_policy_indexes(tmp_path, index):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.fs import dump_yaml
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    (initialized.root / ".madi/policy-index.yaml").write_text(
        dump_yaml(index), encoding="utf-8"
    )

    with pytest.raises(InvalidArtifact):
        Vault.open(initialized.root, initialized.state_home)


def test_open_accepts_only_the_exact_empty_bootstrap_policy_index_form(tmp_path):
    from mneme.core.fs import dump_yaml
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    (initialized.root / ".madi/policy-index.yaml").write_text(
        dump_yaml({"generation": 0, "policies": {}}), encoding="utf-8"
    )

    opened = Vault.open(initialized.root, initialized.state_home)
    assert opened.id == initialized.id


def test_open_rejects_missing_local_state_instead_of_creating_it(tmp_path):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state-a", "person-01")
    missing_state_home = tmp_path / "state-b"

    with pytest.raises(InvalidArtifact):
        Vault.open(initialized.root, missing_state_home)
    assert not missing_state_home.exists()


@pytest.mark.parametrize("precreate_state_home", [False, True])
def test_open_or_bootstrap_local_creates_only_exact_local_layout_without_portable_writes(
    tmp_path, precreate_state_home
):
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state-a", "person-01")
    portable_before = _portable_files(initialized.root)
    new_state_home = tmp_path / "state-b"
    if precreate_state_home:
        new_state_home.mkdir()

    opened = Vault.open_or_bootstrap_local(initialized.root, new_state_home)

    assert opened.local_root == new_state_home / "vaults" / initialized.id
    assert _relative_entries(opened.local_root) == (LOCAL_DIRECTORIES, set())
    assert _portable_files(initialized.root) == portable_before
    assert Vault.open(initialized.root, new_state_home).id == initialized.id
    assert (
        Vault.open_or_bootstrap_local(initialized.root, new_state_home).id
        == initialized.id
    )
    assert _relative_entries(opened.local_root) == (LOCAL_DIRECTORIES, set())


def test_open_or_bootstrap_local_validates_portable_vault_before_local_writes(tmp_path):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.fs import dump_yaml
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state-a", "person-01")
    (initialized.root / ".madi/policy-index.yaml").write_text(
        dump_yaml({"schema": "madi.policy-index.v1", "generation": 1, "policies": []}),
        encoding="utf-8",
    )
    new_state_home = tmp_path / "state-b"

    with pytest.raises(InvalidArtifact):
        Vault.open_or_bootstrap_local(initialized.root, new_state_home)

    assert not new_state_home.exists()


def test_open_or_bootstrap_local_rejects_existing_partial_local_root(tmp_path):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state-a", "person-01")
    new_state_home = tmp_path / "state-b"
    local_root = new_state_home / "vaults" / initialized.id
    (local_root / "bindings").mkdir(parents=True)

    with pytest.raises(InvalidArtifact):
        Vault.open_or_bootstrap_local(initialized.root, new_state_home)

    assert _relative_entries(local_root) == ({"bindings"}, set())


def test_open_or_bootstrap_local_cleans_failed_stage(tmp_path, monkeypatch):
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state-a", "person-01")
    new_state_home = tmp_path / "state-b"
    real_mkdir = Path.mkdir

    def fail_on_staged_evidence(path, *args, **kwargs):
        if path.name == "evidence" and any(
            part.endswith(".bootstrapping") for part in path.parts
        ):
            raise OSError("injected local bootstrap failure")
        return real_mkdir(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", fail_on_staged_evidence)
        with pytest.raises(OSError, match="injected local bootstrap failure"):
            Vault.open_or_bootstrap_local(initialized.root, new_state_home)

    vaults_root = new_state_home / "vaults"
    assert vaults_root.is_dir()
    assert list(vaults_root.iterdir()) == []


def test_open_or_bootstrap_local_converges_when_cooperative_publisher_wins(
    tmp_path, monkeypatch
):
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state-a", "person-01")
    new_state_home = tmp_path / "state-b"
    real_mkdir = Path.mkdir
    real_rename = Path.rename

    def publish_first(path, target):
        if path.name.endswith(".bootstrapping"):
            real_mkdir(target)
            for relative in LOCAL_DIRECTORIES:
                real_mkdir(target / relative)
            raise FileExistsError("cooperative publisher won")
        return real_rename(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", publish_first)
        opened = Vault.open_or_bootstrap_local(initialized.root, new_state_home)

    assert opened.local_root == new_state_home / "vaults" / initialized.id
    assert _relative_entries(opened.local_root) == (LOCAL_DIRECTORIES, set())
    assert not any(
        path.name.endswith(".bootstrapping")
        for path in (new_state_home / "vaults").iterdir()
    )


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
    missing.unlink() if missing.is_file() else shutil.rmtree(missing)

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


def test_open_rejects_a_vault_beneath_a_directory_symlink_ancestor(tmp_path):
    """Catches resolving the supplied root before inspecting all path components."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    initialized = Vault.initialize(
        real_parent / "vault", tmp_path / "state", "person-01"
    )
    alias = tmp_path / "alias-parent"
    _directory_symlink(alias, real_parent)
    try:
        with pytest.raises(InvalidArtifact):
            Vault.open(alias / "vault", initialized.state_home)
    finally:
        alias.unlink()


def test_open_rejects_state_home_beneath_a_directory_symlink_ancestor(tmp_path):
    """Catches local layout checks following an aliased state-home ancestor."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    initialized = Vault.initialize(
        tmp_path / "vault", tmp_path / "real-state", "person-01"
    )
    alias = tmp_path / "state-alias"
    _directory_symlink(alias, initialized.state_home)
    try:
        with pytest.raises(InvalidArtifact):
            Vault.open(initialized.root, alias)
    finally:
        alias.unlink()


def test_initialize_rejects_a_missing_vault_beneath_a_symlink_ancestor(
    tmp_path,
):
    """Catches initialization resolving and writing through an existing link prefix."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    target_parent = tmp_path / "target-parent"
    target_parent.mkdir()
    alias = tmp_path / "alias-parent"
    _directory_symlink(alias, target_parent)
    try:
        with pytest.raises(InvalidArtifact):
            Vault.initialize(
                alias / "missing" / "vault",
                tmp_path / "state",
                "person-01",
            )
    finally:
        alias.unlink()

    assert not (target_parent / "missing" / "vault").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_open_rejects_a_vault_beneath_an_ancestor_junction(tmp_path):
    """Catches final-component lstat missing an earlier Windows junction."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    initialized = Vault.initialize(
        real_parent / "vault", tmp_path / "state", "person-01"
    )
    alias = tmp_path / "alias-parent"
    _directory_junction(alias, real_parent)
    try:
        with pytest.raises(InvalidArtifact):
            Vault.open(alias / "vault", initialized.state_home)
    finally:
        alias.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_open_rejects_an_internal_madi_directory_junction(tmp_path):
    """Catches resolve-plus-containment accepting an in-root junction target."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    initialized = Vault.initialize(
        tmp_path / "vault", tmp_path / "state", "person-01"
    )
    madi = initialized.root / ".madi"
    target = initialized.root / "madi-target"
    madi.rename(target)
    _directory_junction(madi, target)
    try:
        with pytest.raises(InvalidArtifact):
            Vault.open(initialized.root, initialized.state_home)
    finally:
        madi.rmdir()
        target.rename(madi)


def test_initialize_and_open_accept_clean_nested_missing_roots(tmp_path):
    """Catches link validation rejecting ordinary not-yet-created path tails."""
    from mneme.core.vault import Vault

    root = tmp_path / "portable-parent" / "nested" / "vault"
    state_home = tmp_path / "local-parent" / "nested" / "state"

    initialized = Vault.initialize(root, state_home, "person-01")
    opened = Vault.open(root, state_home)

    assert opened.id == initialized.id
    assert opened.root == root
    assert opened.local_root == state_home / "vaults" / initialized.id
    local_directories, local_files = _relative_entries(opened.local_root)
    assert local_directories == LOCAL_DIRECTORIES
    assert all(path.startswith("locks/") for path in local_files)


@pytest.mark.parametrize("relative_path", sorted(LOCAL_DIRECTORIES))
def test_open_rejects_each_missing_required_local_layout_entry(tmp_path, relative_path):
    from mneme.core.errors import InvalidArtifact
    from mneme.core.vault import Vault

    initialized = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    shutil.rmtree(initialized.local_root / relative_path)

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
