"""Machine-local source checkout bindings.

Bindings intentionally live outside routed canonical artifacts.  They may contain
absolute paths and are never referenced by a portable project or source record.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mneme.core.artifacts import StorageClass
from mneme.core.errors import InvalidArtifact, UnsafePath
from mneme.core.fs import dump_yaml, exclusive_file_lock, read_yaml, replace_text
from mneme.core.storage import ArtifactLocation
from mneme.core.validation.vault import validate_contained_path, validate_identifier


_BINDING_SCHEMA = "madi.source-bindings.v1"


@dataclass(frozen=True, slots=True)
class SourceBinding:
    source_id: str
    path: Path

    def __post_init__(self) -> None:
        validate_identifier(self.source_id, label="source id")
        requested = Path(self.path)
        if not requested.is_absolute():
            raise InvalidArtifact("source binding path must be absolute")
        path = requested.resolve(strict=False)
        object.__setattr__(self, "path", path)


class SourceBindingStore:
    """Explicit local-state writer for the one source-id-to-path mapping file."""

    def __init__(self, vault: object) -> None:
        self._vault = vault
        self.location = ArtifactLocation(
            StorageClass.LOCAL_ONLY,
            vault.local_root,
            PurePosixPath("bindings/sources.yaml"),
        )
        self.location.path

    def bind(self, source_id: str, path: str | Path) -> SourceBinding:
        binding = SourceBinding(source_id, Path(path))
        if binding.path.is_relative_to(self._vault.root) or binding.path.is_relative_to(
            self._vault.local_root
        ):
            raise UnsafePath("source binding path must be outside Vault state")
        target = self.location.path
        with exclusive_file_lock(self._vault.local_root / "locks" / "source-bindings.lock"):
            bindings = self._read_all()
            bindings[binding.source_id] = str(binding.path)
            replace_text(
                target,
                dump_yaml(
                    {
                        "schema": _BINDING_SCHEMA,
                        "bindings": {
                            key: bindings[key] for key in sorted(bindings)
                        },
                    }
                ),
            )
        return binding

    def load(self, source_id: str) -> SourceBinding | None:
        validate_identifier(source_id, label="source id")
        value = self._read_all().get(source_id)
        return None if value is None else SourceBinding(source_id, Path(value))

    def _read_all(self) -> dict[str, str]:
        target = self.location.path
        if not target.exists():
            return {}
        metadata = read_yaml(target)
        if not isinstance(metadata, dict) or set(metadata) != {"schema", "bindings"}:
            raise InvalidArtifact("source bindings have an invalid schema")
        if metadata.get("schema") != _BINDING_SCHEMA or not isinstance(
            metadata.get("bindings"), dict
        ):
            raise InvalidArtifact("source bindings have an invalid schema")
        bindings: dict[str, str] = {}
        for source_id, path in metadata["bindings"].items():
            binding = SourceBinding(source_id, Path(path))
            if binding.path.is_relative_to(self._vault.root) or binding.path.is_relative_to(
                self._vault.local_root
            ):
                raise UnsafePath("source binding path must be outside Vault state")
            bindings[source_id] = str(binding.path)
        return bindings
