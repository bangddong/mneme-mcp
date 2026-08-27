"""Canonical artifact families and their storage-independent codecs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from mneme.core.errors import InvalidArtifact
from mneme.core.fs import dump_frontmatter, dump_yaml, normalize_text

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mneme.core.storage import StorageClass


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


@dataclass(frozen=True)
class ReferenceManifest:
    """Typed declaration of references already present in metadata and body."""

    metadata: tuple[ArtifactReference, ...] = ()
    body: tuple[ArtifactReference, ...] = ()
    is_complete: bool = False

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
            references=ReferenceManifest.complete(),
        )


FAMILY_CODECS = {family: ArtifactCodec(family) for family in ArtifactFamily}
