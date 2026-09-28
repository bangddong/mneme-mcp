"""Immutable, self-contained Session revisions and checkpoint persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from mneme.core.artifacts import (
    ArtifactDocument,
    ArtifactFamily,
    ArtifactReference,
    ReferenceKind,
    ReferenceManifest,
    StorageClass,
)
from mneme.core.errors import InvalidArtifact, PortabilityViolation
from mneme.core.fs import normalize_text
from mneme.core.policy import (
    ApprovedRuleProvenance,
    OpaquePolicyAttestation,
    PolicyEvaluation,
    PolicyReceiptRef,
    PolicyRef,
    PolicyRule,
    PolicyViolation,
    PolicyStore,
    Portability,
    evaluate_portability,
    policy_admission_snapshot,
)
from mneme.core.registries import HeadRef, RegistryConflict, RegistryStore
from mneme.core.validation.vault import validate_identifier
from mneme.core.validation.secrets import contains_detectable_secret as _contains_detectable_secret


_SCHEMA = "madi.session-revision.v1"
_REVISION = re.compile(r"^[0-9]{6}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


@dataclass(frozen=True, slots=True)
class SessionRevisionRef:
    session: str
    revision: str

    def __post_init__(self) -> None:
        validate_identifier(self.session, label="session id")
        if not isinstance(self.revision, str) or not _REVISION.fullmatch(self.revision):
            raise InvalidArtifact("session revision must be a six-digit revision")

    def as_head(self) -> HeadRef:
        return HeadRef(self.session, self.revision)


@dataclass(frozen=True, slots=True)
class SessionBody:
    """Host-selected semantic continuity material; Core never generates it."""

    adapter_id: str
    objective: str
    current_state: str
    verified_facts: tuple[str, ...]
    completed_work: tuple[str, ...]
    blockers: tuple[str, ...]
    next_actions: tuple[str, ...]
    source_refs: tuple[ArtifactReference, ...]

    def __post_init__(self) -> None:
        validate_identifier(self.adapter_id, label="adapter id")
        _require_text(self.objective, "objective")
        _require_text(self.current_state, "current state")
        for label, values in (
            ("verified facts", self.verified_facts),
            ("completed work", self.completed_work),
            ("blockers", self.blockers),
            ("next actions", self.next_actions),
        ):
            _require_text_tuple(values, label)
        _require_references(self.source_refs, "source refs")


@dataclass(frozen=True, slots=True)
class SessionProvenanceRef:
    """Typed relation provenance; only ``source`` participates in policy input."""

    kind: str
    storage_class: StorageClass
    id: str

    def __post_init__(self) -> None:
        validate_identifier(self.kind, label="provenance kind")
        if not isinstance(self.storage_class, StorageClass):
            raise InvalidArtifact("provenance reference requires a StorageClass")
        validate_identifier(self.id, label="provenance id")

    def as_artifact_reference(self) -> ArtifactReference:
        return ArtifactReference(ReferenceKind.ID, self.storage_class, self.id)


@dataclass(frozen=True, slots=True)
class SessionRelation:
    """An append-only handoff/continuation relation recorded at one boundary."""

    kind: str
    target: str
    purpose: str
    required_context: str
    next_action: str
    provenance_refs: tuple[ArtifactReference | SessionProvenanceRef, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"handoff", "continues_from"}:
            raise InvalidArtifact("session relation kind must be handoff or continues_from")
        for label, value in (
            ("relation target", self.target),
            ("relation purpose", self.purpose),
            ("required context", self.required_context),
            ("relation next action", self.next_action),
        ):
            _require_text(value, label)
        if not isinstance(self.provenance_refs, tuple) or not all(
            isinstance(item, (ArtifactReference, SessionProvenanceRef))
            for item in self.provenance_refs
        ):
            raise InvalidArtifact(
                "relation provenance refs must be typed provenance or ArtifactReference values"
            )


@dataclass(frozen=True, slots=True)
class CheckpointRequest:
    workstream_id: str
    session_id: str
    storage_class: StorageClass
    expected_parent: SessionRevisionRef | None
    expected_registry_generation: int
    body: SessionBody
    relations: tuple[SessionRelation, ...]
    policy_evaluation: PolicyEvaluation
    revision_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.workstream_id, label="workstream id")
        validate_identifier(self.session_id, label="session id")
        if not isinstance(self.storage_class, StorageClass):
            raise InvalidArtifact("checkpoint requires an explicit StorageClass")
        if self.expected_parent is not None:
            if not isinstance(self.expected_parent, SessionRevisionRef):
                raise InvalidArtifact("expected parent must be a SessionRevisionRef or None")
            if self.expected_parent.session != self.session_id:
                raise InvalidArtifact("expected parent must belong to the requested session")
        if (
            not isinstance(self.expected_registry_generation, int)
            or isinstance(self.expected_registry_generation, bool)
            or self.expected_registry_generation < 0
        ):
            raise InvalidArtifact("expected registry generation must be a non-negative integer")
        if not isinstance(self.body, SessionBody):
            raise InvalidArtifact("checkpoint body must be a SessionBody")
        if not isinstance(self.relations, tuple) or not all(
            isinstance(relation, SessionRelation) for relation in self.relations
        ):
            raise InvalidArtifact("checkpoint relations must be a tuple of SessionRelation values")
        if not isinstance(self.policy_evaluation, PolicyEvaluation):
            raise InvalidArtifact("checkpoint policy evaluation must be a PolicyEvaluation")
        if self.revision_timestamp is not None:
            _canonical_timestamp(self.revision_timestamp)


class SessionStore:
    """Persist an immutable revision before attempting the registry head update."""

    def __init__(
        self,
        vault: object,
        storage_class: StorageClass = StorageClass.PORTABLE,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        if not isinstance(storage_class, StorageClass):
            raise InvalidArtifact("session store requires an explicit StorageClass")
        self._vault = vault
        self.storage_class = storage_class
        self.registries = RegistryStore(vault, storage_class=storage_class)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not callable(self._clock):
            raise InvalidArtifact("session clock must be callable")

    def create_revision(self, request: CheckpointRequest) -> SessionRevisionRef:
        if not isinstance(request, CheckpointRequest):
            raise InvalidArtifact("create_revision requires a CheckpointRequest")
        if request.storage_class is not self.storage_class:
            raise InvalidArtifact("checkpoint storage class does not match SessionStore")
        if self.storage_class is StorageClass.PORTABLE:
            _reject_detectable_secrets(request.body, request.relations)
        # Lock order is admission gate then any ArtifactStore/Registry path lock.
        # The gate is reentrant so policy changes cannot linearize between the
        # immutable revision write and its head publication.
        with policy_admission_snapshot(self._vault):
            self._validate_request_references(request)
            observed = self.registries.load_workstream(request.workstream_id)
            if observed.generation != request.expected_registry_generation:
                raise RegistryConflict("workstream generation is stale")
            self._validate_write_policy_binding(request, observed)
            request = replace(
                request,
                revision_timestamp=_canonical_timestamp(
                    request.revision_timestamp if request.revision_timestamp is not None else self._clock()
                ),
            )
            revision = self._next_revision(request)
            ref = SessionRevisionRef(request.session_id, revision)
            document = self._document(request, ref)
            self._vault.artifacts.write_new(
                ArtifactFamily.SESSION,
                storage_class=self.storage_class,
                relative_path=self._revision_path(request.workstream_id, ref),
                document=document,
            )
            self._advance_head(request, ref, observed)
        return ref

    def _advance_head(
        self,
        request: CheckpointRequest,
        ref: SessionRevisionRef,
        observed: object,
    ) -> None:
        """Advance an exact parent lineage or add a distinct session head.

        Same-session replacement is an ordinary exact-generation CAS, never a
        structural merge.  A CAS conflict therefore leaves the already-written
        revision as a deliberate orphan.  Distinct sessions use Task 11's
        bounded proven-disjoint addition retry.
        """
        from mneme.core.registries import WorkstreamRegistry

        if not isinstance(observed, WorkstreamRegistry):
            raise InvalidArtifact("checkpoint observed workstream is invalid")
        new_head = ref.as_head()
        existing = tuple(head for head in observed.active_heads if head.session == ref.session)
        if not existing:
            if request.expected_parent is not None:
                raise RegistryConflict("expected parent is not an active session head")
            self.registries.add_active_head(
                request.workstream_id,
                new_head,
                observed,
                lambda head: self._validate_head(request.workstream_id, head),
            )
            return
        if len(existing) != 1 or request.expected_parent is None or existing[0] != request.expected_parent.as_head():
            raise RegistryConflict("session lineage head does not match the expected parent")
        replacement = tuple(
            new_head if head == existing[0] else head for head in observed.active_heads
        )
        preferred = new_head if observed.preferred_head == existing[0] else observed.preferred_head
        self.registries.update_workstream(
            replace(observed, active_heads=replacement, preferred_head=preferred),
            expected_generation=observed.generation,
        )

    def read_revision(self, ref: SessionRevisionRef, *, workstream_id: str) -> CheckpointRequest:
        if not isinstance(ref, SessionRevisionRef):
            raise InvalidArtifact("session revision ref must be a SessionRevisionRef")
        validate_identifier(workstream_id, label="workstream id")
        document = self._vault.reader.read(
            ArtifactFamily.SESSION,
            storage_class=self.storage_class,
            relative_path=self._revision_path(workstream_id, ref),
        )
        return self._parse_document(document, workstream_id, ref)

    def _validate_head(self, workstream_id: str, head: HeadRef) -> bool:
        self.read_revision(
            SessionRevisionRef(head.session, head.revision), workstream_id=workstream_id
        )
        return True

    def _next_revision(self, request: CheckpointRequest) -> str:
        directory = self._revision_directory(request.workstream_id, request.session_id)
        numbers: list[int] = []
        if directory.exists():
            if not directory.is_dir() or directory.is_symlink():
                raise InvalidArtifact("session revision directory is unsafe")
            for entry in directory.iterdir():
                if entry.is_symlink() or not entry.is_file() or entry.suffix != ".md":
                    raise InvalidArtifact("session lineage contains an invalid artifact")
                if not _REVISION.fullmatch(entry.stem):
                    raise InvalidArtifact("session lineage contains an invalid revision name")
                numbers.append(int(entry.stem))
        numbers.sort()
        if numbers != list(range(1, len(numbers) + 1)):
            raise InvalidArtifact("session lineage has a revision gap")
        if not numbers:
            if request.expected_parent is not None:
                raise InvalidArtifact("expected parent does not match an empty session lineage")
            return "000001"
        previous = SessionRevisionRef(request.session_id, f"{numbers[-1]:06d}")
        previous_request = self.read_revision(
            previous, workstream_id=request.workstream_id
        )
        if previous_request.body.adapter_id != request.body.adapter_id:
            raise InvalidArtifact("session adapter identity is immutable")
        if request.expected_parent != previous:
            raise InvalidArtifact("expected parent does not match current session lineage")
        return f"{numbers[-1] + 1:06d}"

    def _validate_write_policy_binding(self, request: CheckpointRequest, observed: object) -> None:
        self._validate_receipt_semantics(request)
        from mneme.core.registries import WorkstreamRegistry

        if not isinstance(observed, WorkstreamRegistry):
            raise InvalidArtifact("checkpoint observed workstream is invalid")
        derived = evaluate_portability(
            _requested_portability(request.storage_class),
            self._applicable_policy_rules(request, observed),
            session_semantic_hash(request.body, request.relations),
        )
        receipt = request.policy_evaluation
        if not _same_policy_semantics(receipt, derived):
            raise InvalidArtifact("policy receipt does not match current applicable policy inputs")
        if not derived.allowed:
            raise InvalidArtifact("checkpoint policy receipt does not allow this write")

    def _validate_receipt_semantics(self, request: CheckpointRequest) -> None:
        receipt = request.policy_evaluation
        expected = session_semantic_hash(request.body, request.relations)
        if receipt.semantic_hash != expected:
            raise InvalidArtifact("policy receipt semantic hash does not bind checkpoint content")
        if receipt.requested is not _requested_portability(request.storage_class):
            raise InvalidArtifact("policy receipt portability does not match storage class")
        if not receipt.allowed:
            raise InvalidArtifact("checkpoint policy receipt does not allow this write")

    def _validate_read_policy_binding(self, request: CheckpointRequest) -> None:
        self._validate_receipt_semantics(request)
        for policy_ref in request.policy_evaluation.refs:
            if isinstance(policy_ref, PolicyRef):
                PolicyStore(self._vault).load_rule(policy_ref)

    def _validate_request_references(self, request: CheckpointRequest) -> None:
        _validate_historic_references(request)

    def _applicable_policy_rules(
        self, request: CheckpointRequest, observed: object
    ) -> tuple[PolicyRule, ...]:
        from mneme.core.registries import WorkstreamRegistry

        if not isinstance(observed, WorkstreamRegistry):
            raise InvalidArtifact("checkpoint observed workstream is invalid")
        policies = PolicyStore(self._vault)
        refs: list[PolicyRef] = [
            policies.load_active("vault-default", StorageClass.PORTABLE)
        ]
        refs.extend(observed.policy_refs)
        project_ids: set[str] = set()
        if observed.project is not None:
            project_ids.add(observed.project)
        for source_id in self._source_ids(request):
            source = self._load_source(source_id)
            if source.policy_ref is not None:
                refs.append(source.policy_ref)
            if source.project is not None:
                project_ids.add(source.project)
        for project_id in sorted(project_ids):
            project = self._load_project(project_id)
            if project.policy_ref is not None:
                refs.append(project.policy_ref)
        return tuple(policies.load_rule(policy_ref) for policy_ref in refs)

    def _source_ids(self, request: CheckpointRequest) -> tuple[str, ...]:
        source_ids: list[str] = []
        for reference in request.body.source_refs:
            if reference.kind is ReferenceKind.ID:
                if not isinstance(reference.value, str):
                    raise InvalidArtifact("source registry id must be text")
                source_ids.append(reference.value)
        for relation in request.relations:
            for provenance in relation.provenance_refs:
                if isinstance(provenance, SessionProvenanceRef) and provenance.kind == "source":
                    source_ids.append(provenance.id)
        return tuple(dict.fromkeys(source_ids))

    def _load_source(self, source_id: str):
        store = RegistryStore(self._vault, self.storage_class)
        try:
            return store.load_source(source_id)
        except InvalidArtifact as local_error:
            if self.storage_class is not StorageClass.LOCAL_ONLY:
                raise
            try:
                return RegistryStore(self._vault, StorageClass.PORTABLE).load_source(source_id)
            except InvalidArtifact:
                raise local_error

    def _load_project(self, project_id: str):
        store = RegistryStore(self._vault, self.storage_class)
        try:
            return store.load_project(project_id)
        except InvalidArtifact as local_error:
            if self.storage_class is not StorageClass.LOCAL_ONLY:
                raise
            try:
                return RegistryStore(self._vault, StorageClass.PORTABLE).load_project(project_id)
            except InvalidArtifact:
                raise local_error

    @staticmethod
    def _revision_path(workstream_id: str, ref: SessionRevisionRef) -> str:
        validate_identifier(workstream_id, label="workstream id")
        return f"workstreams/{workstream_id}/sessions/{ref.session}/{ref.revision}.md"

    def _revision_directory(self, workstream_id: str, session_id: str) -> Path:
        ref = SessionRevisionRef(session_id, "000001")
        return self._vault.router.location(
            ArtifactFamily.SESSION,
            self.storage_class,
            self._revision_path(workstream_id, ref),
        ).path.parent

    def _document(self, request: CheckpointRequest, ref: SessionRevisionRef) -> ArtifactDocument:
        metadata: dict[str, Any] = {
            "schema": _SCHEMA,
            "workstream_id": request.workstream_id,
            "session_id": request.session_id,
            "revision": ref.revision,
            "timestamp": _serialize_timestamp(request.revision_timestamp),
            "adapter_id": request.body.adapter_id,
            "parent": _serialize_parent(request.expected_parent),
            "objective": request.body.objective,
            "current_state": request.body.current_state,
            "verified_facts": list(request.body.verified_facts),
            "completed_work": list(request.body.completed_work),
            "blockers": list(request.body.blockers),
            "next_actions": list(request.body.next_actions),
            "source_refs": [_serialize_reference(item) for item in request.body.source_refs],
            "relations": [_serialize_relation(item) for item in request.relations],
            "semantic_hash": request.policy_evaluation.semantic_hash,
            "policy_receipt": _serialize_receipt(request.policy_evaluation),
        }
        metadata_refs = _policy_references(request.policy_evaluation, self.storage_class)
        body_refs = request.body.source_refs + tuple(
            _provenance_artifact_reference(reference)
            for relation in request.relations
            for reference in relation.provenance_refs
        )
        return ArtifactDocument(
            metadata=metadata,
            body=_render_body(request.body, request.relations),
            references=ReferenceManifest.complete(metadata=metadata_refs, body=body_refs),
        )

    def _parse_document(
        self, document: ArtifactDocument, workstream_id: str, ref: SessionRevisionRef
    ) -> CheckpointRequest:
        metadata = document.metadata
        if not isinstance(metadata, dict) or set(metadata) != {
            "schema", "workstream_id", "session_id", "revision", "timestamp", "adapter_id", "parent",
            "objective", "current_state", "verified_facts", "completed_work", "blockers",
            "next_actions", "source_refs", "relations", "semantic_hash", "policy_receipt",
        }:
            raise InvalidArtifact("session revision has an invalid schema")
        if (
            metadata.get("schema") != _SCHEMA
            or metadata.get("workstream_id") != workstream_id
            or metadata.get("session_id") != ref.session
            or metadata.get("revision") != ref.revision
        ):
            raise InvalidArtifact("session revision identity does not match its path")
        body = SessionBody(
            adapter_id=metadata.get("adapter_id"), objective=metadata.get("objective"),
            current_state=metadata.get("current_state"),
            verified_facts=_parse_text_tuple(metadata.get("verified_facts"), "verified facts"),
            completed_work=_parse_text_tuple(metadata.get("completed_work"), "completed work"),
            blockers=_parse_text_tuple(metadata.get("blockers"), "blockers"),
            next_actions=_parse_text_tuple(metadata.get("next_actions"), "next actions"),
            source_refs=_parse_references(metadata.get("source_refs"), "source refs"),
        )
        relations = _parse_relations(metadata.get("relations"))
        receipt = _parse_receipt(metadata.get("policy_receipt"))
        request = CheckpointRequest(
            workstream_id, ref.session, self.storage_class, _parse_parent(metadata.get("parent")),
            0, body, relations, receipt, _parse_timestamp(metadata.get("timestamp")),
        )
        # Decode applies the same one-way provenance boundary as admission.
        # This is structural only: it intentionally does not consult current
        # policy pointers, so historic receipts remain auditable.
        request = validate_historic_session_revision(
            request, ref, workstream_id=workstream_id, storage_class=self.storage_class
        )
        self._validate_read_policy_binding(request)
        if metadata.get("semantic_hash") != receipt.semantic_hash:
            raise InvalidArtifact("session revision semantic hash is not receipt-bound")
        if document.body != _render_body(body, relations):
            raise InvalidArtifact("session revision body is not self-contained canonical content")
        # Explicitly revalidate all decoded references; ArtifactReader intentionally
        # returns an empty manifest and therefore cannot prove the original one.
        artifact = self._document(request, ref)
        from mneme.core.validation.vault import validate_portability

        validate_portability(self.storage_class, artifact)
        return request


def session_semantic_hash(body: SessionBody, relations: tuple[SessionRelation, ...]) -> str:
    if not isinstance(body, SessionBody):
        raise InvalidArtifact("semantic hash requires a SessionBody")
    if not isinstance(relations, tuple) or not all(
        isinstance(relation, SessionRelation) for relation in relations
    ):
        raise InvalidArtifact("semantic hash requires SessionRelation values")
    value = {
        "adapter_id": body.adapter_id, "objective": body.objective,
        "current_state": body.current_state, "verified_facts": body.verified_facts,
        "completed_work": body.completed_work, "blockers": body.blockers,
        "next_actions": body.next_actions,
        "source_refs": [_serialize_reference(item) for item in body.source_refs],
        "relations": [_serialize_relation(item) for item in relations],
    }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def validate_historic_session_revision(
    request: object,
    ref: object,
    *,
    workstream_id: object,
    storage_class: object,
) -> CheckpointRequest:
    """Canonicalize and validate one historic session without reading live policy.

    This verifies the immutable self-contained receipt/body binding and every
    nested semantic value.  It deliberately does not resolve policy pointers or
    load a policy store: historical admission is audited from its own receipt.
    """

    if not isinstance(request, CheckpointRequest):
        raise InvalidArtifact("historic session requires a CheckpointRequest")
    if not isinstance(ref, SessionRevisionRef):
        raise InvalidArtifact("historic session requires a SessionRevisionRef")
    validate_identifier(workstream_id, label="workstream id")
    if not isinstance(storage_class, StorageClass):
        raise InvalidArtifact("historic session requires a StorageClass")
    canonical_ref = SessionRevisionRef(ref.session, ref.revision)
    canonical_body = SessionBody(
        request.body.adapter_id,
        request.body.objective,
        request.body.current_state,
        tuple(request.body.verified_facts),
        tuple(request.body.completed_work),
        tuple(request.body.blockers),
        tuple(request.body.next_actions),
        tuple(
            ArtifactReference(item.kind, item.storage_class, item.value)
            for item in request.body.source_refs
        ),
    )
    canonical_relations = tuple(
        SessionRelation(
            relation.kind,
            relation.target,
            relation.purpose,
            relation.required_context,
            relation.next_action,
            tuple(_canonical_provenance(item) for item in relation.provenance_refs),
        )
        for relation in request.relations
    )
    canonical_parent = (
        None
        if request.expected_parent is None
        else SessionRevisionRef(
            request.expected_parent.session, request.expected_parent.revision
        )
    )
    receipt = request.policy_evaluation
    canonical_receipt = PolicyEvaluation(
        receipt.requested,
        receipt.effective_ceiling,
        receipt.allowed,
        receipt.evaluated_at,
        receipt.evaluator_version,
        tuple(receipt.refs),
        receipt.semantic_hash,
        tuple(PolicyViolation(item.code, item.message) for item in receipt.violations),
        tuple(
            ApprovedRuleProvenance(item.rule_id, item.authorizer)
            for item in receipt.approved_rules
        ),
    )
    timestamp = request.revision_timestamp
    if timestamp is None or timestamp.utcoffset() != timezone.utc.utcoffset(None):
        raise InvalidArtifact("historic session timestamp must be UTC")
    canonical_timestamp = _canonical_timestamp(timestamp)
    canonical = CheckpointRequest(
        request.workstream_id,
        request.session_id,
        request.storage_class,
        canonical_parent,
        request.expected_registry_generation,
        canonical_body,
        canonical_relations,
        canonical_receipt,
        canonical_timestamp,
    )
    if (
        canonical.workstream_id != workstream_id
        or canonical.workstream_id != request.workstream_id
        or canonical.session_id != canonical_ref.session
        or canonical.storage_class is not storage_class
    ):
        raise InvalidArtifact("historic session identity does not match its reader contract")
    _validate_historic_references(canonical)
    if canonical.policy_evaluation.semantic_hash != session_semantic_hash(
        canonical.body, canonical.relations
    ):
        raise InvalidArtifact("historic session receipt does not bind semantic content")
    if canonical.policy_evaluation.requested is not _requested_portability(storage_class):
        raise InvalidArtifact("historic session receipt portability does not match storage")
    if not canonical.policy_evaluation.allowed:
        raise InvalidArtifact("historic session receipt does not admit stored content")
    return canonical


def _canonical_provenance(
    value: object,
) -> ArtifactReference | SessionProvenanceRef:
    if isinstance(value, ArtifactReference):
        return ArtifactReference(value.kind, value.storage_class, value.value)
    if isinstance(value, SessionProvenanceRef):
        return SessionProvenanceRef(value.kind, value.storage_class, value.id)
    raise InvalidArtifact("historic session provenance is invalid")


def _validate_historic_references(request: CheckpointRequest) -> None:
    if request.storage_class is not StorageClass.PORTABLE:
        return
    references = request.body.source_refs + tuple(
        _provenance_artifact_reference(reference)
        for relation in request.relations
        for reference in relation.provenance_refs
    )
    if any(reference.storage_class is StorageClass.LOCAL_ONLY for reference in references):
        raise PortabilityViolation("portable checkpoint contains local-only references")
    if any(
        isinstance(reference, ArtifactReference)
        for relation in request.relations
        for reference in relation.provenance_refs
    ):
        raise PortabilityViolation("portable checkpoint provenance requires explicit typed references")


def _requested_portability(storage_class: StorageClass) -> Portability:
    return (
        Portability.LOCAL_ONLY
        if storage_class is StorageClass.LOCAL_ONLY
        else Portability.PERSONAL_VAULT
    )


def _same_policy_semantics(
    supplied: PolicyEvaluation, derived: PolicyEvaluation
) -> bool:
    """Compare deterministic admission semantics, excluding evaluation wall time."""
    return (
        supplied.requested is derived.requested
        and supplied.effective_ceiling is derived.effective_ceiling
        and supplied.allowed is derived.allowed
        and supplied.evaluator_version == derived.evaluator_version
        and supplied.refs == derived.refs
        and supplied.semantic_hash == derived.semantic_hash
        and supplied.violations == derived.violations
        and supplied.approved_rules == derived.approved_rules
    )


def _canonical_timestamp(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidArtifact("session revision timestamp must be an aware datetime")
    try:
        normalized = value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise InvalidArtifact("session revision timestamp is invalid") from exc
    return normalized


def _serialize_timestamp(value: object) -> str:
    timestamp = _canonical_timestamp(value)
    return timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise InvalidArtifact("session revision timestamp must be canonical RFC3339 UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise InvalidArtifact("session revision timestamp is invalid") from exc
    return parsed.replace(tzinfo=timezone.utc)


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidArtifact(f"session {label} must be non-empty trimmed text")
    return value


def _reject_detectable_secrets(
    body: SessionBody, relations: tuple[SessionRelation, ...]
) -> None:
    """Keep recognizable credentials out of portable Session revisions."""
    values = [
        body.adapter_id,
        body.objective,
        body.current_state,
        *body.verified_facts,
        *body.completed_work,
        *body.blockers,
        *body.next_actions,
    ]
    values.extend(
        reference.value
        for reference in body.source_refs
        if isinstance(reference.value, str)
    )
    for relation in relations:
        values.extend(
            (
                relation.kind,
                relation.target,
                relation.purpose,
                relation.required_context,
                relation.next_action,
            )
        )
        for provenance in relation.provenance_refs:
            if isinstance(provenance, ArtifactReference):
                if isinstance(provenance.value, str):
                    values.append(provenance.value)
            else:
                values.extend((provenance.kind, provenance.id))
    if any(_contains_detectable_secret(value) for value in values):
        raise InvalidArtifact("checkpoint contains a detectable credential")


def _require_text_tuple(value: object, label: str) -> None:
    if not isinstance(value, tuple) or not all(isinstance(item, str) and item.strip() == item and item for item in value):
        raise InvalidArtifact(f"session {label} must be a tuple of non-empty trimmed text")


def _require_references(value: object, label: str) -> None:
    if not isinstance(value, tuple) or not all(isinstance(item, ArtifactReference) for item in value):
        raise InvalidArtifact(f"session {label} must be a tuple of ArtifactReference values")


def _serialize_reference(reference: ArtifactReference) -> dict[str, Any]:
    result = {"kind": reference.kind.value, "storage_class": reference.storage_class.value, "value": reference.value}
    if reference.target_family is not None:
        result["target_family"] = reference.target_family
    return result


def _parse_reference(value: object) -> ArtifactReference:
    if not isinstance(value, dict) or set(value) not in ({"kind", "storage_class", "value"}, {"kind", "storage_class", "value", "target_family"}):
        raise InvalidArtifact("session artifact reference has an invalid schema")
    try:
        return ArtifactReference(ReferenceKind(value["kind"]), StorageClass(value["storage_class"]), value["value"], value.get("target_family"))
    except (TypeError, ValueError) as exc:
        raise InvalidArtifact("session artifact reference has invalid values") from exc


def _parse_references(value: object, label: str) -> tuple[ArtifactReference, ...]:
    if not isinstance(value, list):
        raise InvalidArtifact(f"session {label} must be a list")
    return tuple(_parse_reference(item) for item in value)


def _serialize_relation(relation: SessionRelation) -> dict[str, Any]:
    return {"kind": relation.kind, "target": relation.target, "purpose": relation.purpose,
            "required_context": relation.required_context, "next_action": relation.next_action,
            "provenance_refs": [_serialize_provenance(item) for item in relation.provenance_refs]}


def _provenance_artifact_reference(
    value: ArtifactReference | SessionProvenanceRef,
) -> ArtifactReference:
    return value if isinstance(value, ArtifactReference) else value.as_artifact_reference()


def _serialize_provenance(
    value: ArtifactReference | SessionProvenanceRef,
) -> dict[str, Any]:
    if isinstance(value, ArtifactReference):
        return _serialize_reference(value)
    return {
        "type": "typed",
        "kind": value.kind,
        "storage_class": value.storage_class.value,
        "id": value.id,
    }


def _parse_provenance(
    value: object,
) -> ArtifactReference | SessionProvenanceRef:
    if isinstance(value, dict) and value.get("type") == "typed":
        if set(value) != {"type", "kind", "storage_class", "id"}:
            raise InvalidArtifact("typed provenance has an invalid schema")
        try:
            return SessionProvenanceRef(
                value["kind"], StorageClass(value["storage_class"]), value["id"]
            )
        except ValueError as exc:
            raise InvalidArtifact("typed provenance has invalid values") from exc
    return _parse_reference(value)


def _parse_relations(value: object) -> tuple[SessionRelation, ...]:
    if not isinstance(value, list):
        raise InvalidArtifact("session relations must be a list")
    parsed: list[SessionRelation] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "target", "purpose", "required_context", "next_action", "provenance_refs"}:
            raise InvalidArtifact("session relation has an invalid schema")
        provenance = item["provenance_refs"]
        if not isinstance(provenance, list):
            raise InvalidArtifact("session relation provenance refs must be a list")
        parsed.append(SessionRelation(item["kind"], item["target"], item["purpose"], item["required_context"], item["next_action"], tuple(_parse_provenance(value) for value in provenance)))
    return tuple(parsed)


def _serialize_parent(parent: SessionRevisionRef | None) -> dict[str, str] | None:
    return None if parent is None else {"session": parent.session, "revision": parent.revision}


def _parse_parent(value: object) -> SessionRevisionRef | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"session", "revision"}:
        raise InvalidArtifact("session parent has an invalid schema")
    return SessionRevisionRef(value["session"], value["revision"])


def _parse_text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvalidArtifact(f"session {label} must be a list")
    result = tuple(value)
    _require_text_tuple(result, label)
    return result


def _receipt_to_ref(value: object) -> PolicyReceiptRef:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise InvalidArtifact("session policy receipt ref has an invalid schema")
    if value["type"] == "policy" and set(value) == {"type", "policy_id", "revision", "digest"}:
        return PolicyRef(value["policy_id"], value["revision"], value["digest"])
    if value["type"] == "opaque" and set(value) == {"type", "attestation_id", "revision", "ceiling", "digest"}:
        try:
            return OpaquePolicyAttestation.from_receipt(value["attestation_id"], value["revision"], Portability(value["ceiling"]), value["digest"])
        except ValueError as exc:
            raise InvalidArtifact("session opaque receipt has an invalid ceiling") from exc
    raise InvalidArtifact("session policy receipt ref has an invalid schema")


def _ref_to_receipt(ref: PolicyReceiptRef) -> dict[str, str]:
    if isinstance(ref, PolicyRef):
        return {"type": "policy", "policy_id": ref.policy_id, "revision": ref.revision, "digest": ref.digest}
    return {"type": "opaque", "attestation_id": ref.attestation_id, "revision": ref.revision, "ceiling": ref.ceiling.value, "digest": ref.digest}


def _serialize_receipt(receipt: PolicyEvaluation) -> dict[str, Any]:
    return {"requested": receipt.requested.value, "effective_ceiling": receipt.effective_ceiling.value,
            "allowed": receipt.allowed, "evaluated_at": receipt.evaluated_at.isoformat(),
            "evaluator_version": receipt.evaluator_version, "refs": [_ref_to_receipt(ref) for ref in receipt.refs],
            "semantic_hash": receipt.semantic_hash,
            "violations": [{"code": item.code, "message": item.message} for item in receipt.violations],
            "approved_rules": [{"rule_id": item.rule_id, "authorizer": _ref_to_receipt(item.authorizer)} for item in receipt.approved_rules]}


def _parse_receipt(value: object) -> PolicyEvaluation:
    if not isinstance(value, dict) or set(value) != {"requested", "effective_ceiling", "allowed", "evaluated_at", "evaluator_version", "refs", "semantic_hash", "violations", "approved_rules"}:
        raise InvalidArtifact("session policy receipt has an invalid schema")
    if not isinstance(value["refs"], list) or not isinstance(value["violations"], list) or not isinstance(value["approved_rules"], list):
        raise InvalidArtifact("session policy receipt has invalid list fields")
    try:
        evaluated_at = datetime.fromisoformat(value["evaluated_at"])
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError
        refs = tuple(_receipt_to_ref(item) for item in value["refs"])
        violations = tuple(PolicyViolation(item["code"], item["message"]) for item in value["violations"] if isinstance(item, dict) and set(item) == {"code", "message"})
        if len(violations) != len(value["violations"]):
            raise ValueError
        approved = tuple(ApprovedRuleProvenance(item["rule_id"], _receipt_to_ref(item["authorizer"])) for item in value["approved_rules"] if isinstance(item, dict) and set(item) == {"rule_id", "authorizer"})
        if len(approved) != len(value["approved_rules"]):
            raise ValueError
        return PolicyEvaluation(Portability(value["requested"]), Portability(value["effective_ceiling"]), value["allowed"], evaluated_at, value["evaluator_version"], refs, value["semantic_hash"], violations, approved)
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidArtifact("session policy receipt has invalid values") from exc


def _policy_references(
    receipt: PolicyEvaluation, storage_class: StorageClass
) -> tuple[ArtifactReference, ...]:
    return tuple(
        ArtifactReference(ReferenceKind.ID, storage_class, ref.policy_id)
        for ref in receipt.refs if isinstance(ref, PolicyRef)
    )


def _render_body(body: SessionBody, relations: tuple[SessionRelation, ...]) -> str:
    sections = (
        ("Objective", (body.objective,)), ("Current state", (body.current_state,)),
        ("Verified facts", body.verified_facts), ("Completed work", body.completed_work),
        ("Blockers", body.blockers), ("Next actions", body.next_actions),
        ("Source references", tuple(json.dumps(_serialize_reference(item), sort_keys=True) for item in body.source_refs)),
        ("Relations", tuple(json.dumps(_serialize_relation(item), sort_keys=True) for item in relations)),
    )
    rendered: list[str] = []
    for name, items in sections:
        rendered.append(f"## {name}")
        rendered.extend(f"- {item}" for item in items)
        if not items:
            rendered.append("- none")
        rendered.append("")
    return normalize_text("\n".join(rendered))
