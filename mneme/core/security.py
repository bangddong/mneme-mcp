"""Fail-closed current-policy authorization for outward Core operations."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from mneme.core.artifacts import ReferenceKind, StorageClass
from mneme.core.errors import InvalidArtifact
from mneme.core.policy import (
    PolicyEvaluation,
    PolicyRef,
    PolicyRule,
    PolicyStore,
    Portability,
    evaluate_portability,
)
from mneme.core.registries import RegistryStore
from mneme.core.validation.vault import validate_identifier


_OPERATIONS = frozenset({"recall", "context", "profile", "export", "sync-preflight"})
_CLOSED_CODES = frozenset({"policy-current-denied", "policy-current-unavailable"})
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PolicyArtifactRef:
    """An opaque reference to one current-tree artifact to authorize."""

    kind: str
    artifact_id: str
    storage_class: StorageClass = StorageClass.PORTABLE
    workstream_id: str | None = None
    revision: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {
            "memory",
            "session",
            "source",
            "project",
            "vault",
            "workstream",
        }:
            raise ValueError("policy artifact kind is invalid")
        validate_identifier(self.artifact_id, label="policy artifact id")
        if not isinstance(self.storage_class, StorageClass):
            raise ValueError("policy artifact storage class is invalid")
        if self.kind == "session":
            validate_identifier(self.workstream_id, label="policy workstream id")
            if not isinstance(self.revision, str) or not re.fullmatch(r"[0-9]{6}", self.revision):
                raise ValueError("policy Session revision is invalid")
        elif self.workstream_id is not None or self.revision is not None:
            raise ValueError("only Session authorization names a workstream or revision")

    @classmethod
    def memory(
        cls, memory_id: str, storage_class: StorageClass = StorageClass.PORTABLE
    ) -> PolicyArtifactRef:
        return cls("memory", memory_id, storage_class)

    @classmethod
    def session(
        cls,
        workstream_id: str,
        session_id: str,
        revision: str,
        storage_class: StorageClass = StorageClass.PORTABLE,
    ) -> PolicyArtifactRef:
        return cls("session", session_id, storage_class, workstream_id, revision)

    @classmethod
    def source(
        cls, source_id: str, storage_class: StorageClass = StorageClass.PORTABLE
    ) -> PolicyArtifactRef:
        return cls("source", source_id, storage_class)

    @classmethod
    def project(
        cls, project_id: str, storage_class: StorageClass = StorageClass.PORTABLE
    ) -> PolicyArtifactRef:
        return cls("project", project_id, storage_class)

    @classmethod
    def workstream(
        cls, workstream_id: str, storage_class: StorageClass = StorageClass.PORTABLE
    ) -> PolicyArtifactRef:
        return cls("workstream", workstream_id, storage_class)

    @classmethod
    def vault(cls, vault_id: str) -> PolicyArtifactRef:
        return cls("vault", vault_id, StorageClass.PORTABLE)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A closed local-safe live-policy verdict with an opaque cache fingerprint."""

    allowed: bool
    issue_codes: tuple[str, ...] = ()
    authorization_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise TypeError("policy decision allowed must be a boolean")
        if (
            not isinstance(self.issue_codes, tuple)
            or not all(code in _CLOSED_CODES for code in self.issue_codes)
        ):
            raise ValueError("policy decision has an invalid issue code")
        if self.allowed and self.issue_codes:
            raise ValueError("allowed policy decision cannot carry issue codes")
        if not self.allowed and self.authorization_fingerprint is not None:
            raise ValueError("denied policy decision cannot carry a cache capability")
        if (
            self.authorization_fingerprint is not None
            and (
                not isinstance(self.authorization_fingerprint, str)
                or not _FINGERPRINT.fullmatch(self.authorization_fingerprint)
            )
        ):
            raise ValueError("policy decision fingerprint is invalid")


class PolicyAuthorizer:
    """Re-evaluate immutable write receipts against current canonical pointers.

    Index rows and generated views deliberately never participate in these
    calculations.  A malformed/missing pointer or artifact is indistinguishable
    to callers from other unavailable current policy and fails closed.
    """

    def __init__(self, vault: object) -> None:
        self._vault = vault

    def authorize_current(
        self, artifact_ref: PolicyArtifactRef, operation: str
    ) -> PolicyDecision:
        if not isinstance(artifact_ref, PolicyArtifactRef) or operation not in _OPERATIONS:
            return PolicyDecision(False, ("policy-current-unavailable",))
        try:
            receipt, requested, rules, inputs = self._authorization_inputs(artifact_ref)
            fingerprint = self._fingerprint(artifact_ref, operation, receipt, inputs)
            semantic_hash = (
                sha256(repr(artifact_ref).encode("utf-8")).hexdigest()
                if receipt is None
                else receipt.semantic_hash
            )
            evaluation = evaluate_portability(requested, rules, semantic_hash)
            if evaluation.allowed:
                return PolicyDecision(True, (), fingerprint)
            return PolicyDecision(False, ("policy-current-denied",))
        except Exception:
            return PolicyDecision(False, ("policy-current-unavailable",))

    def authorize_current_view(
        self,
        artifact_refs: tuple[PolicyArtifactRef, ...],
        operation: str,
        *,
        view_binding: object | None = None,
    ) -> PolicyDecision:
        """Authorize every rendered artifact and bind one local view cache key.

        A generated view may only be reused when *all* current candidates remain
        allowed under the same canonical inputs.  Individual cache keys are never
        sufficient for a multi-head CURRENT or multi-memory PROFILE projection.
        """
        if operation not in {"context", "profile"} or not artifact_refs:
            return PolicyDecision(False, ("policy-current-unavailable",))
        try:
            references = tuple(
                sorted(
                    set(artifact_refs),
                    key=lambda item: (
                        item.kind,
                        item.artifact_id,
                        item.storage_class.value,
                        item.workstream_id or "",
                        item.revision or "",
                    ),
                )
            )
        except Exception:
            return PolicyDecision(False, ("policy-current-unavailable",))
        fingerprints: list[str] = []
        for reference in references:
            decision = self.authorize_current(reference, operation)
            if not decision.allowed:
                return PolicyDecision(False, decision.issue_codes)
            if decision.authorization_fingerprint is None:
                return PolicyDecision(False, ("policy-current-unavailable",))
            fingerprints.append(decision.authorization_fingerprint)
        try:
            payload = json.dumps(
                {
                    "artifacts": fingerprints,
                    "operation": operation,
                    "view_binding": view_binding,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return PolicyDecision(False, ("policy-current-unavailable",))
        return PolicyDecision(
            True, (), sha256(payload.encode("utf-8")).hexdigest()
        )

    def authorize_context_view(
        self,
        workstream_id: str,
        storage_class: StorageClass = StorageClass.PORTABLE,
    ) -> PolicyDecision:
        """Bind the current workstream registry and every active head to CURRENT."""
        try:
            registry = RegistryStore(self._vault, storage_class).load_workstream(
                workstream_id
            )
            refs = (PolicyArtifactRef.workstream(workstream_id, storage_class),) + tuple(
                PolicyArtifactRef.session(
                    workstream_id,
                    head.session,
                    head.revision,
                    storage_class,
                )
                for head in registry.active_heads
            )
            return self.authorize_current_view(
                refs,
                "context",
                view_binding={
                    "storage_class": storage_class.value,
                    "view": "CURRENT",
                    "workstream_id": workstream_id,
                },
            )
        except Exception:
            return PolicyDecision(False, ("policy-current-unavailable",))

    def authorize_profile_view(
        self,
        scope: object,
        storage_class: StorageClass = StorageClass.PORTABLE,
    ) -> PolicyDecision:
        """Bind exactly the rendered PROFILE selection and lifecycle to CURRENT."""
        try:
            from mneme.core.memories import (
                MemoryKind,
                MemoryScope,
                MemoryStatus,
                MemoryStore,
                _has_successor,
            )

            selected_scope = MemoryScope.parse(scope)
            records = MemoryStore(self._vault, storage_class).iter_records()
            by_id = {record.id: record for record in records}
            selected = tuple(
                record
                for record in records
                if record.scope == selected_scope
                and record.kind is MemoryKind.PREFERENCE
                and record.status is MemoryStatus.ACCEPTED
                and not _has_successor(record, by_id)
            )
            refs = (PolicyArtifactRef.vault(self._vault.id),) + tuple(
                PolicyArtifactRef.memory(record.id, storage_class) for record in selected
            )
            return self.authorize_current_view(
                refs,
                "profile",
                view_binding={
                    "scope": selected_scope.as_dict(),
                    "storage_class": storage_class.value,
                    "view": "PROFILE",
                },
            )
        except Exception:
            return PolicyDecision(False, ("policy-current-unavailable",))

    def authorize_current_tree(self) -> PolicyDecision:
        """Authorize every portable current-tree artifact before Git transport."""
        try:
            references = self._current_tree_references()
        except Exception:
            return PolicyDecision(False, ("policy-current-unavailable",))
        for reference in references:
            decision = self.authorize_current(reference, "sync-preflight")
            if not decision.allowed:
                return PolicyDecision(False, decision.issue_codes)
        return PolicyDecision(True)

    def _authorization_inputs(
        self, artifact_ref: PolicyArtifactRef
    ) -> tuple[
        PolicyEvaluation | None,
        Portability,
        tuple[PolicyRule, ...],
        tuple[str, ...],
    ]:
        requested = self._requested_portability(artifact_ref.storage_class)
        if artifact_ref.kind == "session":
            from mneme.core.sessions import SessionRevisionRef, SessionStore

            request = SessionStore(self._vault, artifact_ref.storage_class).read_revision(
                SessionRevisionRef(artifact_ref.artifact_id, artifact_ref.revision or ""),
                workstream_id=artifact_ref.workstream_id or "",
            )
            if request.storage_class is not artifact_ref.storage_class:
                raise ValueError("Session storage class is not canonical")
            self._validate_historic_receipt(request.policy_evaluation, requested)
            return (
                request.policy_evaluation,
                requested,
                *self._current_rules(
                    storage_class=artifact_ref.storage_class,
                    workstream_id=request.workstream_id,
                    source_ids=self._session_source_ids(request),
                ),
            )
        if artifact_ref.kind == "memory":
            from mneme.core.memories import MemoryStore

            record = MemoryStore(self._vault, artifact_ref.storage_class).read(
                artifact_ref.artifact_id
            )
            if record.portability is not requested:
                raise ValueError("Memory portability does not match canonical storage")
            self._validate_historic_receipt(record.policy_receipt, requested)
            project_id = record.scope.project_id
            rules, inputs = self._current_rules(
                storage_class=artifact_ref.storage_class,
                workstream_id=record.scope.workstream_id,
                project_ids=() if project_id is None else (project_id,),
                source_ids=tuple(
                    reference.value
                    for reference in record.provenance
                    if reference.kind is ReferenceKind.ID
                    and isinstance(reference.value, str)
                    and (
                        reference.storage_class is artifact_ref.storage_class
                        or (
                            artifact_ref.storage_class is StorageClass.LOCAL_ONLY
                            and reference.storage_class is StorageClass.PORTABLE
                        )
                    )
                ),
            )
            return (
                record.policy_receipt,
                requested,
                rules,
                tuple(sorted((*inputs, self._memory_state_token(record)))),
            )
        if artifact_ref.kind == "source":
            return (
                None,
                requested,
                *self._current_rules(
                    storage_class=artifact_ref.storage_class,
                    source_ids=(artifact_ref.artifact_id,),
                ),
            )
        if artifact_ref.kind == "project":
            return (
                None,
                requested,
                *self._current_rules(
                    storage_class=artifact_ref.storage_class,
                    project_ids=(artifact_ref.artifact_id,),
                ),
            )
        if artifact_ref.kind == "vault":
            return (
                None,
                requested,
                *self._current_rules(storage_class=StorageClass.PORTABLE),
            )
        return (
            None,
            requested,
            *self._current_rules(
                storage_class=artifact_ref.storage_class,
                workstream_id=artifact_ref.artifact_id,
            ),
        )

    def _current_rules(
        self,
        *,
        storage_class: StorageClass,
        workstream_id: str | None = None,
        project_ids: tuple[str, ...] = (),
        source_ids: tuple[str, ...] = (),
    ) -> tuple[tuple[PolicyRule, ...], tuple[str, ...]]:
        policies = PolicyStore(self._vault)
        registries = RegistryStore(self._vault, storage_class)
        vault_ref = policies.load_active("vault-default", StorageClass.PORTABLE)
        refs: list[PolicyRef] = [vault_ref]
        inputs: list[str] = [
            self._input_token("vault", "vault-default", vault_ref)
        ]
        projects = set(project_ids)
        if workstream_id is not None:
            workstream = registries.load_workstream(workstream_id)
            inputs.extend(
                self._input_token("workstream", workstream.id, reference)
                for reference in workstream.policy_refs
            )
            refs.extend(workstream.policy_refs)
            if workstream.project is not None:
                projects.add(workstream.project)
        for source_id in sorted(set(source_ids)):
            source = self._load_source(registries, source_id)
            inputs.append(self._input_token("source", source.id, source.policy_ref))
            if source.policy_ref is not None:
                refs.append(source.policy_ref)
            if source.project is not None:
                projects.add(source.project)
        for project_id in sorted(projects):
            project = self._load_project(registries, project_id)
            inputs.append(self._input_token("project", project.id, project.policy_ref))
            if project.policy_ref is not None:
                refs.append(project.policy_ref)
        return tuple(policies.load_rule(reference) for reference in refs), tuple(sorted(inputs))

    def _load_source(self, registries: RegistryStore, source_id: str):
        try:
            return registries.load_source(source_id)
        except InvalidArtifact as local_error:
            if registries.storage_class is not StorageClass.LOCAL_ONLY:
                raise
            try:
                return RegistryStore(
                    self._vault, StorageClass.PORTABLE
                ).load_source(source_id)
            except InvalidArtifact:
                raise local_error

    def _load_project(self, registries: RegistryStore, project_id: str):
        try:
            return registries.load_project(project_id)
        except InvalidArtifact as local_error:
            if registries.storage_class is not StorageClass.LOCAL_ONLY:
                raise
            try:
                return RegistryStore(
                    self._vault, StorageClass.PORTABLE
                ).load_project(project_id)
            except InvalidArtifact:
                raise local_error

    @staticmethod
    def _session_source_ids(request: object) -> tuple[str, ...]:
        from mneme.core.sessions import SessionProvenanceRef

        source_ids: list[str] = []
        for reference in request.body.source_refs:
            if reference.kind is ReferenceKind.ID and isinstance(reference.value, str):
                source_ids.append(reference.value)
        for relation in request.relations:
            source_ids.extend(
                reference.id
                for reference in relation.provenance_refs
                if isinstance(reference, SessionProvenanceRef) and reference.kind == "source"
            )
        return tuple(dict.fromkeys(source_ids))

    @staticmethod
    def _input_token(
        kind: str, artifact_id: str, reference: PolicyRef | None
    ) -> str:
        if reference is None:
            return f"{kind}:{artifact_id}:none"
        return ":".join(
            (kind, artifact_id, reference.policy_id, reference.revision, reference.digest)
        )

    @staticmethod
    def _memory_state_token(record: object) -> str:
        """Bind a generated PROFILE cache to canonical Memory lifecycle/content state."""
        return ":".join(
            (
                "memory-state",
                record.id,
                record.status.value,
                str(record.generation),
                record.semantic_hash,
                record.accepted_semantic_hash or "none",
                record.superseded_by or "none",
            )
        )

    @staticmethod
    def _fingerprint(
        artifact_ref: PolicyArtifactRef,
        operation: str,
        receipt: PolicyEvaluation | None,
        inputs: tuple[str, ...],
    ) -> str:
        payload = {
            "artifact": {
                "id": artifact_ref.artifact_id,
                "kind": artifact_ref.kind,
                "revision": artifact_ref.revision,
                "storage_class": artifact_ref.storage_class.value,
                "workstream": artifact_ref.workstream_id,
            },
            "inputs": inputs,
            "operation": operation,
            "receipt": None
            if receipt is None
            else {
                "requested": receipt.requested.value,
                "semantic_hash": receipt.semantic_hash,
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _requested_portability(storage_class: StorageClass) -> Portability:
        return (
            Portability.PERSONAL_VAULT
            if storage_class is StorageClass.PORTABLE
            else Portability.LOCAL_ONLY
        )

    @staticmethod
    def _validate_historic_receipt(
        receipt: PolicyEvaluation, requested: Portability
    ) -> None:
        if receipt.requested is not requested or not receipt.allowed:
            raise ValueError("historic policy receipt does not bind actual storage")

    def _current_tree_references(self) -> tuple[PolicyArtifactRef, ...]:
        root = self._vault.root
        references: list[PolicyArtifactRef] = [PolicyArtifactRef.vault(self._vault.id)]
        for path in sorted((root / "projects").glob("*.yaml")):
            references.append(PolicyArtifactRef.project(path.stem))
        for path in sorted((root / "sources").glob("*.yaml")):
            references.append(PolicyArtifactRef.source(path.stem))
        for path in sorted((root / "workstreams").glob("*/workstream.yaml")):
            workstream_id = path.parent.name
            references.append(PolicyArtifactRef.workstream(workstream_id))
            for revision in sorted(path.parent.glob("sessions/*/*.md")):
                references.append(
                    PolicyArtifactRef.session(
                        workstream_id, revision.parent.name, revision.stem
                    )
                )
        for path in sorted((root / "memory").glob("*.md")):
            references.append(PolicyArtifactRef.memory(path.stem))
        return tuple(references)
