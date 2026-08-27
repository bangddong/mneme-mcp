"""Validation seam for Vault layout and one-way portability references."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from mneme.core.artifacts import ArtifactDocument, ArtifactFamily
from mneme.core.errors import InvalidArtifact, PortabilityViolation, UnsafePath


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_identifier(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise InvalidArtifact(f"unsafe {label}: {value!r}")


def validate_separate_roots(portable_root: Path, local_root: Path) -> None:
    portable = Path(portable_root).resolve(strict=False)
    local = Path(local_root).resolve(strict=False)
    if portable == local or portable.is_relative_to(local) or local.is_relative_to(portable):
        raise UnsafePath("portable and machine-local roots must be disjoint")


def validate_relative_path(family: ArtifactFamily, value: str | Path) -> PurePosixPath:
    raw = str(value)
    path = PurePosixPath(raw)
    windows_path = PureWindowsPath(raw)
    if (
        not raw
        or "\\" in raw
        or path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UnsafePath(f"unsafe relative artifact path: {value}")
    if not _matches_family(family, path):
        raise UnsafePath(f"path does not belong to {family.value}: {path}")
    return path


def validate_contained_path(root: Path, relative_path: PurePosixPath) -> Path:
    boundary = Path(root).resolve(strict=False)
    candidate = boundary.joinpath(*relative_path.parts)
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise UnsafePath(f"cannot resolve artifact path: {candidate}") from exc
    if not resolved.is_relative_to(boundary):
        raise UnsafePath(f"artifact path escapes storage root: {candidate}")
    return candidate


def validate_portability(storage_class: object, document: ArtifactDocument) -> None:
    """Fail closed for portable writes using an explicit typed reference manifest."""
    from mneme.core.storage import StorageClass

    if not isinstance(storage_class, StorageClass):
        raise InvalidArtifact("canonical writes require a StorageClass")
    if storage_class is StorageClass.LOCAL_ONLY:
        return
    manifest = document.references
    if manifest is None or not manifest.is_complete:
        raise PortabilityViolation(
            "portable writes require a complete typed reference manifest"
        )
    local_references = [
        reference
        for reference in manifest.all
        if reference.storage_class is StorageClass.LOCAL_ONLY
    ]
    if local_references:
        kinds = ", ".join(sorted({reference.kind.value for reference in local_references}))
        raise PortabilityViolation(
            f"portable artifact contains local-only references: {kinds}"
        )


def _matches_family(family: ArtifactFamily, path: PurePosixPath) -> bool:
    parts = path.parts
    if family is ArtifactFamily.MEMORY:
        return len(parts) == 2 and parts[0] == "memory" and path.suffix == ".md"
    if family is ArtifactFamily.SESSION:
        return (
            len(parts) == 5
            and parts[0] == "workstreams"
            and parts[2] == "sessions"
            and path.suffix == ".md"
        )
    if path.suffix != ".yaml":
        return False
    if len(parts) == 2 and parts[0] in {"projects", "sources"}:
        return True
    if len(parts) == 3 and parts[0] == "workstreams" and parts[2] == "workstream.yaml":
        return True
    if parts == (".madi", "vault.yaml") or parts == (".madi", "policy-index.yaml"):
        return True
    return len(parts) == 4 and parts[:2] == (".madi", "policies")
