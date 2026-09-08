"""Fail-closed current-policy authorization for outward Core operations."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from mneme.core.artifacts import ReferenceKind, StorageClass
from mneme.core.policy import (
    PolicyEvaluation,
    PolicyRef,
    PolicyRule,
    PolicyStore,
    Portability,
    evaluate_portability,
    reevaluate_portability,
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
    workstream_id: str | None = None
    revision: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"memory", "session", "source", "project", "workstream"}:
            raise ValueError("policy artifact kind is invalid")
        validate_identifier(self.artifact_id, label="policy artifact id")
        if self.kind == "session":
            validate_identifier(self.workstream_id, label="policy workstream id")
            if not isinstance(self.revision, str) or not re.fullmatch(r"[0-9]{6}", self.revision):
                raise ValueError("policy Session revision is invalid")
        elif self.workstream_id is not None or self.revision is not None:
            raise ValueError("only Session authorization names a workstream or revision")

    @classmethod
    def memory(cls, memory_id: str) -> PolicyArtifactRef:
        return cls("memory", memory_id)

    @classmethod
    def session(
        cls, workstream_id: str, session_id: str, revision: str
    ) -> PolicyArtifactRef:
        return cls("session", session_id, workstream_id, revision)

    @classmethod
    def source(cls, source_id: str) -> PolicyArtifactRef:
        return cls("source", source_id)

    @classmethod
    def project(cls, project_id: str) -> PolicyArtifactRef:
        return cls("project", project_id)

    @classmethod
    def workstream(cls, workstream_id: str) -> PolicyArtifactRef:
        return cls("workstream", workstream_id)


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
            receipt, rules, inputs = self._authorization_inputs(artifact_ref)
            fingerprint = self._fingerprint(artifact_ref, operation, receipt, inputs)
            if receipt is None:
                evaluation = evaluate_portability(
                    Portability.PERSONAL_VAULT,
                    rules,
                    sha256(repr(artifact_ref).encode("utf-8")).hexdigest(),
                )
            else:
                evaluation = reevaluate_portability(receipt, rules)
            if evaluation.allowed:
                return PolicyDecision(True, (), fingerprint)
            return PolicyDecision(False, ("policy-current-denied",), fingerprint)
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
    ) -> tuple[PolicyEvaluation | None, tuple[PolicyRule, ...], tuple[str, ...]]:
        if artifact_ref.kind == "session":
            from mneme.core.sessions import SessionRevisionRef, SessionStore

            request = SessionStore(self._vault).read_revision(
                SessionRevisionRef(artifact_ref.artifact_id, artifact_ref.revision or ""),
                workstream_id=artifact_ref.workstream_id or "",
            )
            return (
                request.policy_evaluation,
                *self._current_rules(
                    workstream_id=request.workstream_id,
                    source_ids=self._session_source_ids(request),
                ),
            )
        if artifact_ref.kind == "memory":
            from mneme.core.memories import MemoryStore

            record = MemoryStore(self._vault).read(artifact_ref.artifact_id)
            project_id = record.scope.project_id
            return (
                record.policy_receipt,
                *self._current_rules(
                    workstream_id=record.scope.workstream_id,
                    project_ids=() if project_id is None else (project_id,),
                    source_ids=tuple(
                        reference.value
                        for reference in record.provenance
                        if reference.kind is ReferenceKind.ID
                        and reference.storage_class is StorageClass.PORTABLE
                        and isinstance(reference.value, str)
                    ),
                ),
            )
        if artifact_ref.kind == "source":
            return (None, *self._current_rules(source_ids=(artifact_ref.artifact_id,)))
        if artifact_ref.kind == "project":
            return (None, *self._current_rules(project_ids=(artifact_ref.artifact_id,)))
        return (None, *self._current_rules(workstream_id=artifact_ref.artifact_id))

    def _current_rules(
        self,
        *,
        workstream_id: str | None = None,
        project_ids: tuple[str, ...] = (),
        source_ids: tuple[str, ...] = (),
    ) -> tuple[tuple[PolicyRule, ...], tuple[str, ...]]:
        policies = PolicyStore(self._vault)
        registries = RegistryStore(self._vault)
        vault_ref = policies.load_active("vault-default", StorageClass.PORTABLE)
        refs: list[PolicyRef] = [vault_ref]
        inputs = [self._input_token("vault", "vault-default", vault_ref)]
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
            source = registries.load_source(source_id)
            inputs.append(self._input_token("source", source.id, source.policy_ref))
            if source.policy_ref is not None:
                refs.append(source.policy_ref)
            if source.project is not None:
                projects.add(source.project)
        for project_id in sorted(projects):
            project = registries.load_project(project_id)
            inputs.append(self._input_token("project", project.id, project.policy_ref))
            if project.policy_ref is not None:
                refs.append(project.policy_ref)
        return tuple(policies.load_rule(reference) for reference in refs), tuple(sorted(inputs))

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

    def _current_tree_references(self) -> tuple[PolicyArtifactRef, ...]:
        root = self._vault.root
        references: list[PolicyArtifactRef] = []
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
