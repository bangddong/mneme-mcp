"""Typed, CAS-protected registry records for Madi Core."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path, PureWindowsPath
import re
from urllib.parse import urlsplit

from mneme.core.artifacts import (
    ArtifactDocument,
    ArtifactFamily,
    ArtifactReference,
    ReferenceKind,
    ReferenceManifest,
    StorageClass,
)
from mneme.core.errors import ConcurrentWrite, InvalidArtifact, MadiError
from mneme.core.policy import (
    PolicyRef,
    PolicyStore,
    Portability,
    admission_mutation,
    evaluate_portability,
)
from mneme.core.validation.vault import validate_identifier


_WORKSTREAM_SCHEMA = "madi.workstream-registry.v1"
_PROJECT_SCHEMA = "madi.project-registry.v1"
_SOURCE_SCHEMA = "madi.source-registry.v1"
_STATUSES = frozenset({"active", "paused", "closed"})
_MODES = frozenset({"single", "preferred", "parallel"})
_CREDENTIAL_WORD = re.compile(
    r"(?i)(?:^|[?&#;:_-])(api[_-]?key|token|secret|password|passwd|credential|authorization)(?:$|[?&#;:=_-])"
)
_CONFIDENTIAL_MARKER = re.compile(
    r"(?i)(?:^|[/:?&#;_.=-])(confidential|private|secret)(?:$|[/:?&#;_.=-])"
)


class RegistryConflict(MadiError):
    """A registry mutation could not be proven safe to apply."""

    def __init__(self, message: str, *, code: str = "registry-conflict") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class HeadRef:
    session: str
    revision: str

    def __post_init__(self) -> None:
        validate_identifier(self.session, label="session id")
        validate_identifier(self.revision, label="session revision")


@dataclass(frozen=True, slots=True)
class ProjectRegistry:
    id: str
    generation: int
    authority: str
    locator: str | None = None
    policy_ref: PolicyRef | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.id, label="project id")
        _validate_generation(self.generation, label="project")
        validate_identifier(self.authority, label="project authority")
        _validate_locator(self.locator, StorageClass.LOCAL_ONLY)
        if self.policy_ref is not None and not isinstance(self.policy_ref, PolicyRef):
            raise InvalidArtifact("project policy_ref must be a PolicyRef or None")

    def with_policy_ref(self, policy_ref: PolicyRef) -> ProjectRegistry:
        return replace(self, policy_ref=policy_ref)


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    id: str
    generation: int
    kind: str
    authority: str
    project: str | None = None
    locator: str | None = None
    policy_ref: PolicyRef | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.id, label="source id")
        _validate_generation(self.generation, label="source")
        validate_identifier(self.kind, label="source kind")
        validate_identifier(self.authority, label="source authority")
        if self.project is not None:
            validate_identifier(self.project, label="source project id")
        _validate_locator(self.locator, StorageClass.LOCAL_ONLY)
        if self.policy_ref is not None and not isinstance(self.policy_ref, PolicyRef):
            raise InvalidArtifact("source policy_ref must be a PolicyRef or None")

    def with_policy_ref(self, policy_ref: PolicyRef) -> SourceRegistry:
        return replace(self, policy_ref=policy_ref)


@dataclass(frozen=True, slots=True)
class WorkstreamRegistry:
    id: str
    generation: int
    project: str | None
    status: str
    mode: str
    active_heads: tuple[HeadRef, ...] = ()
    preferred_head: HeadRef | None = None
    policy_refs: tuple[PolicyRef, ...] = ()
    overlay_of: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.id, label="workstream id")
        if self.overlay_of is not None:
            validate_identifier(self.overlay_of, label="portable overlay base")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise InvalidArtifact("workstream generation must be a non-negative integer")
        if self.project is not None:
            validate_identifier(self.project, label="project id")
        if self.status not in _STATUSES:
            raise InvalidArtifact("workstream status is invalid")
        if self.mode not in _MODES:
            raise InvalidArtifact("workstream resolution mode is invalid")
        if not isinstance(self.active_heads, tuple) or not all(
            isinstance(head, HeadRef) for head in self.active_heads
        ):
            raise InvalidArtifact("workstream active_heads must be HeadRef values")
        if len({head.session for head in self.active_heads}) != len(self.active_heads):
            raise InvalidArtifact("workstream cannot contain duplicate session heads")
        if self.preferred_head is not None and not isinstance(
            self.preferred_head, HeadRef
        ):
            raise InvalidArtifact("workstream preferred_head must be a HeadRef or None")
        if self.mode == "single" and len(self.active_heads) > 1:
            raise InvalidArtifact("single workstreams allow at most one active head")
        if self.mode == "preferred" and self.preferred_head not in self.active_heads:
            raise InvalidArtifact("preferred workstreams require an active preferred head")
        if self.mode != "preferred" and self.preferred_head is not None:
            raise InvalidArtifact("only preferred workstreams may declare a preferred head")
        if not isinstance(self.policy_refs, tuple) or not all(
            isinstance(policy_ref, PolicyRef) for policy_ref in self.policy_refs
        ):
            raise InvalidArtifact("workstream policy_refs must be PolicyRef values")
        if len({ref.policy_id for ref in self.policy_refs}) != len(self.policy_refs):
            raise InvalidArtifact("workstream cannot contain duplicate policy ids")
        object.__setattr__(
            self,
            "policy_refs",
            tuple(sorted(self.policy_refs, key=lambda item: item.policy_id)),
        )

    def with_status(self, status: str) -> WorkstreamRegistry:
        return replace(self, status=status)

    def with_policy_refs(self, policy_refs: tuple[PolicyRef, ...]) -> WorkstreamRegistry:
        return replace(self, policy_refs=policy_refs)


def canonicalize_workstream_registry(value: object) -> WorkstreamRegistry:
    """Return a validated, fully owned snapshot of a decoded workstream registry.

    Frozen dataclasses can still be altered through ``object.__setattr__``.  Read
    projections therefore re-run the model validators and reconstruct every
    nested reference before retaining a registry supplied by another boundary.
    """

    if not isinstance(value, WorkstreamRegistry):
        raise InvalidArtifact("workstream registry is invalid")
    try:
        if not isinstance(value.active_heads, tuple) or not all(
            isinstance(head, HeadRef) for head in value.active_heads
        ):
            raise InvalidArtifact("workstream active_heads must be HeadRef values")
        if value.preferred_head is not None and not isinstance(
            value.preferred_head, HeadRef
        ):
            raise InvalidArtifact("workstream preferred_head must be a HeadRef or None")
        if not isinstance(value.policy_refs, tuple) or not all(
            isinstance(policy_ref, PolicyRef) for policy_ref in value.policy_refs
        ):
            raise InvalidArtifact("workstream policy_refs must be PolicyRef values")

        heads = tuple(
            HeadRef(head.session, head.revision) for head in value.active_heads
        )
        preferred = None
        if value.preferred_head is not None:
            preferred_value = HeadRef(
                value.preferred_head.session, value.preferred_head.revision
            )
            preferred = next(
                (head for head in heads if head == preferred_value), preferred_value
            )
        policy_refs = tuple(
            PolicyRef(ref.policy_id, ref.revision, ref.digest)
            for ref in value.policy_refs
        )
        return WorkstreamRegistry(
            value.id,
            value.generation,
            value.project,
            value.status,
            value.mode,
            heads,
            preferred,
            policy_refs,
            value.overlay_of,
        )
    except InvalidArtifact:
        raise
    except Exception as exc:
        raise InvalidArtifact("workstream registry is invalid") from exc


class RegistryStore:
    """Registry writer which routes every document through one storage class."""

    def __init__(
        self, vault: object, storage_class: StorageClass = StorageClass.PORTABLE
    ) -> None:
        if not isinstance(storage_class, StorageClass):
            raise InvalidArtifact("registry store requires an explicit StorageClass")
        self._vault = vault
        self.storage_class = storage_class

    @admission_mutation
    def create_workstream(
        self,
        workstream_id: str,
        *,
        project: str | None,
        mode: str,
        policy_refs: tuple[PolicyRef, ...] = (),
        overlay_of: str | None = None,
    ) -> WorkstreamRegistry:
        registry = WorkstreamRegistry(
            id=workstream_id,
            generation=0,
            project=project,
            status="active",
            mode=mode,
            policy_refs=policy_refs,
            overlay_of=overlay_of,
        )
        self._validate_project_reference(registry.project)
        self._validate_workstream_policies(registry)
        self._vault.artifacts.write_new(
            ArtifactFamily.REGISTRY,
            storage_class=self.storage_class,
            relative_path=self._workstream_path(workstream_id),
            document=self._workstream_document(registry),
        )
        return registry

    @admission_mutation
    def register_project(
        self,
        project_id: str,
        *,
        locator: str | None = None,
        authority: str = "project",
    ) -> ProjectRegistry:
        _validate_locator(locator, self.storage_class)
        registry = ProjectRegistry(project_id, 0, authority, locator)
        self._vault.artifacts.write_new(
            ArtifactFamily.REGISTRY,
            storage_class=self.storage_class,
            relative_path=self._project_path(project_id),
            document=self._project_document(registry),
        )
        return registry

    @admission_mutation
    def register_source(
        self,
        source_id: str,
        *,
        kind: str = "filesystem",
        authority: str = "project",
        project: str | None = None,
        locator: str | None = None,
    ) -> SourceRegistry:
        _validate_locator(locator, self.storage_class)
        registry = SourceRegistry(source_id, 0, kind, authority, project, locator)
        self._validate_project_reference(registry.project)
        self._vault.artifacts.write_new(
            ArtifactFamily.REGISTRY,
            storage_class=self.storage_class,
            relative_path=self._source_path(source_id),
            document=self._source_document(registry),
        )
        return registry

    def load_project(self, project_id: str) -> ProjectRegistry:
        validate_identifier(project_id, label="project id")
        document = self._vault.reader.read(
            ArtifactFamily.REGISTRY,
            storage_class=self.storage_class,
            relative_path=self._project_path(project_id),
        )
        return self._parse_project(document.metadata, project_id)

    def load_source(self, source_id: str) -> SourceRegistry:
        validate_identifier(source_id, label="source id")
        document = self._vault.reader.read(
            ArtifactFamily.REGISTRY,
            storage_class=self.storage_class,
            relative_path=self._source_path(source_id),
        )
        registry = self._parse_source(document.metadata, source_id)
        self._validate_project_reference(registry.project)
        return registry

    @admission_mutation
    def assign_project_policy(
        self, project_id: str, policy_ref: PolicyRef, *, expected_generation: int
    ) -> ProjectRegistry:
        _validate_expected_generation(expected_generation)
        self._validate_assignable_policy(policy_ref)
        current = self.load_project(project_id)
        if expected_generation != current.generation:
            raise RegistryConflict("project generation is stale")
        updated = replace(current, generation=current.generation + 1, policy_ref=policy_ref)
        try:
            self._vault.artifacts.write_cas(
                ArtifactFamily.REGISTRY,
                storage_class=self.storage_class,
                relative_path=self._project_path(project_id),
                document=self._project_document(updated, control=True),
                expected_generation=expected_generation,
            )
        except ConcurrentWrite as exc:
            raise RegistryConflict("project generation is stale") from exc
        return updated

    @admission_mutation
    def assign_source_policy(
        self, source_id: str, policy_ref: PolicyRef, *, expected_generation: int
    ) -> SourceRegistry:
        _validate_expected_generation(expected_generation)
        self._validate_assignable_policy(policy_ref)
        current = self.load_source(source_id)
        if expected_generation != current.generation:
            raise RegistryConflict("source generation is stale")
        updated = replace(current, generation=current.generation + 1, policy_ref=policy_ref)
        try:
            self._vault.artifacts.write_cas(
                ArtifactFamily.REGISTRY,
                storage_class=self.storage_class,
                relative_path=self._source_path(source_id),
                document=self._source_document(updated, control=True),
                expected_generation=expected_generation,
            )
        except ConcurrentWrite as exc:
            raise RegistryConflict("source generation is stale") from exc
        return updated

    def bind_source(self, source_id: str, path: str | Path):
        """Store an absolute machine-local checkout binding, never a registry field."""
        from mneme.core.sources.registry import SourceBindingStore

        validate_identifier(source_id, label="source id")
        return SourceBindingStore(self._vault).bind(source_id, path)

    def add_active_head(
        self,
        workstream_id: str,
        head: HeadRef,
        observed_base: WorkstreamRegistry,
        head_validator: Callable[[HeadRef], object],
    ) -> WorkstreamRegistry:
        """CAS-add one independently validated head, with a bounded structural retry."""
        validate_identifier(workstream_id, label="workstream id")
        if not isinstance(head, HeadRef):
            raise InvalidArtifact("active head must be a HeadRef")
        if not isinstance(observed_base, WorkstreamRegistry):
            raise InvalidArtifact("observed_base must be an immutable WorkstreamRegistry")
        if observed_base.id != workstream_id:
            raise RegistryConflict("observed base names a different workstream")
        if not callable(head_validator):
            raise InvalidArtifact("head_validator must be callable")
        if head in observed_base.active_heads or any(
            existing.session == head.session for existing in observed_base.active_heads
        ):
            raise RegistryConflict("requested head is not a distinct session addition")

        for _attempt in range(3):
            self._validate_head(head, head_validator)
            current = self.load_workstream(workstream_id)
            if current.generation < observed_base.generation or not self._is_disjoint_addition(
                observed_base, current, head
            ):
                raise RegistryConflict("workstream changed outside proven-disjoint heads")
            if head in current.active_heads:
                return current
            candidate = replace(current, active_heads=current.active_heads + (head,))
            try:
                return self.update_workstream(
                    candidate, expected_generation=current.generation
                )
            except RegistryConflict:
                continue
        raise RegistryConflict("workstream head addition exhausted three CAS attempts")

    def load_workstream(self, workstream_id: str) -> WorkstreamRegistry:
        validate_identifier(workstream_id, label="workstream id")
        document = self._vault.reader.read(
            ArtifactFamily.REGISTRY,
            storage_class=self.storage_class,
            relative_path=self._workstream_path(workstream_id),
        )
        registry = self._parse_workstream(document.metadata, workstream_id)
        self._validate_project_reference(registry.project)
        self._validate_workstream_policies(registry)
        return registry

    @admission_mutation
    def update_workstream(
        self, registry: WorkstreamRegistry, *, expected_generation: int
    ) -> WorkstreamRegistry:
        if not isinstance(registry, WorkstreamRegistry):
            raise InvalidArtifact("workstream update requires a WorkstreamRegistry")
        _validate_expected_generation(expected_generation)
        if expected_generation != registry.generation:
            raise RegistryConflict("workstream generation does not match CAS base")
        self._validate_project_reference(registry.project)
        self._validate_workstream_policies(registry)
        current = self.load_workstream(registry.id)
        if current.generation != expected_generation:
            raise RegistryConflict("workstream generation is stale")
        updated = replace(registry, generation=expected_generation + 1)
        control = self._is_restrictive_workstream_control(current, updated)
        try:
            self._vault.artifacts.write_cas(
                ArtifactFamily.REGISTRY,
                storage_class=self.storage_class,
                relative_path=self._workstream_path(registry.id),
                document=self._workstream_document(updated, control=control),
                expected_generation=expected_generation,
            )
        except ConcurrentWrite as exc:
            raise RegistryConflict("workstream generation is stale") from exc
        return updated

    @staticmethod
    def _workstream_path(workstream_id: str) -> str:
        validate_identifier(workstream_id, label="workstream id")
        return f"workstreams/{workstream_id}/workstream.yaml"

    @staticmethod
    def _project_path(project_id: str) -> str:
        validate_identifier(project_id, label="project id")
        return f"projects/{project_id}.yaml"

    @staticmethod
    def _source_path(source_id: str) -> str:
        validate_identifier(source_id, label="source id")
        return f"sources/{source_id}.yaml"

    def _project_document(self, registry: ProjectRegistry, *, control: bool = False) -> ArtifactDocument:
        _validate_locator(registry.locator, self.storage_class)
        return self._admit_document(ArtifactDocument(
            metadata={
                "schema": _PROJECT_SCHEMA,
                "id": registry.id,
                "generation": registry.generation,
                "authority": registry.authority,
                "locator": registry.locator,
                "policy": self._serialize_policy_ref(registry.policy_ref),
            },
            references=self._policy_manifest(registry.policy_ref),
        ), self._project_path(registry.id), (registry.policy_ref,), control=control)

    def _source_document(self, registry: SourceRegistry, *, control: bool = False) -> ArtifactDocument:
        _validate_locator(registry.locator, self.storage_class)
        self._validate_project_reference(registry.project)
        refs = [registry.policy_ref]
        if registry.project is not None and self.storage_class is StorageClass.PORTABLE:
            refs.append(self.load_project(registry.project).policy_ref)
        return self._admit_document(ArtifactDocument(
            metadata={
                "schema": _SOURCE_SCHEMA,
                "id": registry.id,
                "generation": registry.generation,
                "kind": registry.kind,
                "authority": registry.authority,
                "project": registry.project,
                "locator": registry.locator,
                "policy": self._serialize_policy_ref(registry.policy_ref),
            },
            references=self._policy_manifest(registry.policy_ref),
        ), self._source_path(registry.id), tuple(refs), control=control)

    def _workstream_document(
        self, registry: WorkstreamRegistry, *, control: bool = False
    ) -> ArtifactDocument:
        self._validate_project_reference(registry.project)
        if registry.overlay_of is not None:
            if self.storage_class is not StorageClass.LOCAL_ONLY:
                raise InvalidArtifact("portable registry cannot declare an overlay")
            RegistryStore(self._vault).load_workstream(registry.overlay_of)
        metadata: dict[str, object] = {
            "schema": _WORKSTREAM_SCHEMA,
            "id": registry.id,
            "generation": registry.generation,
            "project": registry.project,
            "status": registry.status,
            "resolution": {
                "mode": registry.mode,
                "preferred_head": (
                    None
                    if registry.preferred_head is None
                    else {
                        "session": registry.preferred_head.session,
                        "revision": registry.preferred_head.revision,
                    }
                ),
            },
            "active_heads": [
                {"session": head.session, "revision": head.revision}
                for head in sorted(registry.active_heads, key=lambda item: item.session)
            ],
            "policy_refs": [
                RegistryStore._serialize_policy_ref(policy_ref)
                for policy_ref in sorted(registry.policy_refs, key=lambda item: item.policy_id)
            ],
        }
        references = tuple(
            ArtifactReference(ReferenceKind.ID, self.storage_class, policy_ref.policy_id)
            for policy_ref in registry.policy_refs
        )
        if registry.overlay_of is not None:
            metadata["overlay_of"] = registry.overlay_of
        refs = list(registry.policy_refs)
        if registry.project is not None and self.storage_class is StorageClass.PORTABLE:
            refs.append(self.load_project(registry.project).policy_ref)
        return self._admit_document(ArtifactDocument(
            metadata=metadata, references=ReferenceManifest.complete(metadata=references)
        ), self._workstream_path(registry.id), tuple(refs), control=control)

    def _is_restrictive_workstream_control(
        self, current: WorkstreamRegistry, updated: WorkstreamRegistry
    ) -> bool:
        if self.storage_class is StorageClass.LOCAL_ONLY:
            return False
        if (
            current.id != updated.id
            or current.status != updated.status
            or current.mode != updated.mode
            or current.active_heads != updated.active_heads
            or current.preferred_head != updated.preferred_head
            or current.overlay_of != updated.overlay_of
            or (current.project, current.policy_refs)
            == (updated.project, updated.policy_refs)
        ):
            return False
        before = self._workstream_policy_evaluation(current)
        after = self._workstream_policy_evaluation(updated)
        return after.effective_ceiling <= before.effective_ceiling

    def _workstream_policy_evaluation(self, registry: WorkstreamRegistry):
        policies = PolicyStore(self._vault)
        refs = [policies.load_active("vault-default", StorageClass.PORTABLE)]
        refs.extend(registry.policy_refs)
        if registry.project is not None:
            project = self.load_project(registry.project)
            if project.policy_ref is not None:
                refs.append(project.policy_ref)
        return evaluate_portability(
            Portability.PERSONAL_VAULT,
            tuple(policies.load_rule(reference) for reference in refs),
            "0" * 64,
        )

    def _admit_document(self, document: ArtifactDocument, path: str, refs: tuple, *, control: bool = False) -> ArtifactDocument:
        """Evaluate every new portable disclosure and preserve prior receipts.

        Policy assignments are administrative control changes: recording a more
        restrictive ceiling must remain possible even when it withholds identity.
        Their denied evaluation is retained as an explicit control receipt.
        """
        if self.storage_class is StorageClass.LOCAL_ONLY:
            return document
        from mneme.core.memories import _receipt_dict
        from mneme.core.policy import Portability, evaluate_portability

        policies = PolicyStore(self._vault)
        default = policies.load_active("vault-default", StorageClass.PORTABLE)
        rules = tuple(policies.load_rule(ref) for ref in (default, *refs) if ref is not None)
        evaluation = evaluate_portability(Portability.PERSONAL_VAULT, rules, _registry_hash(document.metadata))
        if not control and not evaluation.allowed:
            raise InvalidArtifact("registry disclosure exceeds current effective policy")
        history = []
        location = self._vault.router.location(ArtifactFamily.REGISTRY, self.storage_class, path)
        if location.path.exists():
            prior = self._vault.reader.read(ArtifactFamily.REGISTRY, location=location)
            _strip_admission(prior.metadata, policies)
            history = list(prior.metadata.get("admission_receipts", ()))
        receipt = _receipt_dict(evaluation)
        receipt["control"] = control
        return replace(document, metadata={**document.metadata, "admission_receipts": [*history, receipt]})

    def _parse_workstream(self, metadata: object, workstream_id: str) -> WorkstreamRegistry:
        metadata = _strip_admission(metadata, PolicyStore(self._vault))
        overlay_of = metadata.get("overlay_of") if isinstance(metadata, dict) else None
        if isinstance(metadata, dict) and "overlay_of" in metadata:
            metadata = {key: value for key, value in metadata.items() if key != "overlay_of"}
        if not isinstance(metadata, dict) or set(metadata) != {
            "schema",
            "id",
            "generation",
            "project",
            "status",
            "resolution",
            "active_heads",
            "policy_refs",
        }:
            raise InvalidArtifact("workstream registry has an invalid schema")
        if metadata.get("schema") != _WORKSTREAM_SCHEMA or metadata.get("id") != workstream_id:
            raise InvalidArtifact("workstream registry identity does not match its path")
        resolution = metadata.get("resolution")
        if not isinstance(resolution, dict) or set(resolution) != {
            "mode",
            "preferred_head",
        }:
            raise InvalidArtifact("workstream resolution has an invalid schema")
        heads = metadata.get("active_heads")
        if not isinstance(heads, list):
            raise InvalidArtifact("workstream active_heads must be a list")
        policy_refs = metadata.get("policy_refs")
        if not isinstance(policy_refs, list):
            raise InvalidArtifact("workstream policy_refs must be a list")
        parsed_heads = tuple(RegistryStore._parse_head(value) for value in heads)
        raw_preferred = resolution.get("preferred_head")
        preferred = (
            None if raw_preferred is None else RegistryStore._parse_head(raw_preferred)
        )
        return WorkstreamRegistry(
            id=workstream_id,
            generation=metadata.get("generation"),
            project=metadata.get("project"),
            status=metadata.get("status"),
            mode=resolution.get("mode"),
            active_heads=parsed_heads,
            preferred_head=preferred,
            policy_refs=tuple(RegistryStore._parse_policy_ref(value) for value in policy_refs),
            overlay_of=overlay_of,
        )

    @staticmethod
    def _parse_head(value: object) -> HeadRef:
        if not isinstance(value, dict) or set(value) != {"session", "revision"}:
            raise InvalidArtifact("workstream head has an invalid schema")
        return HeadRef(value.get("session"), value.get("revision"))

    @staticmethod
    def _validate_head(head: HeadRef, validator: Callable[[HeadRef], object]) -> None:
        try:
            result = validator(head)
        except Exception as exc:
            raise RegistryConflict(
                "referenced session revision is invalid", code="invalid-head"
            ) from exc
        if result is not True:
            raise RegistryConflict(
                "referenced session revision is invalid", code="invalid-head"
            )

    @staticmethod
    def _is_disjoint_addition(
        observed_base: WorkstreamRegistry,
        current: WorkstreamRegistry,
        requested: HeadRef,
    ) -> bool:
        """Check the D3 structural-retry predicates without semantic interpretation."""
        if (
            current.id != observed_base.id
            or current.project != observed_base.project
            or current.status != observed_base.status
            or current.mode != observed_base.mode
            or current.preferred_head != observed_base.preferred_head
            or current.policy_refs != observed_base.policy_refs
            or current.overlay_of != observed_base.overlay_of
        ):
            return False
        base_heads = set(observed_base.active_heads)
        current_heads = set(current.active_heads)
        if not base_heads.issubset(current_heads):
            return False
        return not any(
            candidate.session == requested.session and candidate != requested
            for candidate in current.active_heads
        )

    @staticmethod
    def _serialize_policy_ref(policy_ref: PolicyRef | None) -> dict[str, str] | None:
        if policy_ref is None:
            return None
        return {
            "policy_id": policy_ref.policy_id,
            "revision": policy_ref.revision,
            "digest": policy_ref.digest,
        }

    def _policy_manifest(self, policy_ref: PolicyRef | None) -> ReferenceManifest:
        references = ()
        if policy_ref is not None:
            references = (
                ArtifactReference(
                    ReferenceKind.ID, self.storage_class, policy_ref.policy_id
                ),
            )
        return ReferenceManifest.complete(metadata=references)

    def _validate_assignable_policy(self, policy_ref: object) -> None:
        if not isinstance(policy_ref, PolicyRef):
            raise InvalidArtifact("policy assignment requires a PolicyRef")
        if PolicyStore(self._vault)._storage_class_for_ref(policy_ref) is not self.storage_class:
            raise InvalidArtifact("registry policy reference crosses storage classes")

    def _validate_workstream_policies(self, registry: WorkstreamRegistry) -> None:
        if registry.overlay_of is not None:
            if self.storage_class is not StorageClass.LOCAL_ONLY:
                raise InvalidArtifact("portable registry cannot declare an overlay")
            RegistryStore(self._vault).load_workstream(registry.overlay_of)
        for policy_ref in registry.policy_refs:
            self._validate_assignable_policy(policy_ref)

    def _validate_project_reference(self, project_id: str | None) -> None:
        """Resolve project IDs directionally: local first, portable fallback only."""
        if project_id is None:
            return
        validate_identifier(project_id, label="project id")
        if self.storage_class is StorageClass.PORTABLE:
            self.load_project(project_id)
            return
        try:
            self.load_project(project_id)
        except InvalidArtifact as local_error:
            try:
                RegistryStore(self._vault, StorageClass.PORTABLE).load_project(project_id)
            except InvalidArtifact:
                raise local_error

    @staticmethod
    def _parse_policy_ref(value: object) -> PolicyRef | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"policy_id", "revision", "digest"}:
            raise InvalidArtifact("registry policy reference has an invalid schema")
        return PolicyRef(value.get("policy_id"), value.get("revision"), value.get("digest"))

    def _parse_project(self, metadata: object, project_id: str) -> ProjectRegistry:
        metadata = _strip_admission(metadata, PolicyStore(self._vault))
        if not isinstance(metadata, dict) or set(metadata) != {
            "schema", "id", "generation", "authority", "locator", "policy"
        }:
            raise InvalidArtifact("project registry has an invalid schema")
        if metadata.get("schema") != _PROJECT_SCHEMA or metadata.get("id") != project_id:
            raise InvalidArtifact("project registry identity does not match its path")
        _validate_locator(metadata.get("locator"), self.storage_class)
        return ProjectRegistry(
            project_id,
            metadata.get("generation"),
            metadata.get("authority"),
            metadata.get("locator"),
            RegistryStore._parse_policy_ref(metadata.get("policy")),
        )

    def _parse_source(self, metadata: object, source_id: str) -> SourceRegistry:
        metadata = _strip_admission(metadata, PolicyStore(self._vault))
        if not isinstance(metadata, dict) or set(metadata) != {
            "schema", "id", "generation", "kind", "authority", "project", "locator", "policy"
        }:
            raise InvalidArtifact("source registry has an invalid schema")
        if metadata.get("schema") != _SOURCE_SCHEMA or metadata.get("id") != source_id:
            raise InvalidArtifact("source registry identity does not match its path")
        _validate_locator(metadata.get("locator"), self.storage_class)
        return SourceRegistry(
            source_id,
            metadata.get("generation"),
            metadata.get("kind"),
            metadata.get("authority"),
            metadata.get("project"),
            metadata.get("locator"),
            RegistryStore._parse_policy_ref(metadata.get("policy")),
        )


def _registry_hash(metadata: object) -> str:
    return sha256(json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _strip_admission(metadata: object, policies: PolicyStore) -> object:
    if not isinstance(metadata, dict) or "admission_receipts" not in metadata:
        return metadata  # Existing v1 registries remain readable.
    from mneme.core.memories import _parse_receipt

    history = metadata["admission_receipts"]
    if not isinstance(history, list) or not history:
        raise InvalidArtifact("registry admission receipts are invalid")
    for item in history:
        if not isinstance(item, dict) or type(item.get("control")) is not bool:
            raise InvalidArtifact("registry admission control marker is invalid")
        receipt = _parse_receipt({key: value for key, value in item.items() if key != "control"})
        if not receipt.allowed and not item["control"]:
            raise InvalidArtifact("registry admission policy denied disclosure")
        for reference in receipt.refs:
            if isinstance(reference, PolicyRef):
                policies.load_rule(reference)
    semantic = {key: value for key, value in metadata.items() if key != "admission_receipts"}
    if receipt.semantic_hash != _registry_hash(semantic):
        raise InvalidArtifact("registry admission receipt does not bind content")
    return semantic


def _validate_generation(value: object, *, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidArtifact(f"{label} generation must be a non-negative integer")


def _validate_expected_generation(value: object) -> None:
    _validate_generation(value, label="expected")


def _validate_locator(value: str | None, storage_class: StorageClass) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidArtifact("registry locator must be non-empty text without whitespace")
    if "\x00" in value or any(character.isspace() for character in value):
        raise InvalidArtifact("registry locator contains unsafe whitespace")
    if storage_class is StorageClass.LOCAL_ONLY:
        return
    parsed = urlsplit(value)
    windows = PureWindowsPath(value)
    if (
        Path(value).is_absolute()
        or windows.is_absolute()
        or windows.drive
        or value.startswith(("~", "$", "%"))
        or parsed.scheme == "file"
    ):
        raise InvalidArtifact("portable registry locator cannot be a machine path")
    if parsed.username is not None or parsed.password is not None or _CREDENTIAL_WORD.search(value):
        raise InvalidArtifact("portable registry locator cannot contain credentials")
    if _CONFIDENTIAL_MARKER.search(value):
        raise InvalidArtifact("portable registry locator cannot disclose confidential markers")
