"""Canonical artifact families and their storage-independent codecs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mneme.core.errors import InvalidArtifact
from mneme.core.fs import dump_frontmatter, dump_yaml, normalize_text

class StorageClass(str, Enum):
    PORTABLE = "portable"
    LOCAL_ONLY = "local-only"


class ArtifactFamily(str, Enum):
    REGISTRY = "registry"
    SESSION = "session"
    MEMORY = "memory"


class ReferenceKind(str, Enum):
    """D3 disclosure categories that can reveal local-only state."""

    ID = "id"
    PATH = "path"
    HASH = "hash"
    COUNT = "count"
    LABEL = "label"
    EXISTENCE = "existence"


@dataclass(frozen=True)
class ArtifactReference:
    kind: ReferenceKind
    storage_class: StorageClass
    value: str | int | bool
    target_family: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReferenceKind):
            raise InvalidArtifact("artifact reference kind must be a ReferenceKind")
        if not isinstance(self.storage_class, StorageClass):
            raise InvalidArtifact(
                "artifact reference storage_class must be a StorageClass"
            )
        if self.target_family is not None and (
            self.kind is not ReferenceKind.ID or self.target_family != "source"
        ):
            raise InvalidArtifact("unsupported typed provenance target family")
        if self.kind in {
            ReferenceKind.ID,
            ReferenceKind.PATH,
            ReferenceKind.HASH,
            ReferenceKind.LABEL,
        }:
            if not isinstance(self.value, str) or not self.value:
                raise InvalidArtifact(
                    f"{self.kind.value} reference value must be a non-empty string"
                )
        elif self.kind is ReferenceKind.COUNT:
            if (
                not isinstance(self.value, int)
                or isinstance(self.value, bool)
                or self.value < 0
            ):
                raise InvalidArtifact(
                    "count reference value must be a non-negative integer"
                )
        elif self.kind is ReferenceKind.EXISTENCE and not isinstance(self.value, bool):
            raise InvalidArtifact("existence reference value must be a boolean")


@dataclass(frozen=True)
class ReferenceManifest:
    """Typed declaration of references already present in metadata and body."""

    metadata: tuple[ArtifactReference, ...] = ()
    body: tuple[ArtifactReference, ...] = ()
    is_complete: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, tuple) or not isinstance(self.body, tuple):
            raise InvalidArtifact("reference manifest sections must be tuples")
        if not all(isinstance(item, ArtifactReference) for item in self.all):
            raise InvalidArtifact(
                "reference manifest entries must be ArtifactReference values"
            )
        if not isinstance(self.is_complete, bool):
            raise InvalidArtifact("reference manifest completeness must be boolean")

    @classmethod
    def complete(
        cls,
        *,
        metadata: tuple[ArtifactReference, ...] = (),
        body: tuple[ArtifactReference, ...] = (),
    ) -> ReferenceManifest:
        return cls(metadata=metadata, body=body, is_complete=True)

    @property
    def all(self) -> tuple[ArtifactReference, ...]:
        return self.metadata + self.body


@dataclass(frozen=True)
class ArtifactDocument:
    metadata: Mapping[str, Any]
    body: str | None = None
    references: ReferenceManifest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, Mapping):
            raise InvalidArtifact("artifact metadata must be a mapping")
        if self.body is not None and not isinstance(self.body, str):
            raise InvalidArtifact("artifact body must be text or None")
        if self.references is not None and not isinstance(
            self.references, ReferenceManifest
        ):
            raise InvalidArtifact(
                "artifact references must be a ReferenceManifest or None"
            )
        if isinstance(self.body, str):
            object.__setattr__(self, "body", normalize_text(self.body))


class ArtifactCodec:
    """Encode/decode one family identically in portable and local storage."""

    def __init__(self, family: ArtifactFamily):
        self.family = family
        self.schema = f"madi.{family.value}.v1"

    def encode(self, document: ArtifactDocument) -> str:
        if self.family is ArtifactFamily.REGISTRY:
            if document.body is not None:
                raise InvalidArtifact("registry artifacts cannot have a Markdown body")
            return dump_yaml(document.metadata)
        if not isinstance(document.body, str):
            raise InvalidArtifact(f"{self.family.value} artifacts require a Markdown body")
        return dump_frontmatter(document.metadata, document.body)

    def decode(self, metadata: dict[str, Any], body: str | None) -> ArtifactDocument:
        return ArtifactDocument(
            metadata=metadata,
            body=body,
            references=ReferenceManifest(),
        )


FAMILY_CODECS = {family: ArtifactCodec(family) for family in ArtifactFamily}
