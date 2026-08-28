"""Portable Vault identity and exact bootstrap layout."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from mneme.core.errors import ArtifactExists, InvalidArtifact
from mneme.core.fs import dump_yaml, read_yaml, write_new
from mneme.core.storage import ArtifactReader, ArtifactStore, StorageRouter, ViewStore
from mneme.core.validation.vault import (
    validate_identifier,
    validate_owner_id,
    validate_separate_roots,
)


_PORTABLE_DIRECTORIES = (
    ".madi/policies",
    "projects",
    "sources",
    "workstreams",
    "memory",
)
_LOCAL_DIRECTORIES = (
    "bindings",
    "overlays",
    "evidence",
    "pending",
    "views",
    "index",
    "cache",
    "locks",
    "logs",
)


@dataclass(frozen=True)
class Vault:
    root: Path
    state_home: Path
    id: str
    owner: dict[str, str]
    local_root: Path
    router: StorageRouter = field(init=False, repr=False, compare=False)
    artifacts: ArtifactStore = field(init=False, repr=False, compare=False)
    reader: ArtifactReader = field(init=False, repr=False, compare=False)
    views: ViewStore = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        router = StorageRouter(self.root, self.local_root)
        object.__setattr__(self, "router", router)
        object.__setattr__(self, "artifacts", ArtifactStore(router))
        object.__setattr__(self, "reader", ArtifactReader(router))
        object.__setattr__(self, "views", router.view_store())

    @classmethod
    def open(cls, root: Path, state_home: Path) -> Vault:
        requested_root = Path(root)
        if requested_root.is_symlink():
            raise InvalidArtifact("Vault root cannot be a symbolic link")
        portable_root = requested_root.resolve(strict=False)
        local_state_home = Path(state_home).resolve(strict=False)
        _require_file(
            portable_root, portable_root / ".madi/schema-version", "schema-version"
        )
        _require_file(
            portable_root, portable_root / ".madi/vault.yaml", "Vault registry"
        )
        _require_file(
            portable_root,
            portable_root / ".madi/policy-index.yaml",
            "policy index",
        )
        _require_directories(portable_root, _PORTABLE_DIRECTORIES, "portable")
        try:
            schema_version = (portable_root / ".madi/schema-version").read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeError) as exc:
            raise InvalidArtifact("Vault schema-version is missing or unreadable") from exc
        if schema_version != "1\n":
            raise InvalidArtifact(f"unsupported Vault schema version: {schema_version!r}")
        metadata = read_yaml(portable_root / ".madi/vault.yaml")
        vault_id = metadata.get("id")
        owner = metadata.get("owner")
        validate_identifier(vault_id, label="Vault id")
        if (
            not isinstance(owner, dict)
            or owner.get("type") != "person"
            or set(owner) != {"type", "id"}
        ):
            raise InvalidArtifact("Vault owner must be a typed person reference")
        validate_owner_id(owner.get("id"))
        generation = metadata.get("generation")
        if (
            metadata.get("schema_version") != 1
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
        ):
            raise InvalidArtifact("Vault metadata has an invalid foundational schema")
        policy_index = read_yaml(portable_root / ".madi/policy-index.yaml")
        policy_generation = policy_index.get("generation")
        if (
            not isinstance(policy_generation, int)
            or isinstance(policy_generation, bool)
            or policy_generation < 0
            or not isinstance(policy_index.get("policies"), dict)
        ):
            raise InvalidArtifact("policy index has an invalid foundational schema")
        local_root = local_state_home / "vaults" / vault_id
        validate_separate_roots(portable_root, local_root)
        _require_directories(local_root, _LOCAL_DIRECTORIES, "machine-local")
        return cls(portable_root, local_state_home, vault_id, dict(owner), local_root)

    @classmethod
    def initialize(cls, root: Path, state_home: Path, owner_id: str) -> Vault:
        validate_owner_id(owner_id)
        portable_root = Path(root).resolve(strict=False)
        local_state_home = Path(state_home).resolve(strict=False)
        validate_separate_roots(portable_root, local_state_home)
        if portable_root.exists() or portable_root.is_symlink():
            raise ArtifactExists(f"Vault path already exists: {portable_root}")

        vault_id = uuid4().hex
        local_root = local_state_home / "vaults" / vault_id
        validate_separate_roots(portable_root, local_root)
        if local_root.exists() or local_root.is_symlink():
            raise ArtifactExists(f"local Vault state already exists: {local_root}")
        portable_stage = portable_root.parent / (
            f".{portable_root.name}.{vault_id}.initializing"
        )
        local_parent = local_state_home / "vaults"
        local_stage = local_parent / f".{vault_id}.initializing"
        for stage in (portable_stage, local_stage):
            if stage.exists() or stage.is_symlink():
                raise ArtifactExists(f"Vault initialization stage already exists: {stage}")

        portable_root.parent.mkdir(parents=True, exist_ok=True)
        local_parent.mkdir(parents=True, exist_ok=True)
        owned: set[Path] = set()
        try:
            portable_stage.mkdir(exist_ok=False)
            owned.add(portable_stage)
            _create_directories(portable_stage, _PORTABLE_DIRECTORIES)
            write_new(portable_stage / ".madi/schema-version", "1")
            write_new(
                portable_stage / ".madi/vault.yaml",
                dump_yaml(
                    {
                        "generation": 0,
                        "id": vault_id,
                        "owner": {"type": "person", "id": owner_id},
                        "schema_version": 1,
                    }
                ),
            )
            write_new(
                portable_stage / ".madi/policy-index.yaml",
                dump_yaml({"generation": 0, "policies": {}}),
            )

            local_stage.mkdir(exist_ok=False)
            owned.add(local_stage)
            _create_directories(local_stage, _LOCAL_DIRECTORIES)

            if local_root.exists() or portable_root.exists():
                raise ArtifactExists("Vault target appeared during initialization")
            local_stage.rename(local_root)
            owned.remove(local_stage)
            owned.add(local_root)
            portable_stage.rename(portable_root)
            owned.remove(portable_stage)
            owned.add(portable_root)
            vault = cls.open(portable_root, local_state_home)
        except Exception:
            for path in sorted(owned, key=lambda item: len(item.parts), reverse=True):
                _remove_owned_tree(path)
            raise
        return vault


def _create_directories(root: Path, relative_paths: tuple[str, ...]) -> None:
    for relative in relative_paths:
        (root / relative).mkdir(parents=True, exist_ok=False)


def _require_file(root: Path, path: Path, label: str) -> None:
    if not _is_contained(root, path) or path.is_symlink() or not path.is_file():
        raise InvalidArtifact(f"required {label} is missing or unsafe: {path}")


def _require_directories(
    root: Path, relative_paths: tuple[str, ...], storage_label: str
) -> None:
    if root.is_symlink() or not root.is_dir():
        raise InvalidArtifact(f"required {storage_label} root is missing or unsafe: {root}")
    for relative in relative_paths:
        path = root / relative
        if not _is_contained(root, path) or path.is_symlink() or not path.is_dir():
            raise InvalidArtifact(
                f"required {storage_label} directory is missing or unsafe: {path}"
            )


def _is_contained(root: Path, path: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False


def _remove_owned_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
