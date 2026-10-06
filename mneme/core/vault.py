"""Portable Vault identity and exact bootstrap layout."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from mneme.core.errors import ArtifactExists, InvalidArtifact
from mneme.core.fs import (
    dump_yaml,
    is_symlink_or_reparse,
    read_yaml,
    validate_path_chain,
    write_new,
)
from mneme.core.artifacts import StorageClass
from mneme.core.storage import ArtifactReader, ArtifactStore, StorageRouter, ViewStore
from mneme.core.validation.vault import (
    validate_identifier,
    validate_owner_id,
    validate_separate_roots,
)
from mneme.core.validation.secrets import reject_detectable_secrets


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
        vault, policy_index = cls._validated_candidate(root, state_home)
        _require_directories(vault.local_root, _LOCAL_DIRECTORIES, "machine-local")
        cls._validate_portable_policies(vault, policy_index)
        return vault

    @classmethod
    def open_or_bootstrap_local(cls, root: Path, state_home: Path) -> Vault:
        """Open a Vault, atomically creating an absent machine-local layout.

        An existing local root remains subject to the strict ``open`` contract:
        missing or unsafe entries are corruption, not an invitation to repair it.
        Portable artifacts are fully validated before any local path is created.
        """
        vault, policy_index = cls._validated_candidate(root, state_home)
        cls._validate_portable_policies(vault, policy_index)
        local_root = vault.local_root
        if local_root.exists() or is_symlink_or_reparse(local_root):
            return cls.open(vault.root, vault.state_home)

        local_parent = vault.state_home / "vaults"
        local_stage = local_parent / f".{vault.id}.{uuid4().hex}.bootstrapping"
        validate_path_chain(local_parent, allow_missing=True)
        validate_path_chain(local_stage, allow_missing=True)
        local_parent.mkdir(parents=True, exist_ok=True)
        validate_path_chain(local_parent, allow_missing=False)
        stage_owned = False
        try:
            validate_path_chain(local_stage, allow_missing=True)
            local_stage.mkdir(exist_ok=False)
            stage_owned = True
            validate_path_chain(local_stage, allow_missing=False)
            _create_directories(local_stage, _LOCAL_DIRECTORIES)

            validate_path_chain(local_stage, allow_missing=False)
            validate_path_chain(local_root, allow_missing=True)
            if local_root.exists() or is_symlink_or_reparse(local_root):
                return cls.open(vault.root, vault.state_home)
            try:
                local_stage.rename(local_root)
            except OSError:
                if not (local_root.exists() or is_symlink_or_reparse(local_root)):
                    raise
                return cls.open(vault.root, vault.state_home)
            stage_owned = False
            return cls.open(vault.root, vault.state_home)
        finally:
            if stage_owned:
                _remove_owned_tree(local_stage)

    @classmethod
    def _validated_candidate(
        cls, root: Path, state_home: Path
    ) -> tuple[Vault, dict[str, object]]:
        requested_root = validate_path_chain(root, allow_missing=True)
        requested_state_home = validate_path_chain(state_home, allow_missing=True)
        portable_root = requested_root.resolve(strict=False)
        local_state_home = requested_state_home.resolve(strict=False)
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
        reject_detectable_secrets(metadata)
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
        reject_detectable_secrets(policy_index)
        local_root = local_state_home / "vaults" / vault_id
        validate_path_chain(local_root, allow_missing=True)
        validate_separate_roots(portable_root, local_root)
        vault = cls(portable_root, local_state_home, vault_id, dict(owner), local_root)
        return vault, policy_index

    @staticmethod
    def _validate_portable_policies(
        vault: Vault, policy_index: dict[str, object]
    ) -> None:
        from mneme.core.policy import PolicyStore, parse_policy_index

        index = parse_policy_index(policy_index, StorageClass.PORTABLE)
        store = PolicyStore(vault)
        for policy_id, ref in index.policies.items():
            _require_file(
                vault.root,
                vault.root
                / ".madi"
                / "policies"
                / policy_id
                / f"{ref.revision}.yaml",
                "active policy revision",
            )
            store.load_active(policy_id, StorageClass.PORTABLE)

    @classmethod
    def initialize(cls, root: Path, state_home: Path, owner_id: str) -> Vault:
        """Create clean roots after checking every existing lexical component.

        Missing path tails are intentionally permitted because both roots may be
        new.  The component checks are repeated around writes, but remain a
        best-effort preflight rather than a race-free directory-handle walk.
        """
        validate_owner_id(owner_id)
        reject_detectable_secrets(owner_id)
        requested_root = validate_path_chain(root, allow_missing=True)
        requested_state_home = validate_path_chain(state_home, allow_missing=True)
        portable_root = requested_root.resolve(strict=False)
        local_state_home = requested_state_home.resolve(strict=False)
        validate_separate_roots(portable_root, local_state_home)
        if portable_root.exists() or portable_root.is_symlink():
            raise ArtifactExists(f"Vault path already exists: {portable_root}")

        vault_id = uuid4().hex
        local_root = local_state_home / "vaults" / vault_id
        validate_path_chain(local_root, allow_missing=True)
        validate_separate_roots(portable_root, local_root)
        if local_root.exists() or local_root.is_symlink():
            raise ArtifactExists(f"local Vault state already exists: {local_root}")
        portable_stage = portable_root.parent / (
            f".{portable_root.name}.{vault_id}.initializing"
        )
        local_parent = local_state_home / "vaults"
        local_stage = local_parent / f".{vault_id}.initializing"
        for stage in (portable_stage, local_stage):
            validate_path_chain(stage, allow_missing=True)
            if stage.exists() or stage.is_symlink():
                raise ArtifactExists(f"Vault initialization stage already exists: {stage}")

        portable_root.parent.mkdir(parents=True, exist_ok=True)
        local_parent.mkdir(parents=True, exist_ok=True)
        validate_path_chain(portable_root.parent, allow_missing=False)
        validate_path_chain(local_parent, allow_missing=False)
        owned: set[Path] = set()
        try:
            validate_path_chain(portable_stage, allow_missing=True)
            portable_stage.mkdir(exist_ok=False)
            validate_path_chain(portable_stage, allow_missing=False)
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

            validate_path_chain(local_stage, allow_missing=True)
            local_stage.mkdir(exist_ok=False)
            validate_path_chain(local_stage, allow_missing=False)
            owned.add(local_stage)
            _create_directories(local_stage, _LOCAL_DIRECTORIES)

            validate_path_chain(local_root, allow_missing=True)
            validate_path_chain(portable_root, allow_missing=True)
            if local_root.exists() or portable_root.exists():
                raise ArtifactExists("Vault target appeared during initialization")
            validate_path_chain(local_stage, allow_missing=False)
            validate_path_chain(local_root, allow_missing=True)
            local_stage.rename(local_root)
            owned.remove(local_stage)
            owned.add(local_root)
            validate_path_chain(portable_stage, allow_missing=False)
            validate_path_chain(portable_root, allow_missing=True)
            portable_stage.rename(portable_root)
            owned.remove(portable_stage)
            owned.add(portable_root)
            vault = cls.open(portable_root, local_state_home)
            from mneme.core.policy import PolicyStore

            PolicyStore(vault).bootstrap_default()
        except Exception:
            for path in sorted(owned, key=lambda item: len(item.parts), reverse=True):
                _remove_owned_tree(path)
            raise
        return vault


def _create_directories(root: Path, relative_paths: tuple[str, ...]) -> None:
    validate_path_chain(root, allow_missing=False)
    for relative in relative_paths:
        path = root / relative
        validate_path_chain(path, allow_missing=True)
        path.mkdir(parents=True, exist_ok=False)
        validate_path_chain(path, allow_missing=False)


def _require_file(root: Path, path: Path, label: str) -> None:
    validate_path_chain(root, allow_missing=True)
    validate_path_chain(path, allow_missing=True)
    if (
        is_symlink_or_reparse(path)
        or not _is_contained(root, path)
        or not path.is_file()
    ):
        raise InvalidArtifact(f"required {label} is missing or unsafe: {path}")


def _require_directories(
    root: Path, relative_paths: tuple[str, ...], storage_label: str
) -> None:
    validate_path_chain(root, allow_missing=True)
    if is_symlink_or_reparse(root) or not root.is_dir():
        raise InvalidArtifact(f"required {storage_label} root is missing or unsafe: {root}")
    for relative in relative_paths:
        path = root / relative
        validate_path_chain(path, allow_missing=True)
        if (
            is_symlink_or_reparse(path)
            or not _is_contained(root, path)
            or not path.is_dir()
        ):
            raise InvalidArtifact(
                f"required {storage_label} directory is missing or unsafe: {path}"
            )


def _is_contained(root: Path, path: Path) -> bool:
    try:
        validate_path_chain(root, allow_missing=True)
        validate_path_chain(path, allow_missing=True)
        return path.resolve(strict=False).is_relative_to(root.resolve(strict=False))
    except (InvalidArtifact, OSError, RuntimeError, ValueError):
        return False


def _remove_owned_tree(path: Path) -> None:
    try:
        validate_path_chain(path, allow_missing=True)
    except InvalidArtifact:
        return
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
