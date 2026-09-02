"""Candidate and accepted Memory records with policy-bound lifecycle writes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from mneme.core.artifacts import (
    ArtifactDocument,
    ArtifactFamily,
    ArtifactReference,
    ReferenceKind,
    ReferenceManifest,
    StorageClass,
)
from mneme.core.context import ContextView
from mneme.core.errors import ConcurrentWrite, InvalidArtifact, PortabilityViolation
from mneme.core.fs import normalize_text
from mneme.core.policy import (
    ApprovedRuleProvenance,
    OpaquePolicyAttestation,
    PolicyEvaluation,
    PolicyReceiptRef,
    PolicyRef,
    PolicyRule,
    PolicyStore,
    PolicyViolation,
    Portability,
    evaluate_portability,
    policy_admission_gate,
)
from mneme.core.resolver import ResolutionState, ResolvedWorkstream
from mneme.core.registries import RegistryStore
from mneme.core.validation.vault import validate_identifier


_SCHEMA = "madi.memory.v1"


class MemoryKind(str, Enum):
    DECISION = "decision"
    LESSON = "lesson"
    PREFERENCE = "preference"
    KNOWLEDGE = "knowledge"


class MemoryStatus(str, Enum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    RETIRED = "retired"


class MemoryAuthority(str, Enum):
    PERSONAL = "personal"
    EXTERNAL_REFERENCE = "external-reference"


class MemoryScopeType(str, Enum):
    PERSONAL_GLOBAL = "personal-global"
    PROJECT = "project"
    WORKSTREAM = "workstream"


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """The independently validated applicability axis for a Memory."""

    type: MemoryScopeType
    project_id: str | None = None
    workstream_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, MemoryScopeType):
            raise InvalidArtifact("memory scope type is invalid")
        if self.type is MemoryScopeType.PERSONAL_GLOBAL:
            if self.project_id is not None or self.workstream_id is not None:
                raise InvalidArtifact("personal-global scope cannot name a project or workstream")
        elif self.type is MemoryScopeType.PROJECT:
            validate_identifier(self.project_id, label="memory project id")
            if self.workstream_id is not None:
                raise InvalidArtifact("project memory scope cannot name a workstream")
        else:
            validate_identifier(self.workstream_id, label="memory workstream id")
            if self.project_id is not None:
                raise InvalidArtifact("workstream memory scope cannot name a project")

    @classmethod
    def parse(cls, value: object) -> MemoryScope:
        if isinstance(value, MemoryScope):
            return cls(value.type, value.project_id, value.workstream_id)
        if not isinstance(value, Mapping) or not isinstance(value.get("type"), str):
            raise InvalidArtifact("memory scope must be a typed mapping")
        try:
            kind = MemoryScopeType(value["type"])
        except ValueError as exc:
            raise InvalidArtifact("memory scope type is invalid") from exc
        expected = {
            MemoryScopeType.PERSONAL_GLOBAL: {"type"},
            MemoryScopeType.PROJECT: {"type", "project_id"},
            MemoryScopeType.WORKSTREAM: {"type", "workstream_id"},
        }[kind]
        if set(value) != expected:
            raise InvalidArtifact("memory scope has an invalid schema")
        return cls(kind, value.get("project_id"), value.get("workstream_id"))

    def as_dict(self) -> dict[str, str]:
        result = {"type": self.type.value}
        if self.project_id is not None:
            result["project_id"] = self.project_id
        if self.workstream_id is not None:
            result["workstream_id"] = self.workstream_id
        return result


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    kind: MemoryKind
    status: MemoryStatus
    scope: MemoryScope
    authority: MemoryAuthority
    portability: Portability
    body: str
    rationale: str
    provenance: tuple[ArtifactReference, ...]
    supersedes: str | None
    generation: int
    semantic_hash: str
    policy_receipt: PolicyEvaluation
    acceptance_baseline: str | None = None
    receipt_history: tuple[PolicyEvaluation, ...] = ()
    retired_at: datetime | None = None
    retirement_reason: str | None = None
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.id, label="memory id")
        if not isinstance(self.kind, MemoryKind) or not isinstance(self.status, MemoryStatus):
            raise InvalidArtifact("memory kind and status must use their independent axes")
        if not isinstance(self.scope, MemoryScope) or not isinstance(self.authority, MemoryAuthority):
            raise InvalidArtifact("memory scope and authority must use their independent axes")
        if not isinstance(self.portability, Portability):
            raise InvalidArtifact("memory portability must use Portability")
        if not isinstance(self.body, str) or not self.body.strip() or self.body != normalize_text(self.body):
            raise InvalidArtifact("memory body must be canonical normalized text")
        if not isinstance(self.rationale, str) or self.rationale != self.rationale.strip():
            raise InvalidArtifact("memory rationale must be trimmed text")
        if not isinstance(self.provenance, tuple) or not all(isinstance(item, ArtifactReference) for item in self.provenance):
            raise InvalidArtifact("memory provenance must be typed artifact references")
        if self.supersedes is not None:
            validate_identifier(self.supersedes, label="superseded memory id")
            if self.supersedes == self.id:
                raise InvalidArtifact("memory cannot supersede itself")
        _generation(self.generation)
        _digest(self.semantic_hash, "memory semantic hash")
        if not isinstance(self.policy_receipt, PolicyEvaluation):
            raise InvalidArtifact("memory policy receipt is invalid")
        if self.status is MemoryStatus.CANDIDATE:
            if self.acceptance_baseline is not None:
                raise InvalidArtifact("candidate memory cannot have an acceptance baseline")
        else:
            _digest(self.acceptance_baseline, "memory acceptance baseline")
            if self.acceptance_baseline != self.semantic_hash:
                raise InvalidArtifact("accepted semantic fields differ from the immutable acceptance baseline")
        if not isinstance(self.receipt_history, tuple) or not all(isinstance(item, PolicyEvaluation) for item in self.receipt_history):
            raise InvalidArtifact("memory receipt history is invalid")
        if self.retired_at is None:
            if self.retirement_reason is not None:
                raise InvalidArtifact("active memory cannot have a retirement reason")
        else:
            _timestamp(self.retired_at)
            _text(self.retirement_reason, "retirement reason")
            if self.status is not MemoryStatus.RETIRED:
                raise InvalidArtifact("only retired memory can have retirement envelope")
        if self.superseded_by is not None:
            validate_identifier(self.superseded_by, label="superseding memory id")


@dataclass(frozen=True, slots=True)
class MemoryReaders:
    """Storage-bound reader seam for PROFILE generation; it has no write hook."""

    store: MemoryStore

    def __post_init__(self) -> None:
        if not isinstance(self.store, MemoryStore):
            raise TypeError("PROFILE requires a storage-bound MemoryStore reader")

    @property
    def storage_class(self) -> StorageClass:
        return self.store.storage_class

    def load_records(self) -> tuple[MemoryRecord, ...]:
        return self.store.iter_records()


class MemoryStore:
    """Canonical memory lifecycle store sharing ArtifactStore's codec and CAS."""

    def __init__(self, vault: object, storage_class: StorageClass = StorageClass.PORTABLE, *, clock: Callable[[], datetime] | None = None):
        if not isinstance(storage_class, StorageClass):
            raise InvalidArtifact("memory store requires an explicit StorageClass")
        self.vault = vault
        self.storage_class = storage_class
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not callable(self._clock):
            raise InvalidArtifact("memory clock must be callable")

    def submit_candidate(
        self, kind: MemoryKind | str, scope: MemoryScope | Mapping[str, object], authority: MemoryAuthority | str,
        portability: Portability | str, body: str, receipt: PolicyEvaluation, *, rationale: str = "",
        provenance: tuple[ArtifactReference, ...] = (), memory_id: str | None = None, supersedes: str | None = None,
    ) -> MemoryRecord:
        record = self._record(
            memory_id or f"mem-{uuid4().hex}", kind, MemoryStatus.CANDIDATE, scope, authority, portability,
            body, rationale, provenance, supersedes, 0, receipt, None, (), None, None,
        )
        with policy_admission_gate(self.vault):
            self._validate_admission(record, receipt)
            self.vault.artifacts.write_new(ArtifactFamily.MEMORY, storage_class=self.storage_class,
                relative_path=self._path(record.id), document=self._document(record))
        return record

    def edit_candidate(self, memory_id: str, expected_generation: int, *, body: str | None = None,
                       rationale: str | None = None, scope: MemoryScope | Mapping[str, object] | None = None,
                       authority: MemoryAuthority | str | None = None, portability: Portability | str | None = None,
                       provenance: tuple[ArtifactReference, ...] | None = None, receipt: PolicyEvaluation) -> MemoryRecord:
        _generation(expected_generation)
        with policy_admission_gate(self.vault):
            current = self.read(memory_id)
            if current.status is not MemoryStatus.CANDIDATE:
                raise InvalidArtifact("only candidate memories are editable")
            updated = self._record(current.id, current.kind, current.status, scope or current.scope,
                authority or current.authority, portability or current.portability, body if body is not None else current.body,
                rationale if rationale is not None else current.rationale, provenance if provenance is not None else current.provenance,
                current.supersedes, expected_generation + 1, receipt, None, current.receipt_history + (current.policy_receipt,), None, None)
            self._validate_admission(updated, receipt)
            self._write_cas(updated, expected_generation)
        return updated

    def promote(self, memory_id: str, expected_generation: int, receipt: PolicyEvaluation) -> MemoryRecord:
        _generation(expected_generation)
        with policy_admission_gate(self.vault):
            current = self.read(memory_id)
            if current.status is not MemoryStatus.CANDIDATE:
                raise InvalidArtifact("only candidate memories can be promoted")
            accepted = replace(current, status=MemoryStatus.ACCEPTED, generation=expected_generation + 1,
                               policy_receipt=receipt, acceptance_baseline=current.semantic_hash,
                               receipt_history=current.receipt_history + (current.policy_receipt,))
            self._validate_admission(accepted, receipt)
            self._write_cas(accepted, expected_generation)
        return accepted

    def supersede(self, memory_id: str, expected_generation: int, *, body: str,
                  receipt: PolicyEvaluation, rationale: str | None = None,
                  scope: MemoryScope | Mapping[str, object] | None = None,
                  authority: MemoryAuthority | str | None = None,
                  portability: Portability | str | None = None,
                  provenance: tuple[ArtifactReference, ...] | None = None) -> MemoryRecord:
        _generation(expected_generation)
        with policy_admission_gate(self.vault):
            prior = self.read(memory_id)
            if prior.status is not MemoryStatus.ACCEPTED or prior.generation != expected_generation:
                raise InvalidArtifact("supersession requires the exact accepted memory generation")
            if self._derived_successor(prior.id) is not None:
                raise InvalidArtifact("accepted memory already has a reserved successor")
            successor = self._record(
                f"mem-{uuid4().hex}", prior.kind, MemoryStatus.CANDIDATE, scope or prior.scope,
                authority or prior.authority, portability or prior.portability, body,
                prior.rationale if rationale is None else rationale,
                prior.provenance if provenance is None else provenance, prior.id, 0, receipt, None, (), None, None,
            )
            self._validate_admission(successor, receipt)
            self.vault.artifacts.write_new(
                ArtifactFamily.MEMORY, storage_class=self.storage_class,
                relative_path=self._path(successor.id), document=self._document(successor),
            )
            return successor

    def retire(self, memory_id: str, expected_generation: int, reason: str, *, privacy_remediation: bool = False) -> MemoryRecord:
        _generation(expected_generation)
        _text(reason, "retirement reason")
        with policy_admission_gate(self.vault):
            current = self.read(memory_id)
            if current.generation != expected_generation:
                raise ConcurrentWrite(
                    f"stale memory generation: expected {expected_generation}, found {current.generation}"
                )
            if current.status is MemoryStatus.RETIRED:
                raise InvalidArtifact("memory is already retired")
            wording = reason
            if privacy_remediation:
                wording = f"Privacy remediation: {reason}. Prior Git propagation cannot be erased."
            retired = replace(current, status=MemoryStatus.RETIRED, generation=expected_generation + 1,
                              retired_at=_canonical_timestamp(self._clock()), retirement_reason=wording)
            self._write_cas(retired, expected_generation)
        return retired

    def privacy_retire(self, memory_id: str, expected_generation: int, reason: str) -> MemoryRecord:
        """Logical/tombstone retirement; this API deliberately never rewrites Git history."""
        return self.retire(memory_id, expected_generation, reason, privacy_remediation=True)

    def read(self, memory_id: str) -> MemoryRecord:
        validate_identifier(memory_id, label="memory id")
        record = self._read_without_links(memory_id)
        return replace(record, superseded_by=self._derived_successor(record.id))

    def _read_without_links(self, memory_id: str) -> MemoryRecord:
        location = self.vault.router.location(ArtifactFamily.MEMORY, self.storage_class, self._path(memory_id))
        if location.path.is_symlink():
            raise InvalidArtifact("memory artifact cannot be a symbolic link")
        document = self.vault.reader.read(ArtifactFamily.MEMORY, location=location)
        record = self._parse(document, memory_id)
        if record.status is MemoryStatus.ACCEPTED:
            self._validate_historic_receipt(record)
        return record

    def iter_records(self) -> tuple[MemoryRecord, ...]:
        directory = self.vault.router.location(ArtifactFamily.MEMORY, self.storage_class, "memory/placeholder.md").path.parent
        if directory.is_symlink() or not directory.is_dir():
            raise InvalidArtifact("memory directory is unsafe")
        records: list[MemoryRecord] = []
        for item in directory.iterdir():
            if item.is_symlink() or not item.is_file() or item.suffix != ".md":
                raise InvalidArtifact("memory directory contains an invalid artifact")
            validate_identifier(item.stem, label="memory id")
            records.append(self._read_without_links(item.stem))
        links = {record.supersedes: record.id for record in records if record.supersedes is not None}
        if len(links) != len([record for record in records if record.supersedes is not None]):
            raise InvalidArtifact("memory has multiple unresolved successors")
        return tuple(sorted((replace(record, superseded_by=links.get(record.id)) for record in records), key=lambda record: record.id))

    def _record(self, memory_id: str, kind: MemoryKind | str, status: MemoryStatus, scope: MemoryScope | Mapping[str, object],
                authority: MemoryAuthority | str, portability: Portability | str, body: str, rationale: str,
                provenance: tuple[ArtifactReference, ...], supersedes: str | None, generation: int,
                receipt: PolicyEvaluation, acceptance_baseline: str | None, history: tuple[PolicyEvaluation, ...], retired_at: datetime | None,
                retirement_reason: str | None) -> MemoryRecord:
        try:
            parsed_kind = kind if isinstance(kind, MemoryKind) else MemoryKind(kind)
            parsed_authority = authority if isinstance(authority, MemoryAuthority) else MemoryAuthority(authority)
            parsed_portability = portability if isinstance(portability, Portability) else Portability(portability)
        except (TypeError, ValueError) as exc:
            raise InvalidArtifact("memory axis value is invalid") from exc
        parsed_scope = MemoryScope.parse(scope)
        body = normalize_text(body)
        semantic = memory_semantic_hash(id=memory_id, kind=parsed_kind, scope=parsed_scope, authority=parsed_authority,
                                        portability=parsed_portability, body=body, rationale=rationale, provenance=provenance,
                                        supersedes=supersedes)
        return MemoryRecord(memory_id, parsed_kind, status, parsed_scope, parsed_authority, parsed_portability, body,
                            rationale, provenance, supersedes, generation, semantic, receipt, acceptance_baseline, history, retired_at, retirement_reason)

    def _validate_admission(self, record: MemoryRecord, receipt: PolicyEvaluation) -> None:
        if self.storage_class is StorageClass.PORTABLE and any(
            ref.storage_class is StorageClass.LOCAL_ONLY for ref in record.provenance
        ):
            raise PortabilityViolation("portable memory contains local-only references")
        if record.policy_receipt is not receipt and record.policy_receipt != receipt:
            raise InvalidArtifact("memory receipt must bind the written record")
        if receipt.semantic_hash != record.semantic_hash:
            raise InvalidArtifact("memory policy receipt does not bind semantic content")
        requested = _requested_portability(self.storage_class)
        if record.portability is not requested or receipt.requested is not requested:
            raise InvalidArtifact("memory portability must match its storage class")
        if self.storage_class is StorageClass.PORTABLE:
            policies = PolicyStore(self.vault)
            derived = evaluate_portability(
                requested, self._applicable_policy_rules(record), record.semantic_hash
            )
            if not _same_policy_semantics(receipt, derived) or not derived.allowed:
                raise InvalidArtifact("memory policy receipt does not match current applicable policy")
        elif not receipt.allowed:
            raise InvalidArtifact("local memory policy receipt does not allow this write")

    def _applicable_policy_rules(self, record: MemoryRecord) -> tuple[PolicyRule, ...]:
        """Admission-only policy inheritance seam; Task 19 owns later re-evaluation."""
        policies = PolicyStore(self.vault)
        registries = RegistryStore(self.vault, self.storage_class)
        refs = [policies.load_active("vault-default", StorageClass.PORTABLE)]
        project_ids: set[str] = set()
        if record.scope.project_id is not None:
            project_ids.add(record.scope.project_id)
        if record.scope.workstream_id is not None:
            workstream = registries.load_workstream(record.scope.workstream_id)
            refs.extend(workstream.policy_refs)
            if workstream.project is not None:
                project_ids.add(workstream.project)
        for reference in record.provenance:
            if reference.kind is not ReferenceKind.ID or not isinstance(reference.value, str):
                continue
            try:
                source = registries.load_source(reference.value)
            except InvalidArtifact:
                continue
            if source.policy_ref is not None:
                refs.append(source.policy_ref)
            if source.project is not None:
                project_ids.add(source.project)
        for project_id in sorted(project_ids):
            project = registries.load_project(project_id)
            if project.policy_ref is not None:
                refs.append(project.policy_ref)
        return tuple(policies.load_rule(reference) for reference in refs)

    def _write_cas(self, record: MemoryRecord, expected_generation: int) -> None:
        self.vault.artifacts.write_cas(ArtifactFamily.MEMORY, storage_class=self.storage_class,
            relative_path=self._path(record.id), document=self._document(record), expected_generation=expected_generation)

    def _document(self, record: MemoryRecord) -> ArtifactDocument:
        metadata = {
            "schema": _SCHEMA, "id": record.id, "kind": record.kind.value, "status": record.status.value,
            "scope": record.scope.as_dict(), "authority": record.authority.value, "portability": record.portability.value,
            "rationale": record.rationale, "provenance": [_reference_dict(item) for item in record.provenance],
            "supersedes": record.supersedes, "generation": record.generation, "semantic_hash": record.semantic_hash,
            "policy_receipt": _receipt_dict(record.policy_receipt),
            "acceptance_baseline": record.acceptance_baseline,
            "receipt_history": [_receipt_dict(item) for item in record.receipt_history],
            "retired_at": None if record.retired_at is None else _timestamp_text(record.retired_at),
            "retirement_reason": record.retirement_reason,
        }
        return ArtifactDocument(metadata, record.body, ReferenceManifest.complete(metadata=record.provenance))

    def _parse(self, document: ArtifactDocument, memory_id: str) -> MemoryRecord:
        metadata = dict(document.metadata)
        keys = {"schema", "id", "kind", "status", "scope", "authority", "portability", "rationale", "provenance", "supersedes", "generation", "semantic_hash", "policy_receipt", "acceptance_baseline", "receipt_history", "retired_at", "retirement_reason"}
        if set(metadata) != keys or metadata.get("schema") != _SCHEMA or metadata.get("id") != memory_id or not isinstance(document.body, str):
            raise InvalidArtifact("memory record has an invalid schema")
        try:
            record = self._record(memory_id, metadata["kind"], MemoryStatus(metadata["status"]), metadata["scope"], metadata["authority"], metadata["portability"],
                document.body, metadata["rationale"], _references(metadata["provenance"]), metadata["supersedes"], metadata["generation"],
                _parse_receipt(metadata["policy_receipt"]), metadata["acceptance_baseline"], tuple(_parse_receipt(item) for item in _list(metadata["receipt_history"], "receipt history")),
                None if metadata["retired_at"] is None else _parse_timestamp(metadata["retired_at"]), metadata["retirement_reason"])
        except (TypeError, ValueError) as exc:
            raise InvalidArtifact("memory record has invalid values") from exc
        if record.semantic_hash != metadata["semantic_hash"]:
            raise InvalidArtifact("memory semantic hash does not bind content")
        if record.policy_receipt.semantic_hash != record.semantic_hash:
            raise InvalidArtifact("memory receipt does not bind semantic hash")
        if document.body != record.body:
            raise InvalidArtifact("memory body is not canonical")
        return record

    def _validate_historic_receipt(self, record: MemoryRecord) -> None:
        for ref in record.policy_receipt.refs:
            if isinstance(ref, PolicyRef):
                PolicyStore(self.vault).load_rule(ref)

    def _derived_successor(self, memory_id: str) -> str | None:
        successors = [record.id for record in self.iter_records() if record.id != memory_id and record.supersedes == memory_id]
        if len(successors) > 1:
            raise InvalidArtifact("memory has multiple unresolved successors")
        return successors[0] if successors else None

    @staticmethod
    def _path(memory_id: str) -> str:
        validate_identifier(memory_id, label="memory id")
        return f"memory/{memory_id}.md"


def memory_semantic_hash(*, body: str, id: str = "", kind: MemoryKind | str = MemoryKind.KNOWLEDGE,
                         scope: MemoryScope | Mapping[str, object] | None = None, authority: MemoryAuthority | str = MemoryAuthority.PERSONAL,
                         portability: Portability | str = Portability.PERSONAL_VAULT, rationale: str = "",
                         provenance: tuple[ArtifactReference, ...] = (), supersedes: str | None = None) -> str:
    """Hash every semantic field; lifecycle envelope and generation stay outside it."""
    parsed_scope = MemoryScope.parse(scope or {"type": "personal-global"})
    try:
        parsed_kind = kind if isinstance(kind, MemoryKind) else MemoryKind(kind)
        parsed_authority = authority if isinstance(authority, MemoryAuthority) else MemoryAuthority(authority)
        parsed_portability = portability if isinstance(portability, Portability) else Portability(portability)
    except (TypeError, ValueError) as exc:
        raise InvalidArtifact("memory axis value is invalid") from exc
    payload = {"kind": parsed_kind.value, "scope": parsed_scope.as_dict(), "authority": parsed_authority.value,
               "portability": parsed_portability.value, "body": normalize_text(body), "rationale": rationale,
               "provenance": [_reference_dict(item) for item in provenance], "supersedes": supersedes}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def render_profile(
    readers: MemoryReaders,
    scope: MemoryScope | Mapping[str, object],
    *,
    storage_class: StorageClass = StorageClass.PORTABLE,
) -> ContextView:
    """Generate, but never persist, a deterministic PROFILE projection."""
    if not isinstance(readers, MemoryReaders):
        raise TypeError("render_profile requires MemoryReaders")
    if not isinstance(storage_class, StorageClass):
        raise TypeError("PROFILE requires an explicit StorageClass")
    if readers.storage_class is not storage_class:
        raise PortabilityViolation(
            f"{storage_class.value} PROFILE cannot read {readers.storage_class.value} memory records"
        )
    selected_scope = MemoryScope.parse(scope)
    all_records = readers.load_records()
    by_id = {record.id: record for record in all_records}
    records = tuple(
        record for record in all_records if record.scope == selected_scope
        and record.kind is MemoryKind.PREFERENCE and record.status is MemoryStatus.ACCEPTED
        and not _has_accepted_successor(record, by_id)
    )
    records = tuple(sorted(records, key=lambda item: item.id))
    lines = ["# PROFILE", "", f"storage_class: {storage_class.value}", f"scope: {selected_scope.type.value}"]
    if records:
        lines.extend(("", "## Active accepted preferences"))
        for record in records:
            lines.extend((
                f"### {record.id}",
                record.body.rstrip(),
                f"provenance: {_profile_provenance(record.provenance)}",
                f"policy_receipt: {_profile_receipt(record.policy_receipt)}",
                "",
            ))
    else:
        lines.extend(("", "No active accepted preferences."))
    if len({record.body for record in records}) > 1:
        lines.extend(("", "## Conflicts", "These accepted preferences conflict; PROFILE does not choose one."))
        lines.extend(f"- {record.id}" for record in records)
    text = "\n".join(lines) + "\n"
    resolved = ResolvedWorkstream(ResolutionState.RESOLVED, None, (), (), None, ())
    mode = "portable" if storage_class is StorageClass.PORTABLE else "effective-local"
    return ContextView("profile", mode, text, resolved, None, ResolutionState.RESOLVED, None, ResolutionState.RESOLVED)


def _has_accepted_successor(record: MemoryRecord, by_id: Mapping[str, MemoryRecord]) -> bool:
    if record.superseded_by is None:
        return False
    successor = by_id.get(record.superseded_by)
    return successor is not None and successor.status is MemoryStatus.ACCEPTED


def _profile_provenance(provenance: tuple[ArtifactReference, ...]) -> str:
    if not provenance:
        return "none"
    return ", ".join(
        f"{item.kind.value}:{item.storage_class.value}:{item.value}" for item in provenance
    )


def _profile_receipt(receipt: PolicyEvaluation) -> str:
    refs = ",".join(_profile_receipt_ref(item) for item in receipt.refs) or "none"
    return (
        f"requested={receipt.requested.value}; effective_ceiling={receipt.effective_ceiling.value}; "
        f"semantic_hash={receipt.semantic_hash}; refs={refs}"
    )


def _profile_receipt_ref(ref: PolicyReceiptRef) -> str:
    if isinstance(ref, PolicyRef):
        return f"policy:{ref.policy_id}@{ref.revision}:{ref.digest}"
    return f"opaque:{ref.attestation_id}@{ref.revision}:{ref.digest}"


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidArtifact(f"memory {label} must be non-empty trimmed text")
    return value


def _generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidArtifact("memory generation must be a non-negative integer")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise InvalidArtifact(f"{label} must be a SHA-256 digest")
    return value


def _canonical_timestamp(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidArtifact("memory timestamp must be an aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: object) -> datetime:
    return _canonical_timestamp(value)


def _timestamp_text(value: datetime) -> str:
    return _canonical_timestamp(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise InvalidArtifact("memory retired timestamp must be text")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise InvalidArtifact("memory retired timestamp is invalid") from exc


def _reference_dict(reference: ArtifactReference) -> dict[str, Any]:
    if not isinstance(reference, ArtifactReference):
        raise InvalidArtifact("memory provenance must be typed artifact references")
    return {"kind": reference.kind.value, "storage_class": reference.storage_class.value, "value": reference.value}


def _references(value: object) -> tuple[ArtifactReference, ...]:
    if not isinstance(value, list):
        raise InvalidArtifact("memory provenance must be a list")
    from mneme.core.artifacts import ReferenceKind
    try:
        parsed = tuple(ArtifactReference(ReferenceKind(item["kind"]), StorageClass(item["storage_class"]), item["value"])
                     for item in value if isinstance(item, dict) and set(item) == {"kind", "storage_class", "value"})
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidArtifact("memory provenance is invalid") from exc
    if len(parsed) != len(value):
        raise InvalidArtifact("memory provenance contains an invalid entry")
    return parsed


def _receipt_dict(receipt: PolicyEvaluation) -> dict[str, Any]:
    return {"requested": receipt.requested.value, "effective_ceiling": receipt.effective_ceiling.value, "allowed": receipt.allowed,
            "evaluated_at": receipt.evaluated_at.isoformat(), "evaluator_version": receipt.evaluator_version,
            "refs": [_receipt_ref_dict(item) for item in receipt.refs], "semantic_hash": receipt.semantic_hash,
            "violations": [{"code": item.code, "message": item.message} for item in receipt.violations],
            "approved_rules": [{"rule_id": item.rule_id, "authorizer": _receipt_ref_dict(item.authorizer)} for item in receipt.approved_rules]}


def _receipt_ref_dict(ref: PolicyReceiptRef) -> dict[str, str]:
    if isinstance(ref, PolicyRef):
        return {"type": "policy", "policy_id": ref.policy_id, "revision": ref.revision, "digest": ref.digest}
    return {"type": "opaque", "attestation_id": ref.attestation_id, "revision": ref.revision, "ceiling": ref.ceiling.value, "digest": ref.digest}


def _parse_receipt(value: object) -> PolicyEvaluation:
    if not isinstance(value, Mapping) or set(value) != {"requested", "effective_ceiling", "allowed", "evaluated_at", "evaluator_version", "refs", "semantic_hash", "violations", "approved_rules"}:
        raise InvalidArtifact("memory policy receipt has an invalid schema")
    try:
        refs = tuple(_parse_receipt_ref(item) for item in _list(value["refs"], "receipt refs"))
        raw_violations = _list(value["violations"], "receipt violations")
        violations = tuple(PolicyViolation(item["code"], item["message"]) for item in raw_violations if isinstance(item, Mapping) and set(item) == {"code", "message"})
        raw_approved = _list(value["approved_rules"], "approved rules")
        approved = tuple(ApprovedRuleProvenance(item["rule_id"], _parse_receipt_ref(item["authorizer"])) for item in raw_approved if isinstance(item, Mapping) and set(item) == {"rule_id", "authorizer"})
        if len(violations) != len(raw_violations) or len(approved) != len(raw_approved):
            raise InvalidArtifact("memory policy receipt contains an invalid nested entry")
        return PolicyEvaluation(Portability(value["requested"]), Portability(value["effective_ceiling"]), value["allowed"], datetime.fromisoformat(value["evaluated_at"]), value["evaluator_version"], refs, value["semantic_hash"], violations, approved)
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidArtifact("memory policy receipt has invalid values") from exc


def _parse_receipt_ref(value: object) -> PolicyReceiptRef:
    if not isinstance(value, Mapping):
        raise InvalidArtifact("memory policy receipt ref is invalid")
    if value.get("type") == "policy" and set(value) == {"type", "policy_id", "revision", "digest"}:
        return PolicyRef(value["policy_id"], value["revision"], value["digest"])
    if value.get("type") == "opaque" and set(value) == {"type", "attestation_id", "revision", "ceiling", "digest"}:
        return OpaquePolicyAttestation.from_receipt(value["attestation_id"], value["revision"], Portability(value["ceiling"]), value["digest"])
    raise InvalidArtifact("memory policy receipt ref is invalid")


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise InvalidArtifact(f"memory {label} must be a list")
    return value


def _requested_portability(storage_class: StorageClass) -> Portability:
    return Portability.PERSONAL_VAULT if storage_class is StorageClass.PORTABLE else Portability.LOCAL_ONLY


def _same_policy_semantics(supplied: PolicyEvaluation, derived: PolicyEvaluation) -> bool:
    return (supplied.requested is derived.requested and supplied.effective_ceiling is derived.effective_ceiling and
            supplied.allowed is derived.allowed and supplied.evaluator_version == derived.evaluator_version and
            supplied.refs == derived.refs and supplied.semantic_hash == derived.semantic_hash and
            supplied.violations == derived.violations and supplied.approved_rules == derived.approved_rules)
