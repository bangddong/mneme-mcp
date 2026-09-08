"""Explicit routing boundaries for canonical artifacts and generated views."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re

from mneme.core.artifacts import (
    FAMILY_CODECS,
    ArtifactCodec,
    ArtifactDocument,
    ArtifactFamily,
    StorageClass,
)
from mneme.core.errors import InvalidArtifact, UnsafePath
from mneme.core.fs import (
    exclusive_file_lock,
    dump_yaml,
    is_symlink_or_reparse,
    read_frontmatter,
    read_yaml,
    replace_text,
    validate_path_chain,
    write_text_cas,
    write_new,
)
from mneme.core.validation.vault import (
    validate_contained_path,
    validate_portability,
    validate_relative_path,
    validate_separate_roots,
)


@dataclass(frozen=True)
class ArtifactLocation:
    storage_class: StorageClass
    root: Path
    relative_path: PurePosixPath

    def __post_init__(self) -> None:
        if not isinstance(self.storage_class, StorageClass):
            raise InvalidArtifact("artifact location requires a StorageClass")
        raw_root = validate_path_chain(Path(self.root), allow_missing=True)
        object.__setattr__(self, "root", raw_root.resolve(strict=False))
        object.__setattr__(self, "relative_path", PurePosixPath(self.relative_path))

    @property
    def path(self) -> Path:
        return validate_contained_path(self.root, self.relative_path)


class StorageRouter:
    """Map one canonical family schema to an explicit portable or local root."""

    def __init__(self, portable_root: Path, local_root: Path):
        raw_portable_root = validate_path_chain(
            Path(portable_root), allow_missing=True
        )
        raw_local_root = validate_path_chain(Path(local_root), allow_missing=True)
        self.portable_root = raw_portable_root.resolve(strict=False)
        self.local_root = raw_local_root.resolve(strict=False)
        validate_separate_roots(self.portable_root, self.local_root)

    def location(
        self,
        family: ArtifactFamily,
        storage_class: StorageClass,
        relative_path: str | Path,
    ) -> ArtifactLocation:
        if not isinstance(family, ArtifactFamily):
            raise InvalidArtifact("canonical routing requires an ArtifactFamily")
        if not isinstance(storage_class, StorageClass):
            raise InvalidArtifact("canonical routing requires a StorageClass")
        relative = validate_relative_path(family, relative_path)
        root = (
            self.portable_root
            if storage_class is StorageClass.PORTABLE
            else self.local_root / "overlays"
        )
        location = ArtifactLocation(storage_class, root, relative)
        location.path
        return location

    def codec(
        self, family: ArtifactFamily, storage_class: StorageClass
    ) -> ArtifactCodec:
        if not isinstance(storage_class, StorageClass):
            raise InvalidArtifact("codec selection requires a StorageClass")
        return FAMILY_CODECS[family]

    def validate_location(
        self, family: ArtifactFamily, location: ArtifactLocation
    ) -> ArtifactLocation:
        expected = self.location(family, location.storage_class, location.relative_path)
        if location.root != expected.root:
            raise UnsafePath("artifact location root does not match its storage class")
        location.path
        return location

    def lock_path(self, location: ArtifactLocation) -> Path:
        self.validate_location_for_storage(location)
        key = (
            f"{location.storage_class.value}\0{location.root}\0"
            f"{location.relative_path.as_posix()}"
        )
        name = f"{sha256(key.encode('utf-8')).hexdigest()}.lock"
        return validate_contained_path(
            self.local_root, PurePosixPath("locks") / name
        )

    def validate_location_for_storage(self, location: ArtifactLocation) -> None:
        expected_root = (
            self.portable_root
            if location.storage_class is StorageClass.PORTABLE
            else self.local_root / "overlays"
        )
        if location.root != expected_root:
            raise UnsafePath("artifact location root does not match its storage class")

    def view_store(self) -> ViewStore:
        return ViewStore._from_router(self)


class ArtifactStore:
    """The only foundational writer boundary for canonical artifact families."""

    def __init__(self, router: StorageRouter):
        self.router = router

    def write_new(
        self,
        family: ArtifactFamily,
        *,
        storage_class: StorageClass,
        relative_path: str | Path,
        document: ArtifactDocument,
    ) -> ArtifactLocation:
        location = self.router.location(family, storage_class, relative_path)
        return self.write_new_at(family, location=location, document=document)

    def write_new_at(
        self,
        family: ArtifactFamily,
        *,
        location: ArtifactLocation,
        document: ArtifactDocument,
    ) -> ArtifactLocation:
        self.router.validate_location(family, location)
        validate_portability(location.storage_class, document)
        encoded = self.router.codec(family, location.storage_class).encode(document)
        target = location.path
        validate_path_chain(target, allow_missing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        validate_path_chain(target, allow_missing=True)
        self.router.validate_location(family, location)
        write_new(target, encoded)
        return location

    def write_cas(
        self,
        family: ArtifactFamily,
        *,
        storage_class: StorageClass,
        relative_path: str | Path,
        document: ArtifactDocument,
        expected_generation: int,
    ) -> ArtifactLocation:
        location = self.router.location(family, storage_class, relative_path)
        self.router.validate_location(family, location)
        validate_portability(storage_class, document)
        encoded = self.router.codec(family, storage_class).encode(document)
        self.router.validate_location(family, location)
        reader = ArtifactReader(self.router)
        with exclusive_file_lock(self.router.lock_path(location)):
            write_text_cas(
                location.path,
                encoded,
                expected_generation=expected_generation,
                next_generation=document.metadata.get("generation"),
                read_generation=lambda: reader.read(
                    family, location=location
                ).metadata.get("generation"),
            )
        return location

class ArtifactReader:
    """Read-only canonical artifact interface suitable for resolvers and context."""

    def __init__(self, router: StorageRouter):
        self.router = router

    def read(
        self,
        family: ArtifactFamily,
        *,
        location: ArtifactLocation | None = None,
        storage_class: StorageClass | None = None,
        relative_path: str | Path | None = None,
    ) -> ArtifactDocument:
        if location is None:
            if storage_class is None or relative_path is None:
                raise TypeError("read requires an ArtifactLocation or explicit storage_class/path")
            location = self.router.location(family, storage_class, relative_path)
        else:
            self.router.validate_location(family, location)
        if family is ArtifactFamily.REGISTRY:
            return self.router.codec(family, location.storage_class).decode(
                read_yaml(location.path), None
            )
        metadata, body = read_frontmatter(location.path)
        return self.router.codec(family, location.storage_class).decode(metadata, body)


class ViewStore:
    """Generated-only writer restricted to local CURRENT and PROFILE projections."""

    _NAMES = frozenset({"CURRENT.md", "PROFILE.md"})
    _AUTHORIZATION_SCHEMA = "madi.generated-view-authorization.v1"
    _FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("obtain ViewStore from StorageRouter.view_store() or Vault.views")

    @classmethod
    def _from_router(cls, router: StorageRouter) -> ViewStore:
        instance = object.__new__(cls)
        instance.local_root = router.local_root
        instance.root = router.local_root / "views"
        return instance

    def write(
        self,
        relative_path: str,
        text: str,
        *,
        authorization_fingerprint: str | None = None,
    ) -> Path:
        if relative_path not in self._NAMES:
            raise UnsafePath(f"unsupported generated view path: {relative_path}")
        if authorization_fingerprint is not None and not self._valid_fingerprint(
            authorization_fingerprint
        ):
            raise InvalidArtifact("generated view authorization fingerprint is invalid")
        target = validate_contained_path(self.root, PurePosixPath(relative_path))
        self.root.mkdir(parents=True, exist_ok=True)
        target = validate_contained_path(self.root, PurePosixPath(relative_path))
        replace_text(target, text)
        metadata = self._authorization_path(relative_path)
        replace_text(
            metadata,
            dump_yaml(
                {
                    "authorization_fingerprint": authorization_fingerprint,
                    "schema": self._AUTHORIZATION_SCHEMA,
                }
            ),
        )
        return target

    def load(
        self, relative_path: str, *, authorization_fingerprint: str | None
    ) -> str | None:
        """Return a local view only when its live authorization input still matches.

        The caller obtains the opaque fingerprint from ``PolicyAuthorizer``.  A
        missing, malformed, stale, or unsafe view is a cache miss rather than a
        reason to expose stale text or policy detail.
        """
        if relative_path not in self._NAMES or not self._valid_fingerprint(
            authorization_fingerprint
        ):
            return None
        try:
            target = validate_contained_path(self.root, PurePosixPath(relative_path))
            metadata_path = self._authorization_path(relative_path)
            if (
                is_symlink_or_reparse(target)
                or is_symlink_or_reparse(metadata_path)
                or not target.is_file()
                or not metadata_path.is_file()
            ):
                return None
            metadata = read_yaml(metadata_path)
            if metadata != {
                "authorization_fingerprint": authorization_fingerprint,
                "schema": self._AUTHORIZATION_SCHEMA,
            }:
                return None
            return target.read_text(encoding="utf-8")
        except Exception:
            return None

    def _authorization_path(self, relative_path: str) -> Path:
        return validate_contained_path(
            self.root, PurePosixPath(f"{relative_path}.authorization.yaml")
        )

    @classmethod
    def _valid_fingerprint(cls, value: object) -> bool:
        return isinstance(value, str) and cls._FINGERPRINT.fullmatch(value) is not None
