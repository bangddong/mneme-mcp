"""Portable Vault identity and exact bootstrap layout."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from mneme.core.errors import ArtifactExists, InvalidArtifact
from mneme.core.fs import dump_yaml, read_yaml, write_new
from mneme.core.storage import ArtifactReader, ArtifactStore, StorageRouter, ViewStore
from mneme.core.validation.vault import validate_identifier, validate_separate_roots


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
        object.__setattr__(self, "views", ViewStore(self.local_root))

    @classmethod
    def open(cls, root: Path, state_home: Path) -> Vault:
        portable_root = Path(root).resolve(strict=False)
        local_state_home = Path(state_home).resolve(strict=False)
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
        validate_identifier(owner.get("id"), label="owner id")
        generation = metadata.get("generation")
        if (
            metadata.get("schema_version") != 1
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
        ):
            raise InvalidArtifact("Vault metadata has an invalid foundational schema")
        local_root = local_state_home / "vaults" / vault_id
        validate_separate_roots(portable_root, local_root)
        return cls(portable_root, local_state_home, vault_id, dict(owner), local_root)

    @classmethod
    def initialize(cls, root: Path, state_home: Path, owner_id: str) -> Vault:
        validate_identifier(owner_id, label="owner id")
        portable_root = Path(root).resolve(strict=False)
        local_state_home = Path(state_home).resolve(strict=False)
        validate_separate_roots(portable_root, local_state_home)
        if portable_root.exists():
            raise ArtifactExists(f"Vault path already exists: {portable_root}")

        vault_id = uuid4().hex
        local_root = local_state_home / "vaults" / vault_id
        validate_separate_roots(portable_root, local_root)
        if local_root.exists():
            raise ArtifactExists(f"local Vault state already exists: {local_root}")

        portable_root.mkdir(parents=True, exist_ok=False)
        for relative in _PORTABLE_DIRECTORIES:
            (portable_root / relative).mkdir(parents=True, exist_ok=False)
        write_new(portable_root / ".madi/schema-version", "1")
        write_new(
            portable_root / ".madi/vault.yaml",
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
            portable_root / ".madi/policy-index.yaml",
            dump_yaml({"generation": 0, "policies": {}}),
        )

        local_root.mkdir(parents=True, exist_ok=False)
        for relative in _LOCAL_DIRECTORIES:
            (local_root / relative).mkdir(parents=True, exist_ok=False)

        return cls.open(portable_root, local_state_home)
